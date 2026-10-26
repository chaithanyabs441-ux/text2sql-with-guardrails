import logging
from typing import Any

from pydantic import BaseModel, Field

from t2s.config import settings
from t2s.llm.openai_adapter import OpenAICompatibleAdapter

logger = logging.getLogger(__name__)

class BackTranslationResponse(BaseModel):
    question: str = Field(description="The natural language question this SQL answers.")
    reasoning: str = Field(description="Brief reasoning of how the SQL maps to the question.")

class HallucinationDetector:
    def __init__(self) -> None:
        self._judge_llm: OpenAICompatibleAdapter | None = None
        self._embedder: Any = None

    @property
    def judge_llm(self) -> OpenAICompatibleAdapter:
        """Create the judge provider client on first back-translation call."""
        if self._judge_llm is None:
            if not settings.JUDGE_API_KEY:
                raise RuntimeError("JUDGE_API_KEY is not configured.")
            self._judge_llm = OpenAICompatibleAdapter(
                api_key=settings.JUDGE_API_KEY,
                base_url=settings.JUDGE_BASE_URL,
                model=settings.JUDGE_MODEL,
            )
        return self._judge_llm

    @judge_llm.setter
    def judge_llm(self, client: OpenAICompatibleAdapter) -> None:
        """Allow tests and callers to provide a compatible judge client."""
        self._judge_llm = client

    def back_translate(self, sql: str) -> str:
        prompt = f"""You are an expert SQL analyst.
Read the following SQL query and write the natural language question that it answers.
Focus on the exact tables, filters, aggregations, ordering, and row limit used.
If the SQL contains an explicit LIMIT value, describe that exact value. Do not
replace an explicit smaller LIMIT with a default or policy row cap such as 1000.
If the SQL contains no LIMIT, do not claim that the user requested a particular
number of rows.

SQL:
{sql}

Respond in exactly this JSON format:
{{
    "question": "The question this SQL answers.",
    "reasoning": "Brief explanation of the SQL logic."
}}
"""
        response = self.judge_llm.generate_json(prompt, BackTranslationResponse)
        return response.question

    def calculate_similarity(self, original_question: str, back_translated_question: str) -> float:
        """Computes semantic similarity using embeddings (0.0 to 1.0)."""
        if self._embedder is None:
            from sentence_transformers import SentenceTransformer

            self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
        from sentence_transformers import util

        emb1 = self._embedder.encode(original_question, convert_to_tensor=True)
        emb2 = self._embedder.encode(back_translated_question, convert_to_tensor=True)
        
        # Cosine similarity
        cosine_score = util.cos_sim(emb1, emb2).item()
        # Normalize from [-1, 1] to [0, 1]
        return (cosine_score + 1) / 2
