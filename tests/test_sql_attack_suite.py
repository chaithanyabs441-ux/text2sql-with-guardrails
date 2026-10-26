"""Offline corpus and reporting tests for deterministic SQL guardrails."""

from eval.run_sql_attack_suite import evaluate_cases, summarize, write_reports
from eval.sql_attack_suite import ATTACKS, BENIGN_SQL


def test_corpus_has_required_attack_and_benign_coverage():
    assert len(ATTACKS) >= 40
    assert len(BENIGN_SQL) >= 20
    assert {case.category for case in ATTACKS} >= {
        "ddl_dml",
        "multi_statement",
        "catalog_access",
        "dangerous_function",
        "join_tautology",
        "resource_limits",
        "sensitive_columns",
        "obfuscation",
    }


def test_offline_suite_records_guardrail_results_and_known_offset_gap(
    sql_policy, tmp_path
):
    records = evaluate_cases(sql_policy)
    summary = summarize(records)
    _, metrics_path = write_reports(tmp_path, records)

    assert len(records) == len(ATTACKS) + len(BENIGN_SQL)
    assert not [row for row in records if row["status"] == "error"]
    assert not [
        row
        for row in records
        if row["scope"] == "benign" and row["status"] == "blocked"
    ]
    assert next(row for row in records if row["id"] == "join_04")["status"] == "blocked"
    assert next(row for row in records if row["id"] == "resource_01")["status"] == (
        "mitigated_limit"
    )
    assert next(row for row in records if row["id"] == "resource_02")["status"] == (
        "mitigated_offset"
    )
    assert next(
        row for row in summary
        if row["scope"] == "benign_false_positive"
    )["rate"] == 0.0
    assert metrics_path.exists()
