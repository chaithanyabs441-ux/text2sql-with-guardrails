"""Build confidence calibration metrics from a saved evaluation run.

This uses cached varied-temperature SQL runs and one judge back-translation per
unique SQL. It writes train/validation/test metrics, thresholds, and an SVG.
"""

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pandas as pd
import sqlglot
from sqlglot import exp

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from t2s.verification.agreement import MultiCandidateAgreement  # noqa: E402
from t2s.verification.back_translation import HallucinationDetector  # noqa: E402
from t2s.verification.calibration import (  # noqa: E402
    FEATURE_NAMES,
    fit_and_evaluate,
    write_reliability_svg,
)
from t2s.verification.result_comparison import result_fingerprint  # noqa: E402
from t2s.verification.sanity_checks import ResultSanityChecker  # noqa: E402


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load non-empty JSON lines from the evaluation artifact."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _table_names(sql: str) -> set[str]:
    """Return unqualified table names referenced by a generated statement."""
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError:
        return set()
    return {table.name.lower() for table in tree.find_all(exp.Table) if table.name}


def _coverage(record: dict[str, Any]) -> float:
    """Measure whether the generation's declared tables cover its SQL tables."""
    references = _table_names(record.get("safe_sql") or "")
    if not references:
        return 1.0
    generation = record.get("generation") or {}
    declared = {str(name).split(".")[-1].lower() for name in generation.get("tables_used", [])}
    return len(references & declared) / len(references)


def build_samples(
    records: list[dict[str, Any]],
    output_dir: Path,
    detector: HallucinationDetector | None = None,
    agreement_by_question: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Derive labeled signal rows and cache judge translations by SQL text."""
    detector = detector or HallucinationDetector()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "backtranslation_cache.json"
    cache: dict[str, dict[str, Any]] = (
        json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    )
    print(f"Back-translation cache: {len(cache)} SQL statements already cached.", flush=True)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("question_id", "unknown"))].append(record)

    result_clusters: dict[str, Counter[tuple[int, tuple[str, ...]]]] = {}
    for question_id, candidates in grouped.items():
        fingerprints = Counter()
        for record in candidates:
            rows = record.get("generated_rows")
            if rows is not None and record.get("generated_execution_error") is None:
                fingerprints[result_fingerprint(pd.DataFrame(rows))] += 1
        result_clusters[question_id] = fingerprints
    sanity_checker = ResultSanityChecker()
    samples = []
    for record in records:
        if record.get("result_match") not in (True, False):
            continue
        sql = record.get("safe_sql")
        generated_rows = record.get("generated_rows")
        generation = record.get("generation") or {}
        if not sql or generated_rows is None or record.get("generated_execution_error"):
            continue
        if sql not in cache:
            print(
                f"Back-translating SQL {len(cache) + 1} (judge + embedding similarity).",
                flush=True,
            )
            translated = detector.back_translate(sql)
            similarity = detector.calculate_similarity(record["question"], translated)
            cache[sql] = {"question": translated, "similarity": similarity}
            cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        fingerprint = result_fingerprint(pd.DataFrame(generated_rows))
        clusters = result_clusters[str(record["question_id"])]
        question_id = str(record["question_id"])
        agreement_key = f"{question_id}:{record.get('strategy', 'schema_glossary')}"
        agreement = (
            agreement_by_question[agreement_key]
            if agreement_by_question is not None
            else clusters[fingerprint] / max(1, len(grouped[question_id]))
        )
        warnings = sanity_checker.check(pd.DataFrame(generated_rows))
        samples.append(
            {
                "question_id": str(record["question_id"]),
                "strategy": record.get("strategy"),
                "correct": bool(record["result_match"]),
                "back_translation_similarity": float(cache[sql]["similarity"]),
                "agreement_ratio": float(agreement),
                "sanity_warning_count": len(warnings),
                "self_reported_confidence": float(
                    generation.get("self_reported_confidence", 0.5)
                ),
                "schema_coverage": _coverage(record),
            }
        )
    return samples


def temperature_agreements(
    records: list[dict[str, Any]], output_dir: Path
) -> dict[str, float]:
    """Run and cache varied-temperature agreement per question and strategy."""
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "temperature_agreement_cache.json"
    cache: dict[str, dict[str, Any]] = (
        json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    )
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if record.get("result_match") not in (True, False):
            continue
        key = (str(record["question_id"]), str(record.get("strategy", "schema_glossary")))
        candidates.setdefault(key, record)
    evaluator = MultiCandidateAgreement()
    agreement_by_question = {}
    print(
        f"Temperature agreement: {len(cache)} results already cached; "
        f"{len(candidates)} question/strategy pairs total.",
        flush=True,
    )
    for index, ((question_id, strategy), record) in enumerate(candidates.items(), start=1):
        question = str(record["question"])
        cache_key = hashlib.sha256(f"{strategy}\0{question}".encode()).hexdigest()
        if cache_key not in cache:
            print(
                f"Generating and executing 3 agreement candidates ({index}/{len(candidates)}).",
                flush=True,
            )
            result = evaluator.evaluate(question, strategy=strategy)
            cache[cache_key] = {"agreement_ratio": result.agreement_ratio}
            cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        agreement_by_question[f"{question_id}:{strategy}"] = float(
            cache[cache_key]["agreement_ratio"]
        )
    return agreement_by_question


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, default=PROJECT_ROOT / "eval" / "results" / "raw_outputs.jsonl"
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=PROJECT_ROOT / "eval" / "results" / "calibration",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    records = _read_jsonl(args.input)
    agreements = temperature_agreements(records, args.output_dir)
    samples = build_samples(
        records, args.output_dir, agreement_by_question=agreements
    )
    if not samples:
        raise SystemExit("No comparable generated results found; run eval/run_eval.py first.")
    model, report, labels, probabilities = fit_and_evaluate(samples, seed=args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "samples.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in samples), encoding="utf-8"
    )
    report_data = {
        "features": list(FEATURE_NAMES),
        "signal_definitions": {
            "agreement_ratio": (
                "Dominant result set among 3 candidates generated at temperatures "
                "0.0, 0.3, and 0.7 for the row's prompting strategy; failed candidates "
                "count against agreement."
            ),
            "schema_coverage": (
                "Fraction of SQL-referenced tables listed in the generation's tables_used field."
            ),
        },
        **report.__dict__,
        "model": {
            "type": "logistic_regression",
            "intercept": model.intercept_.tolist(),
            "coefficients": model.coef_.tolist(),
        },
    }
    (args.output_dir / "report.json").write_text(
        json.dumps(report_data, indent=2), encoding="utf-8"
    )
    write_reliability_svg(args.output_dir / "reliability.svg", labels, probabilities)
    print(f"Calibration samples: {report.sample_count}")
    print(
        f"Question split: {report.train_question_count} train / "
        f"{report.test_question_count} test"
    )
    print(f"Combined signals: {report.calibrator}")
    print(f"Self-reported confidence: {report.self_reported_confidence}")
    print(f"Artifacts: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
