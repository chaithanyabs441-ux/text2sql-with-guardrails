"""Generate SQL candidates and measure agreement over executed result sets."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from t2s.generation.generator import SQLGenerator
from t2s.guardrails.policy import SQLPolicyEngine
from t2s.sandbox.executor import ExecutionSandbox
from t2s.schema.models import DatabaseSchema
from t2s.verification.result_comparison import result_sets_equal

DEFAULT_TEMPERATURES = (0.0, 0.3, 0.7)


@dataclass(frozen=True)
class CandidateOutcome:
    """Outcome metadata for one generated and executed candidate."""

    temperature: float
    status: str
    safe_sql: str | None = None
    row_count: int | None = None
    execution_latency_ms: float | None = None
    error: str | None = None
    cluster_id: int | None = None


@dataclass(frozen=True)
class AgreementResult:
    """Agreement ratio and per-candidate outcomes for one question."""

    candidate_count: int
    executed_count: int
    dominant_cluster_size: int
    agreement_ratio: float
    candidates: tuple[CandidateOutcome, ...]


class MultiCandidateAgreement:
    """Execute several safe SQL candidates and cluster identical result sets."""

    def __init__(
        self,
        generator: Any | None = None,
        sandbox: Any | None = None,
        policy: SQLPolicyEngine | None = None,
        temperatures: Sequence[float] = DEFAULT_TEMPERATURES,
    ) -> None:
        if not 3 <= len(temperatures) <= 5:
            raise ValueError("Agreement requires between 3 and 5 temperatures")
        if any(temperature < 0 or temperature > 2 for temperature in temperatures):
            raise ValueError("Temperatures must be between 0 and 2")
        if len(set(temperatures)) < 3:
            raise ValueError("Agreement temperatures must include at least 3 values")
        self.generator = generator or SQLGenerator()
        self.sandbox = sandbox or ExecutionSandbox()
        self.policy = policy or SQLPolicyEngine()
        self.temperatures = tuple(temperatures)

    def evaluate(
        self,
        question: str,
        strategy: str = "schema_glossary",
        database_error: str | None = None,
        sql_authorizer: Callable[[str], None] | None = None,
        schema_override: DatabaseSchema | None = None,
        policy: SQLPolicyEngine | None = None,
        schema_name: str = "core",
        generic_dataset: bool = False,
    ) -> AgreementResult:
        """Generate, policy-check, and sandbox-execute every candidate."""
        outcomes: list[CandidateOutcome] = []
        cluster_frames = []
        cluster_members: list[list[int]] = []

        for index, temperature in enumerate(self.temperatures):
            try:
                generation_options: dict[str, Any] = {}
                if schema_override is not None:
                    generation_options["schema_override"] = schema_override
                if policy is not None:
                    generation_options["policy_engine"] = policy
                if generic_dataset:
                    generation_options["generic_dataset"] = True
                response, generated_sql = self.generator.generate(
                    question,
                    strategy=strategy,
                    database_error=database_error,
                    temperature=temperature,
                    **generation_options,
                )
                if not response.answerable:
                    outcomes.append(CandidateOutcome(temperature, "unanswerable"))
                    continue
                if not generated_sql:
                    outcomes.append(CandidateOutcome(temperature, "no_sql"))
                    continue
                safe_sql = (policy or self.policy).validate(generated_sql)
                if sql_authorizer is not None:
                    sql_authorizer(safe_sql)
            except Exception as exc:
                outcomes.append(
                    CandidateOutcome(temperature, "generation_or_policy_error", error=str(exc))
                )
                continue

            if schema_name == "core":
                frame, latency_ms, error = self.sandbox.execute(safe_sql)
            else:
                frame, latency_ms, error = self.sandbox.execute(
                    safe_sql, schema_name=schema_name
                )
            if error:
                outcomes.append(
                    CandidateOutcome(
                        temperature,
                        "execution_error",
                        safe_sql=safe_sql,
                        execution_latency_ms=latency_ms,
                        error=error,
                    )
                )
                continue

            cluster_id = next(
                (
                    candidate_cluster
                    for candidate_cluster, representative in enumerate(cluster_frames)
                    if result_sets_equal(representative, frame)
                    and result_sets_equal(frame, representative)
                ),
                None,
            )
            if cluster_id is None:
                cluster_id = len(cluster_frames)
                cluster_frames.append(frame)
                cluster_members.append([])
            cluster_members[cluster_id].append(index)
            outcomes.append(
                CandidateOutcome(
                    temperature,
                    "executed",
                    safe_sql=safe_sql,
                    row_count=len(frame),
                    execution_latency_ms=latency_ms,
                )
            )

        cluster_by_candidate = {
            index: cluster_id
            for cluster_id, members in enumerate(cluster_members, start=1)
            for index in members
        }
        outcomes = [
            replace(outcome, cluster_id=cluster_by_candidate[index])
            if index in cluster_by_candidate
            else outcome
            for index, outcome in enumerate(outcomes)
        ]
        dominant_size = max((len(members) for members in cluster_members), default=0)
        return AgreementResult(
            candidate_count=len(self.temperatures),
            executed_count=sum(outcome.status == "executed" for outcome in outcomes),
            dominant_cluster_size=dominant_size,
            agreement_ratio=dominant_size / len(self.temperatures),
            candidates=tuple(outcomes),
        )
