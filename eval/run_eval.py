"""Run the golden set against each SQL prompting strategy."""

import argparse
import csv
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from t2s.generation.generator import SQLGenerator  # noqa: E402
from t2s.guardrails.exceptions import UnsafeQueryError  # noqa: E402
from t2s.guardrails.policy import SQLPolicyEngine  # noqa: E402
from t2s.sandbox.executor import ExecutionSandbox  # noqa: E402
from t2s.verification.result_comparison import result_sets_equal  # noqa: E402

DEFAULT_STRATEGIES = ("zero_shot", "schema_glossary", "schema_glossary_selfcorrect")
METRIC_FIELDS = (
    "strategy", "total_questions", "gold_sql_questions", "valid_sql_count",
    "valid_sql_rate", "comparable_pairs", "correct_result_pairs", "execution_accuracy",
    "average_generation_latency_ms", "prompt_tokens", "completion_tokens",
    "estimated_token_cost_usd",
)


def load_questions(path: Path) -> list[dict[str, Any]]:
    """Read the golden-set question array."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    questions = payload.get("questions")
    if not isinstance(questions, list):
        raise ValueError("Golden set must contain a 'questions' array")
    return questions


def _records(frame: pd.DataFrame | None) -> list[dict[str, Any]] | None:
    if frame is None:
        return None
    return frame.to_dict(orient="records")


def _is_connection_error(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    return any(
        marker in lowered
        for marker in (
            "password authentication failed",
            "could not connect",
            "connection refused",
            "connection timed out",
            "no module named 'psycopg",
        )
    )


class EvaluationRunner:
    """Generate, execute, compare, and persist one evaluation run."""

    def __init__(
        self,
        generator: Any | None = None,
        sandbox: Any | None = None,
        policy: SQLPolicyEngine | None = None,
        seed: int = 42,
    ) -> None:
        self.generator = generator or SQLGenerator()
        self.sandbox = sandbox or ExecutionSandbox()
        self.policy = policy or SQLPolicyEngine()
        self.database_blocked: str | None = None
        self.seed = seed

    def run(
        self,
        questions: list[dict[str, Any]],
        strategies: tuple[str, ...] = DEFAULT_STRATEGIES,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return detailed raw records and one aggregate metric row per strategy."""
        raw_records: list[dict[str, Any]] = []
        stats = {
            strategy: {
                "total_questions": len(questions),
                "gold_sql_questions": 0,
                "valid_sql_count": 0,
                "comparable_pairs": 0,
                "correct_result_pairs": 0,
                "generation_latency_ms": [],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
                "usage_count": 0,
                "cost_complete": True,
            }
            for strategy in strategies
        }
        gold_cache: dict[str, dict[str, Any]] = {}
        for question in questions:
            question_id = question.get("id", "unknown")
            gold_sql = question.get("gold_sql")
            gold = gold_cache.get(question_id)
            if gold is None:
                gold = self._run_gold(gold_sql)
                gold_cache[question_id] = gold

            for strategy in strategies:
                record, usage = self._run_one(question, strategy, gold)
                raw_records.append(record)
                aggregate = stats[strategy]
                aggregate["generation_latency_ms"].append(
                    record["generation_latency_ms"]
                )
                if gold_sql is not None:
                    aggregate["gold_sql_questions"] += 1
                if record["safe_sql"]:
                    aggregate["valid_sql_count"] += 1
                if record["result_match"] is not None:
                    aggregate["comparable_pairs"] += 1
                    if record["result_match"]:
                        aggregate["correct_result_pairs"] += 1

                if usage is None:
                    aggregate["cost_complete"] = False
                else:
                    aggregate["usage_count"] += 1
                    aggregate["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
                    aggregate["completion_tokens"] += usage.get(
                        "completion_tokens", 0
                    ) or 0
                    cost = usage.get("estimated_cost_usd")
                    if cost is None:
                        aggregate["cost_complete"] = False
                    else:
                        aggregate["cost_usd"] += cost

        return raw_records, [self._format_metrics(key, value) for key, value in stats.items()]

    def _run_gold(self, gold_sql: str | None) -> dict[str, Any]:
        if gold_sql is None:
            return {"sql": None, "rows": None, "error": None, "latency_ms": None}
        try:
            safe_sql = self.policy.validate(gold_sql)
        except UnsafeQueryError as exc:
            return {"sql": gold_sql, "rows": None, "error": str(exc), "latency_ms": None}
        if self.database_blocked:
            return {
                "sql": safe_sql,
                "rows": None,
                "error": f"Skipped after database connection failure: {self.database_blocked}",
                "latency_ms": None,
            }
        frame, latency_ms, error = self.sandbox.execute(safe_sql)
        if _is_connection_error(error):
            self.database_blocked = error
        return {
            "sql": safe_sql,
            "rows": _records(frame) if error is None else None,
            "error": error,
            "latency_ms": latency_ms,
        }

    def _run_one(
        self, question: dict[str, Any], strategy: str, gold: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        response = None
        safe_sql = ""
        generation_error = None
        generated_frame = None
        generated_latency_ms = None
        generated_error = None
        generation_latency_ms = 0.0
        retry_count = 0
        database_error = None
        usage_records = []

        while True:
            started = time.perf_counter()
            try:
                response, generated_sql = self.generator.generate(
                    question["question"],
                    strategy=strategy,
                    database_error=database_error,
                )
                safe_sql = self.policy.validate(generated_sql) if generated_sql else ""
                try:
                    usage = self.generator.llm.get_usage()
                    if usage:
                        usage_records.append(usage)
                except Exception:
                    pass
            except Exception as exc:
                generation_error = f"{type(exc).__name__}: {exc}"
                generation_latency_ms += (time.perf_counter() - started) * 1000
                break
            generation_latency_ms += (time.perf_counter() - started) * 1000

            if not safe_sql:
                break
            if self.database_blocked:
                generated_error = (
                    "Skipped after database connection failure: "
                    f"{self.database_blocked}"
                )
                break

            generated_frame, generated_latency_ms, generated_error = (
                self.sandbox.execute(safe_sql)
            )
            if _is_connection_error(generated_error):
                self.database_blocked = generated_error
            if (
                generated_error
                and not self.database_blocked
                and strategy == "schema_glossary_selfcorrect"
                and retry_count < 2
            ):
                retry_count += 1
                database_error = generated_error[:2000]
                continue
            break

        usage = None
        if usage_records:
            costs = [item.get("estimated_cost_usd") for item in usage_records]
            usage = {
                "prompt_tokens": sum(item.get("prompt_tokens", 0) or 0 for item in usage_records),
                "completion_tokens": sum(
                    item.get("completion_tokens", 0) or 0 for item in usage_records
                ),
                "estimated_cost_usd": (
                    sum(costs) if all(cost is not None for cost in costs) else None
                ),
                "calls": len(usage_records),
            }

        result_match = None
        if (
            generated_frame is not None
            and generated_error is None
            and gold["rows"] is not None
            and gold["error"] is None
        ):
            gold_frame = pd.DataFrame(gold["rows"])
            result_match = result_sets_equal(generated_frame, gold_frame)

        record = {
            "question_id": question.get("id", "unknown"),
            "question": question.get("question"),
            "strategy": strategy,
            "seed": self.seed,
            "expected_answerable": question.get("expected_answerable"),
            "expected_ambiguity": question.get("expected_ambiguity"),
            "gold_sql": gold["sql"],
            "gold_rows": gold["rows"],
            "gold_execution_error": gold["error"],
            "gold_execution_latency_ms": gold["latency_ms"],
            "generation": response.model_dump() if response is not None else None,
            "safe_sql": safe_sql,
            "generation_error": generation_error,
            "generated_rows": _records(generated_frame)
            if generated_error is None
            else None,
            "generated_execution_error": generated_error,
            "generated_execution_latency_ms": generated_latency_ms,
            "generation_latency_ms": round(generation_latency_ms, 3),
            "retry_count": retry_count,
            "usage": usage,
            "result_match": result_match,
        }
        return record, usage

    @staticmethod
    def _format_metrics(strategy: str, aggregate: dict[str, Any]) -> dict[str, Any]:
        total = aggregate["total_questions"]
        compared = aggregate["comparable_pairs"]
        latencies = aggregate["generation_latency_ms"]
        cost = (
            round(aggregate["cost_usd"], 10)
            if aggregate["usage_count"] and aggregate["cost_complete"]
            else None
        )
        return {
            "strategy": strategy,
            "total_questions": total,
            "gold_sql_questions": aggregate["gold_sql_questions"],
            "valid_sql_count": aggregate["valid_sql_count"],
            "valid_sql_rate": aggregate["valid_sql_count"] / total if total else 0.0,
            "comparable_pairs": compared,
            "correct_result_pairs": aggregate["correct_result_pairs"],
            "execution_accuracy": (
                aggregate["correct_result_pairs"] / compared if compared else None
            ),
            "average_generation_latency_ms": (
                round(sum(latencies) / len(latencies), 3) if latencies else None
            ),
            "prompt_tokens": aggregate["prompt_tokens"],
            "completion_tokens": aggregate["completion_tokens"],
            "estimated_token_cost_usd": cost,
        }


def write_reports(
    output_dir: Path,
    raw_records: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
) -> tuple[Path, Path]:
    """Write per-attempt JSONL and per-strategy CSV reports."""
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw_outputs.jsonl"
    csv_path = output_dir / "metrics.csv"
    with raw_path.open("w", encoding="utf-8", newline="\n") as raw_file:
        for record in raw_records:
            raw_file.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
    with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=METRIC_FIELDS)
        writer.writeheader()
        writer.writerows(metrics)
    return raw_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--golden-set", type=Path, default=PROJECT_ROOT / "eval" / "golden_set.json"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "eval" / "results"
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--strategies", nargs="+", choices=sorted(DEFAULT_STRATEGIES))
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)
    questions = load_questions(args.golden_set)
    strategies = tuple(args.strategies or DEFAULT_STRATEGIES)
    runner = EvaluationRunner(seed=args.seed)
    raw_records, metrics = runner.run(questions, strategies)
    raw_path, csv_path = write_reports(args.output_dir, raw_records, metrics)
    print(f"Evaluated {len(questions)} questions across {len(strategies)} strategies.")
    print(f"Raw outputs: {raw_path}")
    print(f"Metrics CSV: {csv_path}")
    if runner.database_blocked:
        print(f"Database execution unavailable: {runner.database_blocked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
