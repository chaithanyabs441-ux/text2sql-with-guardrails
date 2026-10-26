"""Offline tests for confidence signal extraction and held-out calibration."""

import json

import numpy as np
from eval import run_calibration
from eval.run_calibration import build_samples

from t2s.verification.calibration import (
    FEATURE_NAMES,
    fit_and_evaluate,
    tune_abstention_thresholds,
    write_reliability_svg,
)


def _samples() -> list[dict[str, object]]:
    rows = []
    for question_index in range(40):
        correct = question_index % 2 == 0
        for _strategy_index in range(3):
            rows.append(
                {
                    "question_id": f"q{question_index}",
                    "correct": correct,
                    "back_translation_similarity": 0.85 if correct else 0.35,
                    "agreement_ratio": 0.9 if correct else 0.4,
                    "sanity_warning_count": 0 if correct else 2,
                    "self_reported_confidence": 0.8 if correct else 0.6,
                    "schema_coverage": 1.0 if correct else 0.5,
                }
            )
    return rows


def test_calibrator_splits_by_question_and_reports_baseline(tmp_path):
    rows = _samples()
    model, report, labels, probabilities = fit_and_evaluate(
        rows, seed=7, bootstrap_samples=100
    )

    assert len(FEATURE_NAMES) == model.coef_.shape[1] == 5
    assert report.sample_count == len(rows)
    assert report.question_count == 40
    assert report.repeat_count == 3
    assert report.fold_count == 5
    assert report.seeds == (7, 8, 9)
    assert len(labels) == len(probabilities) == len(rows)
    assert report.calibrator["auroc"] == 1.0
    assert report.calibrator["brier_score"] < report.self_reported_confidence["brier_score"]
    assert report.calibrator["ece"] is not None
    assert set(report.bootstrap_intervals) == {
        "combined",
        "self_reported_confidence_platt",
    }
    assert len(report.signal_ablation) == 11
    assert report.selective_prediction
    assert report.abstention_thresholds["answer_threshold"] <= 1.0

    diagram = tmp_path / "reliability.svg"
    write_reliability_svg(diagram, labels, probabilities)
    assert '<svg ' in diagram.read_text(encoding="utf-8")
    from t2s.verification.calibration import write_ablation_svg, write_selective_svg

    selective = tmp_path / "selective.svg"
    ablation = tmp_path / "ablation.svg"
    write_selective_svg(selective, report.selective_prediction)
    write_ablation_svg(ablation, report.signal_ablation)
    assert '<svg ' in selective.read_text(encoding="utf-8")
    assert '<svg ' in ablation.read_text(encoding="utf-8")


def test_warn_threshold_is_selected_within_the_warning_band():
    thresholds = tune_abstention_thresholds(
        np.asarray([0, 0, 1, 1]), np.asarray([0.2, 0.4, 0.7, 0.9])
    )

    assert thresholds == {"answer_threshold": 0.7, "warn_threshold": 0.2}


def test_signal_extraction_uses_candidate_agreement_and_cached_judge(tmp_path):
    class StubDetector:
        def back_translate(self, sql):
            return "list the rows"

        def calculate_similarity(self, question, translated):
            return 0.75

    records = []
    for index, (sql, rows, correct) in enumerate(
        [
            ("SELECT id FROM orders", [{"id": 1}], True),
            ("SELECT id FROM orders", [{"id": 1}], True),
            ("SELECT id FROM customers", [{"id": 2}], False),
        ]
    ):
        records.append(
            {
                "question_id": "q1",
                "question": "list rows",
                "strategy": f"s{index}",
                "safe_sql": sql,
                "generated_rows": rows,
                "generated_execution_error": None,
                "result_match": correct,
                "generation": {
                    "tables_used": ["orders" if index < 2 else "customers"],
                    "self_reported_confidence": 0.8,
                },
            }
        )

    samples = build_samples(records, tmp_path, detector=StubDetector())
    assert len(samples) == 3
    assert samples[0]["agreement_ratio"] == 2 / 3
    assert samples[2]["agreement_ratio"] == 1 / 3
    assert samples[0]["back_translation_similarity"] == 0.75
    cached = json.loads((tmp_path / "backtranslation_cache.json").read_text())
    assert len(cached) == 2


def test_temperature_agreement_is_cached_per_question_and_strategy(monkeypatch, tmp_path):
    calls = []

    class StubAgreement:
        def evaluate(self, question, strategy):
            calls.append((question, strategy))
            return type("Result", (), {"agreement_ratio": 2 / 3})()

    monkeypatch.setattr(run_calibration, "MultiCandidateAgreement", StubAgreement)
    records = [
        {
            "question_id": "q1",
            "question": "Question one?",
            "strategy": "zero_shot",
            "result_match": True,
        },
        {
            "question_id": "q1",
            "question": "Question one?",
            "strategy": "schema_glossary",
            "result_match": False,
        },
        {
            "question_id": "q2",
            "question": "Unlabeled?",
            "strategy": "zero_shot",
            "result_match": None,
        },
    ]

    agreements = run_calibration.temperature_agreements(records, tmp_path)
    again = run_calibration.temperature_agreements(records, tmp_path)

    assert agreements == {"q1:zero_shot": 2 / 3, "q1:schema_glossary": 2 / 3}
    assert again == agreements
    assert calls == [("Question one?", "zero_shot"), ("Question one?", "schema_glossary")]
