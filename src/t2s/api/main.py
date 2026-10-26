"""FastAPI application for guarded natural-language SQL queries."""

import json
import logging
import math
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal

import sqlglot
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlglot import exp

from t2s.generation.generator import SQLGenerator
from t2s.guardrails.exceptions import UnsafeQueryError
from t2s.sandbox.executor import ExecutionSandbox
from t2s.schema.models import DatabaseSchema
from t2s.verification.abstention import AbstentionThresholds, choose_decision
from t2s.verification.agreement import MultiCandidateAgreement
from t2s.verification.back_translation import HallucinationDetector
from t2s.verification.sanity_checks import ResultSanityChecker

logger = logging.getLogger(__name__)

app = FastAPI(title="Trustworthy Text-to-SQL API")

# These components no longer create LLM or embedding clients during import.
generator = SQLGenerator()
detector = HallucinationDetector()
sandbox = ExecutionSandbox()
checker = ResultSanityChecker()
agreement = MultiCandidateAgreement(generator=generator, sandbox=sandbox)

HISTORY_MAX_ITEMS = 100
SELF_CORRECT_MAX_RETRIES = 2
CALIBRATION_REPORT = (
    Path(__file__).resolve().parents[3] / "eval" / "results" / "calibration" / "report.json"
)
FALLBACK_THRESHOLDS = AbstentionThresholds(answer_threshold=0.85, warn_threshold=0.50)
_history: deque["QueryHistoryEntry"] = deque(maxlen=HISTORY_MAX_ITEMS)
_history_lock = Lock()


@app.get("/health")
def health_check() -> dict[str, str]:
    """Report that the API process is ready to accept requests."""
    return {"status": "ok"}


class QueryRequest(BaseModel):
    """A natural-language question to translate and execute."""

    question: str
    strategy: Literal[
        "zero_shot", "schema_glossary", "schema_glossary_selfcorrect"
    ] = "schema_glossary"


class QueryResponse(BaseModel):
    """Generated SQL, execution results, and verification signals."""

    sql: str
    explanation: str
    data: list[Any]
    warnings: list[str]
    confidence_score: float
    back_translated_question: str
    confidence_signals: dict[str, float]
    decision: Literal["answer", "warn", "abstain_with_alternatives"]
    alternatives: list[str]
    calibration_ready: bool


class QueryHistoryEntry(BaseModel):
    """Metadata for a recent successful request; results are not retained."""

    question: str
    strategy: str
    sql: str
    created_at: datetime
    warnings: list[str]
    confidence_score: float


@app.post("/v1/query", response_model=QueryResponse)
def process_query(req: QueryRequest) -> QueryResponse:
    """Generate, validate, execute, and verify SQL for one question."""
    try:
        response, safe_sql = generator.generate(req.question, strategy=req.strategy)

        if not response.answerable:
            raise HTTPException(
                status_code=400,
                detail="Question is unanswerable with the current schema.",
            )

        df, _latency_ms, error = sandbox.execute(safe_sql)
        retry_count = 0
        while (
            error
            and req.strategy == "schema_glossary_selfcorrect"
            and retry_count < SELF_CORRECT_MAX_RETRIES
        ):
            retry_count += 1
            response, safe_sql = generator.generate(
                req.question,
                strategy=req.strategy,
                database_error=error[:2000],
            )
            if not response.answerable:
                raise HTTPException(
                    status_code=400,
                    detail="Question is unanswerable with the current schema.",
                )
            df, _latency_ms, error = sandbox.execute(safe_sql)

        warnings = checker.check(df, error)
        back_translated_question = detector.back_translate(safe_sql)
        back_translation_similarity = detector.calculate_similarity(
            req.question, back_translated_question
        )
        agreement_result = agreement.evaluate(req.question, strategy=req.strategy)
        agreement_ratio = agreement_result.agreement_ratio
        schema_coverage = _schema_coverage(safe_sql, response.tables_used)
        confidence_signals = {
            "back_translation_similarity": back_translation_similarity,
            "agreement_ratio": agreement_ratio,
            "sanity_warning_count": float(len(warnings)),
            "self_reported_confidence": response.self_reported_confidence,
            "schema_coverage": schema_coverage,
        }
        confidence_score, thresholds, calibration_ready = _calibrated_confidence(
            confidence_signals
        )
        decision = choose_decision(
            confidence_score,
            thresholds,
            warning_count=len(warnings),
            agreement_ratio=agreement_ratio,
        )
        alternatives = list(
            dict.fromkeys(
                candidate.safe_sql
                for candidate in agreement_result.candidates
                if candidate.status == "executed"
                and candidate.safe_sql
                and candidate.safe_sql != safe_sql
            )
        )
        data_records = df.to_dict(orient="records") if not df.empty else []
        if decision == "abstain_with_alternatives":
            safe_sql = ""
            data_records = []

        result = QueryResponse(
            sql=safe_sql,
            explanation=response.explanation,
            data=data_records,
            warnings=warnings,
            confidence_score=confidence_score,
            back_translated_question=back_translated_question,
            confidence_signals=confidence_signals,
            decision=decision,
            alternatives=alternatives,
            calibration_ready=calibration_ready,
        )
        entry = QueryHistoryEntry(
            question=req.question,
            strategy=req.strategy,
            sql=safe_sql,
            created_at=datetime.now(UTC),
            warnings=warnings,
            confidence_score=confidence_score,
        )
        if decision != "abstain_with_alternatives":
            with _history_lock:
                _history.append(entry)
        return result
    except HTTPException:
        raise
    except UnsafeQueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        # Lazy LLM clients report missing provider credentials when first used.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Query processing failed")
        raise HTTPException(
            status_code=500,
            detail="The query could not be processed.",
        ) from exc


@app.get("/v1/schema", response_model=DatabaseSchema)
def get_schema() -> DatabaseSchema:
    """Return the business schema used to ground SQL generation."""
    return generator.get_schema()


@app.get("/v1/history", response_model=list[QueryHistoryEntry])
def get_history() -> list[QueryHistoryEntry]:
    """Return recent successful query metadata, newest first."""
    with _history_lock:
        return list(reversed(_history))


def _schema_coverage(sql: str, declared_tables: list[str]) -> float:
    """Return the fraction of SQL table references reported by the generator."""
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.ParseError:
        return 0.0
    references = {table.name.lower() for table in tree.find_all(exp.Table) if table.name}
    if not references:
        return 1.0
    declared = {name.split(".")[-1].lower() for name in declared_tables}
    return len(references & declared) / len(references)


def _calibrated_confidence(
    signals: dict[str, float],
) -> tuple[float, AbstentionThresholds, bool]:
    """Score signals from the saved logistic model or use a labeled fallback."""
    fallback = (signals["self_reported_confidence"], FALLBACK_THRESHOLDS, False)
    try:
        report = json.loads(CALIBRATION_REPORT.read_text(encoding="utf-8"))
        features = report["features"]
        coefficients = report["model"]["coefficients"][0]
        intercept = report["model"]["intercept"][0]
        logit = intercept + sum(
            coefficient * signals[name]
            for name, coefficient in zip(features, coefficients, strict=True)
        )
        score = 1 / (1 + math.exp(-max(-30.0, min(30.0, logit))))
        threshold_data = report.get("abstention_thresholds")
        if threshold_data is None:
            return score, FALLBACK_THRESHOLDS, False
        thresholds = AbstentionThresholds(**threshold_data)
        return score, thresholds, True
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return fallback
