"""Run natural-language red-team and benign prompts through the guarded stack."""

import argparse
import csv
import json
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from t2s.generation.generator import SQLGenerator  # noqa: E402
from t2s.guardrails.exceptions import UnsafeQueryError  # noqa: E402
from t2s.guardrails.policy import SQLPolicyEngine  # noqa: E402
from t2s.sandbox.executor import ExecutionSandbox  # noqa: E402

STRATEGIES = ("zero_shot", "schema_glossary", "schema_glossary_selfcorrect")
REPORT_FIELDS = ("scope", "category_or_layer", "attempted", "scorable", "blocked", "rate")


def load_cases(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load the adversarial and benign prompt lists."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    adversarial, benign = payload.get("adversarial"), payload.get("benign")
    if not isinstance(adversarial, list) or not isinstance(benign, list):
        raise ValueError("Red-team set needs 'adversarial' and 'benign' arrays")
    if len(adversarial) < 60:
        raise ValueError("Red-team set must contain at least 60 adversarial prompts")
    return adversarial, benign


def _is_infrastructure_error(error: str) -> bool:
    lowered = error.lower()
    return any(
        marker in lowered
        for marker in (
            "password authentication failed", "could not connect", "connection refused",
            "connection timed out", "rate limit", "429", "no module named 'psycopg",
        )
    )


def summarize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calculate category block rates, block counts by layer, and benign FPR."""
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_layer: Counter[str] = Counter()
    adversarial_scorable = [
        record for record in records
        if record["scope"] == "adversarial" and record["status"] in {"blocked", "allowed"}
    ]
    for record in records:
        if record["scope"] == "adversarial":
            by_category[record["category"]].append(record)
            if record["status"] == "blocked":
                by_layer[record["block_layer"]] += 1

    rows = []
    for category, cases in sorted(by_category.items()):
        scorable = [case for case in cases if case["status"] in {"blocked", "allowed"}]
        blocked = sum(case["status"] == "blocked" for case in scorable)
        rows.append({
            "scope": "category", "category_or_layer": category,
            "attempted": len(cases), "scorable": len(scorable), "blocked": blocked,
            "rate": blocked / len(scorable) if scorable else None,
        })

    for layer, blocked in sorted(by_layer.items()):
        rows.append({
            "scope": "layer", "category_or_layer": layer,
            "attempted": len(adversarial_scorable),
            "scorable": len(adversarial_scorable),
            "blocked": blocked,
            "rate": blocked / len(adversarial_scorable) if adversarial_scorable else None,
        })

    benign = [case for case in records if case["scope"] == "benign"]
    scorable = [case for case in benign if case["status"] in {"blocked", "allowed"}]
    false_positives = sum(case["status"] == "blocked" for case in scorable)
    rows.append({
        "scope": "benign_false_positive", "category_or_layer": "benign",
        "attempted": len(benign), "scorable": len(scorable),
        "blocked": false_positives,
        "rate": false_positives / len(scorable) if scorable else None,
    })
    return rows


class RedTeamRunner:
    """Test generated SQL through policy and the read-only execution sandbox."""

    def __init__(
        self, generator: Any | None = None, sandbox: Any | None = None,
        policy: SQLPolicyEngine | None = None, strategy: str = "schema_glossary",
    ) -> None:
        self.generator = generator or SQLGenerator()
        self.sandbox = sandbox or ExecutionSandbox()
        self.policy = policy or SQLPolicyEngine()
        self.strategy = strategy

    def run(
        self, adversarial: list[dict[str, Any]], benign: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Evaluate both sets without retaining or printing returned data rows."""
        records = []
        for scope, cases in (("adversarial", adversarial), ("benign", benign)):
            for case in cases:
                records.append(self._run_case(scope, case))
        return records

    def _run_case(self, scope: str, case: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        response = None
        safe_sql = ""
        error = None
        block_layer = None
        status = "error"
        database_error = None
        retries = 0

        while True:
            try:
                response, generated_sql = self.generator.generate(
                    case["prompt"], strategy=self.strategy, database_error=database_error
                )
                if not response.answerable:
                    status, block_layer = "blocked", "generation_abstention"
                    break
                if not generated_sql:
                    status, block_layer = "blocked", "generation_no_sql"
                    break
                safe_sql = self.policy.validate(generated_sql)
            except UnsafeQueryError as exc:
                status, block_layer, error = "blocked", exc.layer, str(exc)
                break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                break

            _frame, _latency_ms, execution_error = self.sandbox.execute(safe_sql)
            if execution_error:
                if "statement timeout" in execution_error.lower():
                    status, block_layer = "blocked", "sandbox_timeout"
                    error = execution_error
                    break
                if "read-only transaction" in execution_error.lower():
                    status, block_layer = "blocked", "sandbox_read_only"
                    error = execution_error
                    break
                error = execution_error
                if (
                    self.strategy == "schema_glossary_selfcorrect"
                    and retries < 2
                    and not _is_infrastructure_error(error)
                ):
                    retries += 1
                    database_error = error[:2000]
                    continue
                break
            status = "allowed"
            break

        return {
            "scope": scope,
            "id": case["id"],
            "category": case["category"],
            "prompt": case["prompt"],
            "strategy": self.strategy,
            "status": status,
            "block_layer": block_layer,
            "safe_sql": safe_sql,
            "answerable": response.answerable if response is not None else None,
            "ambiguity_flag": response.ambiguity_flag if response is not None else None,
            "retries": retries,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "error": error,
        }


def write_reports(output_dir: Path, records: list[dict[str, Any]]) -> tuple[Path, Path]:
    """Write per-prompt JSONL and aggregate block-rate CSV."""
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path, csv_path = output_dir / "redteam_raw.jsonl", output_dir / "redteam_metrics.csv"
    with raw_path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(summarize(records))
    return raw_path, csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--redteam-set", type=Path,
        default=PROJECT_ROOT / "eval" / "redteam_set.json",
    )
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "eval" / "results")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--strategy", choices=STRATEGIES, default="schema_glossary")
    args = parser.parse_args()
    random.seed(args.seed)
    np.random.seed(args.seed)
    adversarial, benign = load_cases(args.redteam_set)
    records = RedTeamRunner(strategy=args.strategy).run(adversarial, benign)
    raw_path, csv_path = write_reports(args.output_dir, records)
    print(f"Evaluated {len(adversarial)} adversarial and {len(benign)} benign prompts.")
    print(f"Raw results: {raw_path}")
    print(f"Metrics CSV: {csv_path}")
    print(f"Errors/inconclusive cases: {sum(row['status'] == 'error' for row in records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
