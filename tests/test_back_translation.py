"""Tests for instructions sent to the SQL back-translation judge."""

from t2s.verification.back_translation import BackTranslationResponse, HallucinationDetector


class RecordingJudge:
    def generate_json(self, prompt, response_model):
        assert response_model is BackTranslationResponse
        self.prompt = prompt
        return BackTranslationResponse(
            question="Show the ten highest-revenue customers.",
            reasoning="The query sorts by revenue and limits output to ten rows.",
        )


def test_back_translation_prompt_respects_explicit_limit():
    detector = HallucinationDetector()
    judge = RecordingJudge()
    detector.judge_llm = judge

    question = detector.back_translate(
        "SELECT id FROM customers ORDER BY net_revenue DESC LIMIT 10"
    )

    assert question == "Show the ten highest-revenue customers."
    assert "describe that exact value" in judge.prompt
    assert "replace an explicit smaller LIMIT" in judge.prompt
