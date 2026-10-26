"""Train and evaluate a question-level confidence calibrator."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import train_test_split

FEATURE_NAMES = (
    "back_translation_similarity",
    "agreement_ratio",
    "sanity_warning_count",
    "self_reported_confidence",
    "schema_coverage",
)


@dataclass(frozen=True)
class CalibrationReport:
    """Held-out performance and split metadata for one calibrator run."""

    sample_count: int
    train_question_count: int
    validation_question_count: int
    test_question_count: int
    test_candidate_count: int
    calibrator: dict[str, float | None]
    self_reported_confidence: dict[str, float | None]
    reliability_bins: tuple[dict[str, float | int], ...]
    abstention_thresholds: dict[str, float]


def _ece(
    labels: np.ndarray, probabilities: np.ndarray, bins: int
) -> tuple[float, tuple[dict[str, float | int], ...]]:
    """Return expected calibration error and per-bin reliability data."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    records: list[dict[str, float | int]] = []
    total = len(labels)
    ece = 0.0
    for index in range(bins):
        in_bin = (probabilities >= edges[index]) & (
            probabilities <= edges[index + 1]
            if index == bins - 1
            else probabilities < edges[index + 1]
        )
        count = int(in_bin.sum())
        if not count:
            continue
        confidence = float(probabilities[in_bin].mean())
        accuracy = float(labels[in_bin].mean())
        ece += count / total * abs(accuracy - confidence)
        records.append({"count": count, "confidence": confidence, "accuracy": accuracy})
    return ece, tuple(records)


def _metrics(labels: np.ndarray, probabilities: np.ndarray) -> dict[str, float | None]:
    """Calculate ranking and probability calibration metrics."""
    auc = float(roc_auc_score(labels, probabilities)) if len(np.unique(labels)) == 2 else None
    ece, _ = _ece(labels, probabilities, bins=10)
    return {
        "auroc": auc,
        "brier_score": float(brier_score_loss(labels, probabilities)),
        "ece": ece,
    }


def tune_abstention_thresholds(
    labels: np.ndarray,
    probabilities: np.ndarray,
    answer_error_target: float = 0.10,
    warn_error_target: float = 0.40,
) -> dict[str, float]:
    """Choose validation thresholds that bound observed error in each band."""
    if len(labels) != len(probabilities) or not len(labels):
        raise ValueError("Threshold tuning requires equally sized non-empty arrays")
    if not 0 <= answer_error_target <= warn_error_target <= 1:
        raise ValueError("Error targets must satisfy 0 <= answer <= warn <= 1")

    def minimum_threshold(target: float) -> float:
        for threshold in sorted(set(float(value) for value in probabilities)):
            selected = probabilities >= threshold
            if selected.any() and float(1 - labels[selected].mean()) <= target:
                return threshold
        return 1.01

    answer_threshold = minimum_threshold(answer_error_target)
    warn_threshold = minimum_threshold(warn_error_target)
    if warn_threshold > answer_threshold:
        warn_threshold = answer_threshold
    return {"answer_threshold": answer_threshold, "warn_threshold": warn_threshold}


def fit_and_evaluate(
    rows: list[dict[str, Any]], seed: int = 42, test_size: float = 0.25
) -> tuple[LogisticRegression, CalibrationReport, np.ndarray, np.ndarray]:
    """Fit signals and self-confidence baseline, splitting by question ID.

    Rows are candidate outcomes with ``question_id``, ``correct`` and all
    ``FEATURE_NAMES``. Every candidate for one question stays in one split.
    """
    eligible = [
        row
        for row in rows
        if row.get("question_id") is not None
        and row.get("correct") in (True, False, 0, 1)
        and all(row.get(name) is not None for name in FEATURE_NAMES)
    ]
    if len(eligible) < 8:
        raise ValueError("Need at least 8 labeled candidates with complete signals")
    labels = np.asarray([int(row["correct"]) for row in eligible], dtype=int)
    if len(np.unique(labels)) < 2:
        raise ValueError("Calibration requires both correct and incorrect examples")

    question_labels: dict[str, int] = {}
    for row in eligible:
        key = str(row["question_id"])
        question_labels[key] = max(question_labels.get(key, 0), int(row["correct"]))
    question_ids = np.asarray(list(question_labels))
    strata = np.asarray([question_labels[key] for key in question_ids])
    if len(question_ids) < 4 or len(np.unique(strata)) < 2:
        raise ValueError("Need at least 4 questions with both outcome classes")
    train_validation_ids, test_ids = train_test_split(
        question_ids,
        test_size=test_size,
        random_state=seed,
        stratify=strata,
    )
    train_validation_strata = np.asarray(
        [question_labels[key] for key in train_validation_ids]
    )
    train_ids, validation_ids = train_test_split(
        train_validation_ids,
        test_size=0.25,
        random_state=seed,
        stratify=train_validation_strata,
    )
    train_set = set(train_ids)
    validation_set = set(validation_ids)
    test_set = set(test_ids)
    train_rows = [row for row in eligible if str(row["question_id"]) in train_set]
    test_rows = [row for row in eligible if str(row["question_id"]) in test_set]
    validation_rows = [
        row for row in eligible if str(row["question_id"]) in validation_set
    ]
    if len({int(row["correct"]) for row in train_rows}) < 2:
        raise ValueError("Training question split must contain both outcome classes")
    y_train = np.asarray([int(row["correct"]) for row in train_rows])
    y_test = np.asarray([int(row["correct"]) for row in test_rows])
    x_train = np.asarray([[float(row[name]) for name in FEATURE_NAMES] for row in train_rows])
    x_test = np.asarray([[float(row[name]) for name in FEATURE_NAMES] for row in test_rows])
    y_validation = np.asarray([int(row["correct"]) for row in validation_rows])
    x_validation = np.asarray(
        [[float(row[name]) for name in FEATURE_NAMES] for row in validation_rows]
    )
    baseline = np.clip(
        np.asarray([float(row["self_reported_confidence"]) for row in test_rows]),
        0.0,
        1.0,
    )

    model = LogisticRegression(max_iter=1000, random_state=seed)
    model.fit(x_train, y_train)
    validation_probabilities = model.predict_proba(x_validation)[:, 1]
    thresholds = tune_abstention_thresholds(y_validation, validation_probabilities)
    probabilities = model.predict_proba(x_test)[:, 1]
    ece, reliability = _ece(y_test, probabilities, bins=10)
    calibrator_metrics = _metrics(y_test, probabilities)
    calibrator_metrics["ece"] = ece
    report = CalibrationReport(
        sample_count=len(eligible),
        train_question_count=len(train_set),
        validation_question_count=len(validation_set),
        test_question_count=len(test_set),
        test_candidate_count=len(test_rows),
        calibrator=calibrator_metrics,
        self_reported_confidence=_metrics(y_test, baseline),
        reliability_bins=reliability,
        abstention_thresholds=thresholds,
    )
    return model, report, y_test, probabilities


def write_reliability_svg(
    path: Path, labels: np.ndarray, probabilities: np.ndarray, bins: int = 10
) -> None:
    """Write a dependency-free SVG reliability diagram for held-out data."""
    _, records = _ece(labels, probabilities, bins)
    width, height, margin = 520, 440, 55
    plot = width - 2 * margin
    marks = [
        f'<line x1="{margin}" y1="{margin}" x2="{margin}" y2="{height-margin}" stroke="#333"/>',
        (
            f'<line x1="{margin}" y1="{height-margin}" x2="{width-margin}" '
            f'y2="{height-margin}" stroke="#333"/>'
        ),
    ]
    for step in range(1, 5):
        coord = margin + plot * step / 5
        marks.append(
            f'<line x1="{coord:.1f}" y1="{margin}" x2="{coord:.1f}" '
            f'y2="{height-margin}" stroke="#ddd"/>'
        )
        marks.append(
            f'<line x1="{margin}" y1="{height-margin-plot*step/5:.1f}" '
            f'x2="{width-margin}" y2="{height-margin-plot*step/5:.1f}" stroke="#ddd"/>'
        )
    marks.append(
        f'<line x1="{margin}" y1="{height-margin}" x2="{width-margin}" '
        f'y2="{margin}" stroke="#777" stroke-dasharray="5,5"/>'
    )
    for record in records:
        x = margin + float(record["confidence"]) * plot
        y = height - margin - float(record["accuracy"]) * plot
        marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="#1769aa"/>')
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="white"/>'
        + "".join(marks)
        + (
            f'<text x="{width/2}" y="{height-10}" text-anchor="middle">'
            "Mean predicted confidence</text>"
        )
        + (
            f'<text x="18" y="{height/2}" text-anchor="middle" '
            f'transform="rotate(-90 18 {height/2})">Observed accuracy</text>'
        )
        + "</svg>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")
