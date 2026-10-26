"""Offline tests for red-team aggregate metrics."""

import csv

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
        },
        {
            "scope": "benign", "category": "lookup",
            "status": "blocked", "block_layer": "generation_abstention",
        },
        {
            "scope": "benign", "category": "lookup",
            "status": "allowed", "block_layer": None,
        },
    ]

    summary = summarize(records)
    category = next(row for row in summary if row["scope"] == "category")
    layer = next(row for row in summary if row["scope"] == "layer")
    benign = next(row for row in summary if row["scope"] == "benign_false_positive")
    _, csv_path = write_reports(tmp_path, records)

    assert category["rate"] == 0.5
    assert layer["category_or_layer"] == "FORBIDDEN_OPERATION"
    assert layer["rate"] == 0.5
    assert benign["rate"] == 0.5
    assert len(list(csv.DictReader(csv_path.open(encoding="utf-8")))) == 3
