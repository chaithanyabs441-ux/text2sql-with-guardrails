"""Offline tests for red-team aggregate metrics."""

import csv
import json

from eval.run_redteam import summarize, write_reports


def test_summarize_reports_category_layer_and_benign_false_positive_rates(tmp_path):
    records = [
        {
            "scope": "adversarial", "category": "destructive",
            "status": "blocked", "block_layer": "FORBIDDEN_OPERATION",
        },
        {
            "scope": "adversarial", "category": "destructive",
            "status": "allowed", "block_layer": None,
            "safe_sql": "SELECT id FROM customers LIMIT 1000",
        },
        {
            "scope": "benign", "category": "lookup",
            "status": "blocked", "block_layer": "generation_abstention",
        },
        {
            "scope": "benign", "category": "lookup",
            "status": "allowed", "block_layer": None,
            "safe_sql": "SELECT id FROM customers LIMIT 1000",
        },
    ]

    summary = summarize(records)
    category = next(row for row in summary if row["scope"] == "category")
    layer = next(row for row in summary if row["scope"] == "layer")
    benign = next(row for row in summary if row["scope"] == "benign_false_positive")
    _, csv_path = write_reports(tmp_path, records)
    raw_rows = [
        json.loads(line)
        for line in (tmp_path / "redteam_raw.jsonl").read_text().splitlines()
    ]
    outcome_rows = list(
        csv.DictReader((tmp_path / "redteam_outcome_metrics.csv").open(encoding="utf-8"))
    )

    assert category["rate"] == 0.5
    assert layer["category_or_layer"] == "FORBIDDEN_OPERATION"
    assert layer["rate"] == 0.5
    assert benign["rate"] == 0.5
    assert len(list(csv.DictReader(csv_path.open(encoding="utf-8")))) == 3
    assert raw_rows[0]["outcome"] == "blocked_by_guardrail"
    assert raw_rows[1]["outcome"] == "executed_harmless"
    assert any(row["outcome"] == "executed_harmful" for row in outcome_rows)
