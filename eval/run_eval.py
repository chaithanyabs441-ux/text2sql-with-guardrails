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
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from eval.metrics import clopper_pearson_interval  # noqa: E402

from t2s.generation.generator import SQLGenerator  # noqa: E402
from t2s.guardrails.exceptions import UnsafeQueryError  # noqa: E402
from t2s.guardrails.policy import SQLPolicyEngine  # noqa: E402
from t2s.sandbox.executor import ExecutionSandbox  # noqa: E402
from t2s.verification.result_comparison import result_sets_equal  # noqa: E402
from t2s.verification.sanity_checks import ResultSanityChecker  # noqa: E402

DEFAULT_STRATEGIES = ("zero_shot", "schema_glossary", "schema_glossary_selfcorrect")
METRIC_FIELDS = (
    "strategy", "total_questions", "gold_sql_questions", "valid_sql_count",
    "valid_sql_rate", "comparable_pairs", "correct_result_pairs",
    "execution_accuracy_all_gold", "all_gold_ci95_lower", "all_gold_ci95_upper",
    "execution_accuracy_comparable", "comparable_ci95_lower", "comparable_ci95_upper",
    "all_gold_delta_vs_schema_glossary", "comparable_delta_vs_schema_glossary",
)
CATEGORY_METRIC_FIELDS = (
    "strategy", "category", "gold_sql_questions", "correct_result_pairs",
    "execution_accuracy_all_gold", "all_gold_ci95_lower", "all_gold_ci95_upper",
    "comparable_pairs", "comparable_correct", "execution_accuracy_comparable",
    "comparable_ci95_lower", "comparable_ci95_upper",
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
        self.sanity_checker = ResultSanityChecker()
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
                record = self._run_one(question, strategy, gold)
                raw_records.append(record)
                aggregate = stats[strategy]
                if gold_sql is not None:
                    aggregate["gold_sql_questions"] += 1
                if record["safe_sql"]:
                    aggregate["valid_sql_count"] += 1
                if record["result_match"] is not None:
                    aggregate["comparable_pairs"] += 1
                    if record["result_match"]:
                        aggregate["correct_result_pairs"] += 1

        metrics = [self._format_metrics(key, value) for key, value in stats.items()]
        baseline = next(
            (row for row in metrics if row["strategy"] == "schema_glossary"), None
        )
        for row in metrics:
            row["all_gold_delta_vs_schema_glossary"] = None
            row["comparable_delta_vs_schema_glossary"] = None
            if row["strategy"] != "schema_glossary_selfcorrect" or baseline is None:
                continue
            for field, delta_field in (
                ("execution_accuracy_all_gold", "all_gold_delta_vs_schema_glossary"),
                ("execution_accuracy_comparable", "comparable_delta_vs_schema_glossary"),
            ):
                if row[field] is not None and baseline[field] is not None:
                    row[delta_field] = row[field] - baseline[field]
        return raw_records, metrics

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
    ) -> dict[str, Any]:
        response = None
        safe_sql = ""
        generation_error = None
        generated_frame = None
        generated_latency_ms = None
        generated_error = None
        sanity_warnings: list[str] = []
        retry_count = 0
        retry_feedback = None
        attempts: list[dict[str, Any]] = []
        usage_records: list[dict[str, Any]] = []

        while True:
            response = None
            safe_sql = ""
            generation_error = None
            generated_frame = None
            generated_latency_ms = None
            generated_error = None
            sanity_warnings = []
            attempt = {
                "attempt_number": len(attempts) + 1,
                "generated_sql": None,
                "safe_sql": "",
                "generation_error": None,
                "execution_error": None,
                "execution_latency_ms": None,
                "result_row_count": None,
                "sanity_warnings": [],
                "retry_reason": None,
                "cache_status": "unknown",
                "generation_latency_ms": None,
                "usage": None,
            }
            started = time.perf_counter()
            try:
                response, generated_sql = self.generator.generate(
                    question["question"],
                    strategy=strategy,
                    database_error=retry_feedback,
                )
                attempt["generated_sql"] = generated_sql
                try:
                    usage = self.generator.llm.get_usage()
                    if usage:
                        usage_records.append(usage)
                        attempt["usage"] = usage
                        attempt["cache_status"] = (
                            "cached" if usage.get("cache_hit") is True else "live"
                        )
                except Exception:
                    pass
                safe_sql = self.policy.validate(generated_sql) if generated_sql else ""
                attempt["safe_sql"] = safe_sql
            except Exception as exc:
                generation_error = f"{type(exc).__name__}: {exc}"
                attempt["generation_error"] = generation_error
                attempt["generation_latency_ms"] = round(
                    (time.perf_counter() - started) * 1000, 3
                )
                attempts.append(attempt)
                break
            attempt["generation_latency_ms"] = round(
                (time.perf_counter() - started) * 1000, 3
            )

            if not safe_sql:
                attempts.append(attempt)
                break
            if self.database_blocked:
                generated_error = (
                    "Skipped after database connection failure: "
                    f"{self.database_blocked}"
                )
                attempt["execution_error"] = generated_error
                attempts.append(attempt)
                break

            generated_frame, generated_latency_ms, generated_error = (
                self.sandbox.execute(safe_sql)
            )
            if generated_frame is None and generated_error is None:
                generated_error = "Executor returned no result dataframe."
            if _is_connection_error(generated_error):
                self.database_blocked = generated_error
            attempt["execution_error"] = generated_error
            attempt["execution_latency_ms"] = generated_latency_ms
            attempt["result_row_count"] = (
                len(generated_frame) if generated_frame is not None else None
            )
            sanity_warnings = (
                self.sanity_checker.check(generated_frame)
                if generated_error is None and generated_frame is not None
                else []
            )
            attempt["sanity_warnings"] = sanity_warnings

            if generated_error:
                retry_reason = f"Execution error: {generated_error}"
            elif sanity_warnings:
                retry_reason = "; ".join(sanity_warnings)
            else:
                retry_reason = None

            should_retry = (
                retry_reason is not None
                and not self.database_blocked
                and strategy == "schema_glossary_selfcorrect"
                and retry_count < 2
            )
            if should_retry:
                attempt["retry_reason"] = retry_reason
                attempts.append(attempt)
                retry_count += 1
                retry_feedback = retry_reason[:2000]
                continue

            attempts.append(attempt)
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
                "cached_calls": sum(item.get("cache_hit") is True for item in usage_records),
                "live_calls": sum(item.get("cache_hit") is not True for item in usage_records),
            }

        result_match = None
        if (
            generated_frame is not None
            and generated_error is None
            and gold["rows"] is not None
            and gold["error"] is None
        ):
            gold_frame = pd.DataFrame(gold["rows"])
            result_match = result_sets_equal(gold_frame, generated_frame)

        record = {
            "question_id": question.get("id", "unknown"),
            "question": question.get("question"),
            "category": question.get("category", "uncategorized"),
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
            "attempts": attempts,
            "generated_rows": _records(generated_frame)
            if generated_error is None
            else None,
            "generated_execution_error": generated_error,
            "generated_execution_latency_ms": generated_latency_ms,
            "retry_count": retry_count,
            "usage": usage,
            "sanity_warnings": sanity_warnings,
            "result_match": result_match,
        }
        return record

    @staticmethod
    def _format_metrics(strategy: str, aggregate: dict[str, Any]) -> dict[str, Any]:
        total = aggregate["total_questions"]
        compared = aggregate["comparable_pairs"]
        gold_sql_questions = aggregate["gold_sql_questions"]
        correct = aggregate["correct_result_pairs"]
        all_gold_lower, all_gold_upper = clopper_pearson_interval(
            correct, gold_sql_questions
        )
        comparable_lower, comparable_upper = clopper_pearson_interval(correct, compared)
        return {
            "strategy": strategy,
            "total_questions": total,
            "gold_sql_questions": aggregate["gold_sql_questions"],
            "valid_sql_count": aggregate["valid_sql_count"],
            "valid_sql_rate": aggregate["valid_sql_count"] / total if total else 0.0,
            "comparable_pairs": compared,
            "correct_result_pairs": correct,
            "execution_accuracy_all_gold": (
                correct / gold_sql_questions if gold_sql_questions else None
            ),
            "all_gold_ci95_lower": all_gold_lower,
            "all_gold_ci95_upper": all_gold_upper,
            "execution_accuracy_comparable": correct / compared if compared else None,
            "comparable_ci95_lower": comparable_lower,
            "comparable_ci95_upper": comparable_upper,
        }


def _category_metrics(raw_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format accuracy, denominators, and exact intervals per category."""
    grouped: dict[tuple[str, str], dict[str, int]] = {}
    for record in raw_records:
        if record.get("gold_sql") is None:
            continue
        key = (record.get("strategy", ""), record.get("category", "uncategorized"))
        stats = grouped.setdefault(
            key, {"gold": 0, "correct": 0, "comparable": 0, "comparable_correct": 0}
        )
        stats["gold"] += 1
        if record.get("result_match") is not None:
            stats["comparable"] += 1
            stats["comparable_correct"] += int(record["result_match"])
        stats["correct"] += int(record.get("result_match") is True)

    category_rows = []
    for (strategy, category), stats in sorted(grouped.items()):
        all_lower, all_upper = clopper_pearson_interval(stats["correct"], stats["gold"])
        comparable_lower, comparable_upper = clopper_pearson_interval(
            stats["comparable_correct"], stats["comparable"]
        )
        category_rows.append(
            {
                "strategy": strategy,
                "category": category,
                "gold_sql_questions": stats["gold"],
                "correct_result_pairs": stats["correct"],
                "execution_accuracy_all_gold": stats["correct"] / stats["gold"],
                "all_gold_ci95_lower": all_lower,
                "all_gold_ci95_upper": all_upper,
                "comparable_pairs": stats["comparable"],
                "comparable_correct": stats["comparable_correct"],
                "execution_accuracy_comparable": (
                    stats["comparable_correct"] / stats["comparable"]
                    if stats["comparable"]
                    else None
                ),
                "comparable_ci95_lower": comparable_lower,
                "comparable_ci95_upper": comparable_upper,
            }
        )
    return category_rows


def write_reports(
    output_dir: Path,
    raw_records: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
) -> tuple[Path, Path]:
    """Write raw attempts, strategy metrics, and per-category metrics."""
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "raw_outputs.jsonl"
    csv_path = output_dir / "metrics.csv"
    category_path = output_dir / "category_metrics.csv"
    with raw_path.open("w", encoding="utf-8", newline="\n") as raw_file:
        for record in raw_records:
            raw_file.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
    with csv_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=METRIC_FIELDS)
        writer.writeheader()
        writer.writerows(metrics)
    with category_path.open("w", encoding="utf-8", newline="") as category_file:
        writer = csv.DictWriter(category_file, fieldnames=CATEGORY_METRIC_FIELDS)
        writer.writeheader()
        writer.writerows(_category_metrics(raw_records))
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
    print(f"Category metrics CSV: {args.output_dir / 'category_metrics.csv'}")
    for row in metrics:
        if row["strategy"] == "schema_glossary_selfcorrect":
            all_gold_delta = row["all_gold_delta_vs_schema_glossary"]
            comparable_delta = row["comparable_delta_vs_schema_glossary"]
            print(
                "Self-correction delta vs schema_glossary "
                f"(all gold): {all_gold_delta if all_gold_delta is not None else 'PENDING'}; "
                "comparable: "
                f"{comparable_delta if comparable_delta is not None else 'PENDING'}"
            )
    if runner.database_blocked:
        print(f"Database execution unavailable: {runner.database_blocked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
