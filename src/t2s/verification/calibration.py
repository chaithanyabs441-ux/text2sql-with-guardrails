"""Question-grouped cross-validation and selective-prediction calibration."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import StratifiedGroupKFold

FEATURE_NAMES = (
    "back_translation_similarity",
    "agreement_ratio",
    "sanity_warning_count",
    "self_reported_confidence",
    "schema_coverage",
)
BOOTSTRAP_METRICS = ("auroc", "brier_score", "ece")
DEFAULT_SEEDS = (42, 43, 44)
DEFAULT_FOLDS = 5


@dataclass(frozen=True)
class CalibrationReport:
    """Cross-validated performance, uncertainty, and abstention details."""

    sample_count: int
    question_count: int
    fold_count: int
    repeat_count: int
    seeds: tuple[int, ...]
    calibrator: dict[str, float | None]
    self_reported_confidence: dict[str, float | None]
    bootstrap_intervals: dict[str, dict[str, dict[str, float | int | None]]]
    reliability_bins: tuple[dict[str, float | int], ...]
    abstention_thresholds: dict[str, float]
    abstention_diagnostics: dict[str, Any]
    decision_level_metrics: dict[str, dict[str, float | int | None]]
    signal_ablation: tuple[dict[str, Any], ...]
    selective_prediction: tuple[dict[str, float], ...]


def _ece(
    labels: np.ndarray, probabilities: np.ndarray, bins: int = 10
) -> tuple[float, tuple[dict[str, float | int], ...]]:
    """Return expected calibration error and per-bin reliability data."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    records: list[dict[str, float | int]] = []
    total = len(labels)
    if not total:
        return 0.0, ()
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
    ece, _ = _ece(labels, probabilities)
    return {
        "auroc": auc,
        "brier_score": float(brier_score_loss(labels, probabilities)),
        "ece": ece,
    }


def _cross_validated_probabilities(
    features: np.ndarray,
    labels: np.ndarray,
    groups: np.ndarray,
    seeds: tuple[int, ...],
    fold_count: int,
) -> np.ndarray:
    """Produce repeated out-of-fold probabilities without splitting a question."""
    unique_groups = np.unique(groups)
    if len(unique_groups) < fold_count:
        raise ValueError(
            f"Need at least {fold_count} distinct questions for grouped cross-validation"
        )

    probability_sums = np.zeros(len(labels), dtype=float)
    prediction_counts = np.zeros(len(labels), dtype=int)
    for seed in seeds:
        splitter = StratifiedGroupKFold(
            n_splits=fold_count, shuffle=True, random_state=seed
        )
        for train_indices, test_indices in splitter.split(features, labels, groups):
            model = LogisticRegression(max_iter=1000, random_state=seed)
            model.fit(features[train_indices], labels[train_indices])
            probability_sums[test_indices] += model.predict_proba(
                features[test_indices]
            )[:, 1]
            prediction_counts[test_indices] += 1

    if np.any(prediction_counts != len(seeds)):
        raise RuntimeError("Cross-validation did not produce one prediction per seed and row")
    return probability_sums / prediction_counts


def _bootstrap_intervals(
    labels: np.ndarray,
    probabilities: np.ndarray,
    groups: np.ndarray,
    *,
    samples: int,
    seed: int,
) -> dict[str, dict[str, float | int | None]]:
    """Bootstrap metric intervals by resampling whole questions."""
    if samples < 1:
        raise ValueError("Bootstrap sample count must be positive")
    rng = np.random.default_rng(seed)
    group_indices = {
        group: np.flatnonzero(groups == group) for group in np.unique(groups)
    }
    unique_groups = np.asarray(list(group_indices))
    values = {name: [] for name in BOOTSTRAP_METRICS}
    for _ in range(samples):
        selected_groups = rng.choice(unique_groups, size=len(unique_groups), replace=True)
        selected_indices = np.concatenate(
            [group_indices[group] for group in selected_groups]
        )
        replicate = _metrics(labels[selected_indices], probabilities[selected_indices])
        for name in BOOTSTRAP_METRICS:
            if replicate[name] is not None:
                values[name].append(float(replicate[name]))

    intervals: dict[str, dict[str, float | int | None]] = {}
    for name, metric_values in values.items():
        if metric_values:
            lower, upper = np.percentile(metric_values, [2.5, 97.5])
            intervals[name] = {
                "lower": float(lower),
                "upper": float(upper),
                "valid_bootstrap_replicates": len(metric_values),
            }
        else:
            intervals[name] = {
                "lower": None,
                "upper": None,
                "valid_bootstrap_replicates": 0,
            }
    return intervals


def _select_threshold(
    labels: np.ndarray,
    probabilities: np.ndarray,
    target_precision: float,
    minimum_coverage: float,
) -> tuple[float, dict[str, float | bool]]:
    """Select the lowest threshold meeting precision and minimum coverage."""
    candidates = sorted(set(float(value) for value in probabilities))
    eligible: list[tuple[float, float, float]] = []
    fallback: list[tuple[float, float, float]] = []
    for threshold in candidates:
        selected = probabilities >= threshold
        count = int(selected.sum())
        if not count:
            continue
        coverage = count / len(labels)
        precision = float(labels[selected].mean())
        if coverage >= minimum_coverage:
            fallback.append((precision, coverage, threshold))
            if precision >= target_precision:
                eligible.append((precision, coverage, threshold))

    target_reached = bool(eligible)
    if eligible:
        # Highest coverage wins; for ties, prefer precision and then the lower cutoff.
        precision, coverage, threshold = max(
            eligible, key=lambda item: (item[1], item[0], -item[2])
        )
    elif fallback:
        # Report the best observed option when the precision target is unreachable.
        precision, coverage, threshold = max(
            fallback, key=lambda item: (item[0], item[1], item[2])
        )
    else:
        threshold = float(np.max(probabilities))
        selected = probabilities >= threshold
        coverage = float(selected.mean())
        precision = float(labels[selected].mean())

    threshold = min(1.0, max(0.0, threshold))
    return threshold, {
        "target_precision": target_precision,
        "minimum_coverage": minimum_coverage,
        "target_reached": target_reached,
        "observed_precision": precision,
        "observed_coverage": coverage,
    }


def _select_warning_band_threshold(
    labels: np.ndarray,
    probabilities: np.ndarray,
    answer_threshold: float,
    target_precision: float,
    minimum_coverage: float,
) -> tuple[float, dict[str, float | bool]]:
    """Select a warn cutoff using only the band below the answer cutoff."""
    candidates = sorted(
        set(float(value) for value in probabilities if value < answer_threshold)
    )
    eligible: list[tuple[float, float, float]] = []
    fallback: list[tuple[float, float, float]] = []
    for threshold in candidates:
        selected = (probabilities >= threshold) & (probabilities < answer_threshold)
        count = int(selected.sum())
        if not count:
            continue
        coverage = count / len(labels)
        precision = float(labels[selected].mean())
        if coverage >= minimum_coverage:
            fallback.append((precision, coverage, threshold))
            if precision >= target_precision:
                eligible.append((precision, coverage, threshold))

    target_reached = bool(eligible)
    if eligible:
        precision, coverage, threshold = max(
            eligible, key=lambda item: (item[1], item[0], -item[2])
        )
    elif fallback:
        precision, coverage, threshold = max(
            fallback, key=lambda item: (item[0], item[1], -item[2])
        )
    else:
        threshold = answer_threshold
        selected = (probabilities >= threshold) & (probabilities < answer_threshold)
        coverage = float(selected.mean())
        precision = None

    return threshold, {
        "target_precision": target_precision,
        "minimum_coverage": minimum_coverage,
        "target_reached": target_reached,
        "observed_precision": precision,
        "observed_coverage": coverage,
    }


def tune_abstention_thresholds(
    labels: np.ndarray,
    probabilities: np.ndarray,
    answer_error_target: float = 0.10,
    warn_error_target: float = 0.40,
    minimum_coverage: float = 0.10,
) -> dict[str, float]:
    """Choose reachable answer/warn cutoffs from out-of-fold predictions."""
    if len(labels) != len(probabilities) or not len(labels):
        raise ValueError("Threshold tuning requires equally sized non-empty arrays")
    if not 0 <= answer_error_target <= warn_error_target <= 1:
        raise ValueError("Error targets must satisfy 0 <= answer <= warn <= 1")
    if not 0 < minimum_coverage <= 1:
        raise ValueError("minimum_coverage must be in (0, 1]")
    if np.any((probabilities < 0) | (probabilities > 1)):
        raise ValueError("Probabilities must be between 0 and 1")

    answer_threshold, _ = _select_threshold(
        labels, probabilities, 1.0 - answer_error_target, minimum_coverage
    )
    warn_threshold, _ = _select_warning_band_threshold(
        labels,
        probabilities,
        answer_threshold,
        1.0 - warn_error_target,
        minimum_coverage,
    )
    return {
        "answer_threshold": answer_threshold,
        "warn_threshold": warn_threshold,
    }


def _decision_metrics(
    labels: np.ndarray, probabilities: np.ndarray, thresholds: dict[str, float]
) -> dict[str, dict[str, float | int | None]]:
    """Summarize precision and share of samples in each decision band."""
    answer_threshold = thresholds["answer_threshold"]
    warn_threshold = thresholds["warn_threshold"]
    masks = {
        "answer": probabilities >= answer_threshold,
        "warn": (probabilities >= warn_threshold) & (probabilities < answer_threshold),
        "abstain": probabilities < warn_threshold,
    }
    result = {}
    for name, selected in masks.items():
        count = int(selected.sum())
        result[name] = {
            "count": count,
            "coverage": count / len(labels),
            "precision": float(labels[selected].mean()) if count else None,
        }
    return result


def _selective_curve(
    labels: np.ndarray, probabilities: np.ndarray, points: int = 21
) -> tuple[dict[str, float], ...]:
    """Return accuracy at increasing score cutoffs with observed coverage."""
    quantiles = np.quantile(probabilities, np.linspace(0.0, 1.0, points))
    thresholds = sorted(set(float(value) for value in quantiles), reverse=True)
    curve = []
    for threshold in thresholds:
        selected = probabilities >= threshold
        curve.append(
            {
                "threshold": threshold,
                "coverage": float(selected.mean()),
                "accuracy": float(labels[selected].mean()),
            }
        )
    return tuple(curve)


def _ablation_configs() -> tuple[tuple[str, tuple[int, ...]], ...]:
    """Return all single-signal, leave-one-out, and full-signal configurations."""
    all_indices = tuple(range(len(FEATURE_NAMES)))
    configurations = [
        (f"single:{FEATURE_NAMES[index]}", (index,))
        for index in all_indices
    ]
    configurations.extend(
        (
            f"leave_one_out:{FEATURE_NAMES[index]}",
            tuple(candidate for candidate in all_indices if candidate != index),
        )
        for index in all_indices
    )
    configurations.append(("all_signals", all_indices))
    return tuple(configurations)


def fit_and_evaluate(
    rows: list[dict[str, Any]],
    seed: int = 42,
    *,
    repeat_count: int = 3,
    fold_count: int = DEFAULT_FOLDS,
    bootstrap_samples: int = 1000,
    answer_target_precision: float = 0.90,
    warn_target_precision: float = 0.60,
    minimum_coverage: float = 0.10,
) -> tuple[LogisticRegression, CalibrationReport, np.ndarray, np.ndarray]:
    """Calibrate both signals and self-confidence with grouped repeated CV.

    Every candidate for one question stays in the same fold. Both the combined
    logistic model and the one-feature self-confidence baseline use the same
    out-of-fold partitions and Platt-style logistic calibration.
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
    groups = np.asarray([str(row["question_id"]) for row in eligible])
    question_count = len(np.unique(groups))
    if fold_count < 2 or repeat_count < 1:
        raise ValueError("Cross-validation requires at least 2 folds and 1 repeat")
    seeds = tuple(seed + index for index in range(repeat_count))

    all_features = np.asarray(
        [[float(row[name]) for name in FEATURE_NAMES] for row in eligible],
        dtype=float,
    )
    baseline_index = FEATURE_NAMES.index("self_reported_confidence")
    baseline_features = all_features[:, [baseline_index]]
    if not np.isfinite(all_features).all():
        raise ValueError("Calibration features must be finite")

    probabilities = _cross_validated_probabilities(
        all_features, labels, groups, seeds, fold_count
    )
    baseline_probabilities = _cross_validated_probabilities(
        baseline_features, labels, groups, seeds, fold_count
    )
    thresholds = tune_abstention_thresholds(
        labels,
        probabilities,
        answer_error_target=1.0 - answer_target_precision,
        warn_error_target=1.0 - warn_target_precision,
        minimum_coverage=minimum_coverage,
    )
    _, answer_diagnostics = _select_threshold(
        labels, probabilities, answer_target_precision, minimum_coverage
    )
    _, warn_diagnostics = _select_warning_band_threshold(
        labels,
        probabilities,
        thresholds["answer_threshold"],
        warn_target_precision,
        minimum_coverage,
    )
    answer_diagnostics["selected_threshold"] = thresholds["answer_threshold"]
    warn_diagnostics["selected_threshold"] = thresholds["warn_threshold"]

    bootstrap = {
        "combined": _bootstrap_intervals(
            labels,
            probabilities,
            groups,
            samples=bootstrap_samples,
            seed=seed + 10_000,
        ),
        "self_reported_confidence_platt": _bootstrap_intervals(
            labels,
            baseline_probabilities,
            groups,
            samples=bootstrap_samples,
            seed=seed + 20_000,
        ),
    }
    combined_metrics = _metrics(labels, probabilities)
    baseline_metrics = _metrics(labels, baseline_probabilities)
    _, reliability = _ece(labels, probabilities)

    ablation_results = []
    for name, columns in _ablation_configs():
        variant_probabilities = _cross_validated_probabilities(
            all_features[:, columns], labels, groups, seeds, fold_count
        )
        ablation_results.append(
            {
                "name": name,
                "signals": [FEATURE_NAMES[index] for index in columns],
                "metrics": _metrics(labels, variant_probabilities),
            }
        )

    final_model = LogisticRegression(max_iter=1000, random_state=seed)
    final_model.fit(all_features, labels)
    report = CalibrationReport(
        sample_count=len(eligible),
        question_count=question_count,
        fold_count=fold_count,
        repeat_count=repeat_count,
        seeds=seeds,
        calibrator=combined_metrics,
        self_reported_confidence=baseline_metrics,
        bootstrap_intervals=bootstrap,
        reliability_bins=reliability,
        abstention_thresholds=thresholds,
        abstention_diagnostics={
            "selection_source": "repeated_grouped_out_of_fold_predictions",
            "answer": answer_diagnostics,
            "warn": warn_diagnostics,
            "target_unreachable": not (
                bool(answer_diagnostics["target_reached"])
                and bool(warn_diagnostics["target_reached"])
            ),
        },
        decision_level_metrics=_decision_metrics(labels, probabilities, thresholds),
        signal_ablation=tuple(ablation_results),
        selective_prediction=_selective_curve(labels, probabilities),
    )
    return final_model, report, labels, probabilities


def write_reliability_svg(
    path: Path, labels: np.ndarray, probabilities: np.ndarray, bins: int = 10
) -> None:
    """Write a dependency-free SVG reliability diagram."""
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
        + f'<text x="{width/2}" y="{height-10}" text-anchor="middle">'
        + "Mean predicted confidence</text>"
        + f'<text x="18" y="{height/2}" text-anchor="middle" '
        + f'transform="rotate(-90 18 {height/2})">Observed accuracy</text></svg>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")


def write_selective_svg(path: Path, curve: tuple[dict[str, float], ...]) -> None:
    """Write an accuracy-versus-coverage SVG selective-prediction curve."""
    width, height, margin = 600, 440, 55
    plot_width = width - 2 * margin
    plot_height = height - 2 * margin
    points = " ".join(
        f"{margin + item['coverage'] * plot_width:.1f},"
        f"{height - margin - item['accuracy'] * plot_height:.1f}"
        for item in curve
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="white"/>'
        f'<line x1="{margin}" y1="{margin}" x2="{margin}" y2="{height-margin}" stroke="#333"/>'
        f'<line x1="{margin}" y1="{height-margin}" x2="{width-margin}" '
        f'y2="{height-margin}" stroke="#333"/>'
        f'<polyline points="{points}" fill="none" stroke="#1769aa" stroke-width="3"/>'
        f'<text x="{width/2}" y="{height-10}" text-anchor="middle">Coverage</text>'
        f'<text x="18" y="{height/2}" text-anchor="middle" '
        f'transform="rotate(-90 18 {height/2})">Accuracy</text></svg>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")


def write_ablation_svg(path: Path, ablation: tuple[dict[str, Any], ...]) -> None:
    """Write a dependency-free comparison of cross-validated ablation AUROCs."""
    width, row_height, margin = 760, 30, 220
    height = 100 + row_height * len(ablation)
    bars = []
    for index, item in enumerate(ablation):
        y = 45 + index * row_height
        auc = item["metrics"]["auroc"]
        label = item["name"]
        bars.append(
            f'<text x="{margin-8}" y="{y+15}" text-anchor="end" font-size="11">{label}</text>'
        )
        if auc is not None:
            bar_width = max(0.0, min(1.0, float(auc))) * (width - margin - 35)
            bars.append(
                f'<rect x="{margin}" y="{y}" width="{bar_width:.1f}" height="18" '
                'fill="#1769aa"/>'
            )
            bars.append(
                f'<text x="{margin+bar_width+5:.1f}" y="{y+14}" font-size="11">{auc:.3f}</text>'
            )
        else:
            bars.append(
                f'<text x="{margin}" y="{y+14}" font-size="11">AUROC unavailable</text>'
            )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="white"/>'
        + "".join(bars)
        + "</svg>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")
