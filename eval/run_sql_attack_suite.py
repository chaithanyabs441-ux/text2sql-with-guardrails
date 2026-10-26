"""Run the deterministic SQL attack corpus directly against SQLPolicyEngine."""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "eval"))

from sql_attack_suite import ATTACKS, BENIGN_SQL  # noqa: E402

from t2s.guardrails.exceptions import UnsafeQueryError  # noqa: E402
from t2s.guardrails.policy import SQLPolicyEngine  # noqa: E402


def _integer_clause(query: exp.Expression, clause: str) -> int | None:
    """Return a literal integer from a LIMIT/OFFSET AST clause, if present."""
    node = query.args.get(clause)
    expression = node.args.get("expression") if node is not None else None
    if not isinstance(expression, exp.Literal) or expression.is_string:
        return None
    try:
        return int(expression.this)
    except (TypeError, ValueError):
        return None


def _was_capped(source_sql: str, safe_sql: str, clause: str, maximum: int) -> bool:
    """Check that every oversized source clause is within the configured bound."""
    try:
        source = sqlglot.parse_one(source_sql, read="postgres")
        safe = sqlglot.parse_one(safe_sql, read="postgres")
    except sqlglot.errors.ParseError:
        return False
    source_queries = [source, *source.find_all(exp.Select)]
    safe_queries = [safe, *safe.find_all(exp.Select)]
    source_values = [_integer_clause(query, clause) for query in source_queries]
    safe_values = [_integer_clause(query, clause) for query in safe_queries]
    oversized_indices = [
        index for index, value in enumerate(source_values)
        if value is not None and value > maximum
    ]
    if not oversized_indices:
        return False
    return all(
        index < len(safe_values)
        and safe_values[index] is not None
        and safe_values[index] <= maximum
        for index in oversized_indices
    )


def evaluate_cases(policy: SQLPolicyEngine | None = None) -> list[dict[str, Any]]:
    """Evaluate every attack and benign query with no LLM or database access."""
    engine = policy or SQLPolicyEngine()
    records: list[dict[str, Any]] = []
    cases = [
        ("adversarial", case.case_id, case.category, case.sql, case.should_block,
         case.expected_mitigation)
        for case in ATTACKS
    ]
    cases.extend(
        ("benign", f"benign_{index:02d}", "benign", sql, False, None)
        for index, sql in enumerate(BENIGN_SQL, start=1)
    )

    for scope, case_id, category, sql, should_block, mitigation in cases:
        try:
            safe_sql = engine.validate(sql)
        except UnsafeQueryError as exc:
            records.append(
                {
                    "scope": scope,
                    "id": case_id,
                    "category": category,
                    "status": "blocked",
                    "block_rule": exc.layer,
                    "should_block": should_block,
                    "expected_mitigation": mitigation,
                    "sql": sql,
                    "validated_sql": None,
                    "error": exc.message,
                }
            )
            continue
        except Exception as exc:  # Keep parser/runtime defects visible in the report.
            records.append(
                {
                    "scope": scope,
                    "id": case_id,
                    "category": category,
                    "status": "error",
                    "block_rule": None,
                    "should_block": should_block,
                    "expected_mitigation": mitigation,
                    "sql": sql,
                    "validated_sql": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        status = "allowed"
        if mitigation == "limit" and _was_capped(sql, safe_sql, "limit", engine.max_rows):
            status = "mitigated_limit"
        elif mitigation == "offset" and _was_capped(sql, safe_sql, "offset", engine.max_rows):
            status = "mitigated_offset"
        records.append(
            {
                "scope": scope,
                "id": case_id,
                "category": category,
                "status": status,
                "block_rule": None,
                "should_block": should_block,
                "expected_mitigation": mitigation,
                "sql": sql,
                "validated_sql": safe_sql,
                "error": None,
            }
        )
    return records


def summarize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Report block rates by attack category/rule and benign false positives."""
    attacks = [row for row in records if row["scope"] == "adversarial"]
    benign = [row for row in records if row["scope"] == "benign"]
    category_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rule_counts: Counter[str] = Counter()
    for row in attacks:
        category_groups[row["category"]].append(row)
        if row["status"] == "blocked":
            rule_counts[row["block_rule"]] += 1

    summary: list[dict[str, Any]] = []
    for category, category_rows in sorted(category_groups.items()):
        count = len(category_rows)
        blocked = sum(row["status"] == "blocked" for row in category_rows)
        mitigated = sum(row["status"].startswith("mitigated_") for row in category_rows)
        allowed = sum(row["status"] == "allowed" for row in category_rows)
        summary.append(
            {
                "scope": "category",
                "category_or_rule": category,
                "attempted": count,
                "blocked": blocked,
                "mitigated": mitigated,
                "allowed": allowed,
                "rate": blocked / count if count else 0.0,
            }
        )

    for rule, count in sorted(rule_counts.items()):
        summary.append(
            {
                "scope": "rule",
                "category_or_rule": rule,
                "attempted": len(attacks),
                "blocked": count,
                "mitigated": 0,
                "allowed": 0,
                "rate": count / len(attacks) if attacks else 0.0,
            }
        )

    false_positives = sum(row["status"] == "blocked" for row in benign)
    summary.append(
        {
            "scope": "benign_false_positive",
            "category_or_rule": "benign",
            "attempted": len(benign),
            "blocked": false_positives,
            "mitigated": 0,
            "allowed": len(benign) - false_positives,
            "rate": false_positives / len(benign) if benign else 0.0,
        }
    )
    return summary


def write_reports(output_dir: Path, records: list[dict[str, Any]]) -> tuple[Path, Path]:
    """Write per-query decisions and aggregate offline security metrics."""
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "sql_attack_raw.jsonl"
    metrics_path = output_dir / "sql_attack_metrics.csv"
    with raw_path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    with metrics_path.open("w", encoding="utf-8", newline="") as stream:
        fields = [
            "scope", "category_or_rule", "attempted", "blocked", "mitigated",
            "allowed", "rate",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summarize(records))
    return raw_path, metrics_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path,
        default=PROJECT_ROOT / "eval" / "results" / "offline_sql_security",
    )
    args = parser.parse_args()
    audit_path = args.output_dir / "policy_audit.jsonl"
    records = evaluate_cases(SQLPolicyEngine(audit_log_path=audit_path))
    raw_path, metrics_path = write_reports(args.output_dir, records)
    attacks = [row for row in records if row["scope"] == "adversarial"]
    blocked = sum(row["status"] == "blocked" for row in attacks)
    mitigated = sum(row["status"].startswith("mitigated_") for row in attacks)
    benign = [row for row in records if row["scope"] == "benign"]
    false_positives = sum(row["status"] == "blocked" for row in benign)
    errors = sum(row["status"] == "error" for row in records)
    print(f"Attack SQL: {blocked}/{len(attacks)} blocked; {mitigated} safely rewritten.")
    print(
        f"Benign SQL false positives: {false_positives}/{len(benign)}; "
        f"unexpected errors: {errors}."
    )
    print(f"Raw results: {raw_path}")
    print(f"Metrics CSV: {metrics_path}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
