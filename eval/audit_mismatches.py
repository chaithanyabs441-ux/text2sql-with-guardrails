"""Rescore saved outputs and export a small audit of remaining mismatches.

The output CSV leaves ``my_label`` empty for manual classification as
``real_error``, ``comparison_artifact``, or ``gold_sql_bug``. This script only
reads saved evaluation outputs; it does not call an LLM or execute SQL.
"""

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from t2s.verification.result_comparison import (  # noqa: E402
    result_fingerprint,
    result_sets_equal,
)

RAW_OUTPUTS = PROJECT_ROOT / "eval" / "results" / "raw_outputs.jsonl"
RESCORE_CSV = PROJECT_ROOT / "eval" / "results" / "comparison_rescore.csv"
MISMATCH_CSV = PROJECT_ROOT / "eval" / "results" / "mismatch_audit.csv"


def _old_result_sets_equal(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    """Reproduce the saved run's historical exact comparison for reference."""
    return result_fingerprint(left) == result_fingerprint(right)


def _load_records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as result_file:
        return [json.loads(line) for line in result_file if line.strip()]


def _rows_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _is_comparable(record: dict[str, Any]) -> bool:
    return (
        record.get("gold_execution_error") is None
        and record.get("generated_execution_error") is None
        and record.get("gold_rows") is not None
        and record.get("generated_rows") is not None
    )


def _first_rows(rows: list[dict[str, Any]], limit: int = 5) -> str:
    return json.dumps(rows[:limit], ensure_ascii=False, default=str)


def main() -> int:
    records = _load_records(RAW_OUTPUTS)
    aggregate: dict[str, dict[str, int]] = defaultdict(
        lambda: {"comparable": 0, "old_correct": 0, "new_correct": 0}
    )
    mismatches: list[dict[str, Any]] = []

    for record in records:
        if not _is_comparable(record):
            continue
        gold_rows = record["gold_rows"]
        generated_rows = record["generated_rows"]
        gold = _rows_frame(gold_rows)
        generated = _rows_frame(generated_rows)
        old_match = _old_result_sets_equal(generated, gold)
        new_match = result_sets_equal(gold, generated)

        strategy_stats = aggregate[record["strategy"]]
        strategy_stats["comparable"] += 1
        strategy_stats["old_correct"] += int(old_match)
        strategy_stats["new_correct"] += int(new_match)

        if not new_match and len(mismatches) < 25:
            mismatches.append(
                {
                    "question_id": record.get("question_id", ""),
                    "strategy": record.get("strategy", ""),
                    "question": record.get("question", ""),
                    "gold_sql": record.get("gold_sql", ""),
                    "generated_sql": record.get("safe_sql", ""),
                    "gold_first_rows": _first_rows(gold_rows),
                    "generated_first_rows": _first_rows(generated_rows),
                    "my_label": "",
                }
            )

    RESCORE_CSV.parent.mkdir(parents=True, exist_ok=True)
    rescore_fields = [
        "strategy",
        "comparable_pairs",
        "old_correct",
        "old_accuracy",
        "new_correct",
        "new_accuracy",
    ]
    rescore_rows = []
    for strategy, stats in sorted(aggregate.items()):
        comparable = stats["comparable"]
        rescore_rows.append(
            {
                "strategy": strategy,
                "comparable_pairs": comparable,
                "old_correct": stats["old_correct"],
                "old_accuracy": stats["old_correct"] / comparable if comparable else "",
                "new_correct": stats["new_correct"],
                "new_accuracy": stats["new_correct"] / comparable if comparable else "",
            }
        )
    with RESCORE_CSV.open("w", encoding="utf-8", newline="") as rescore_file:
        writer = csv.DictWriter(rescore_file, fieldnames=rescore_fields)
        writer.writeheader()
        writer.writerows(rescore_rows)

    mismatch_fields = [
        "question_id",
        "strategy",
        "question",
        "gold_sql",
        "generated_sql",
        "gold_first_rows",
        "generated_first_rows",
        "my_label",
    ]
    with MISMATCH_CSV.open("w", encoding="utf-8", newline="") as mismatch_file:
        writer = csv.DictWriter(mismatch_file, fieldnames=mismatch_fields)
        writer.writeheader()
        writer.writerows(mismatches)

    print("Old vs new accuracy among successfully comparable saved outputs:")
    for row in rescore_rows:
        print(
            f"{row['strategy']}: {row['old_correct']}/{row['comparable_pairs']} "
            f"({row['old_accuracy']:.1%}) -> {row['new_correct']}/"
            f"{row['comparable_pairs']} ({row['new_accuracy']:.1%})"
        )
    print(f"Rescore CSV: {RESCORE_CSV}")
    print(f"First {len(mismatches)} remaining mismatches: {MISMATCH_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
