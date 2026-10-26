"""Offline tests for multi-candidate result agreement."""

import pandas as pd

from t2s.generation.generator import SQLGenerationResponse
from t2s.verification.agreement import MultiCandidateAgreement


class StubGenerator:
    def __init__(self, unanswerable_temperature=None):
        self.temperatures = []
        self.unanswerable_temperature = unanswerable_temperature

    def generate(self, question, strategy, database_error=None, temperature=0.0):
        self.temperatures.append(temperature)
        answerable = temperature != self.unanswerable_temperature
        response = SQLGenerationResponse(
            sql=f"SELECT {int(temperature * 10)}",
            explanation="Stub candidate.",
            tables_used=[],
            self_reported_confidence=0.8,
            answerable=answerable,
            ambiguity_flag=False,
        )
        return response, response.sql if answerable else ""


class StubSandbox:
    def execute(self, sql):
        if "  0\n" in sql or "  3\n" in sql:
            return pd.DataFrame({"value": [1, 2]}), 1.0, None
        return pd.DataFrame({"other_label": [2, 3]}), 1.0, None


def test_agreement_clusters_unordered_equal_results_and_passes_temperatures():
    generator = StubGenerator()
    analyzer = MultiCandidateAgreement(
        generator=generator,
        sandbox=StubSandbox(),
        temperatures=(0.0, 0.3, 0.7),
    )

    result = analyzer.evaluate("question")

    assert generator.temperatures == [0.0, 0.3, 0.7]
    assert result.candidate_count == 3
    assert result.executed_count == 3
    assert result.dominant_cluster_size == 2
    assert result.agreement_ratio == 2 / 3
    assert result.candidates[0].cluster_id == result.candidates[1].cluster_id


def test_unanswerable_candidate_lowers_agreement_ratio():
    analyzer = MultiCandidateAgreement(
        generator=StubGenerator(unanswerable_temperature=0.7),
        sandbox=StubSandbox(),
        temperatures=(0.0, 0.3, 0.7),
    )

    result = analyzer.evaluate("question")

    assert result.executed_count == 2
    assert result.dominant_cluster_size == 2
    assert result.agreement_ratio == 2 / 3
    assert result.candidates[2].status == "unanswerable"
