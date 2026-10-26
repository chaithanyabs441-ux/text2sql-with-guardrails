import logging

from pydantic import BaseModel, Field, field_validator

from t2s.config import settings
from t2s.generation.prompt_builder import PromptBuilder
from t2s.guardrails.policy import SQLPolicyEngine
from t2s.llm.exceptions import ProviderRefusalError
from t2s.llm.openai_adapter import OpenAICompatibleAdapter
from t2s.schema.introspector import SchemaIntrospector
from t2s.schema.models import DatabaseSchema
from t2s.schema.retriever import SchemaRetriever

logger = logging.getLogger(__name__)

# The strict Pydantic model for the LLM output
class SQLGenerationResponse(BaseModel):
    sql: str
    explanation: str
    tables_used: list[str]
    self_reported_confidence: float = Field(ge=0, le=1)
    answerable: bool
    ambiguity_flag: bool

    @field_validator("sql", mode="before")
    @classmethod
    def normalize_missing_sql(cls, value: object) -> object:
        """Treat provider ``null`` SQL as empty so unanswerable outputs validate."""
        return "" if value is None else value

class SQLGenerator:
    def __init__(self, schema_retriever: SchemaRetriever | None = None) -> None:
        self._llm: OpenAICompatibleAdapter | None = None
        self.prompt_builder = PromptBuilder()
        self.policy_engine = SQLPolicyEngine()
        self.schema_retriever = schema_retriever or SchemaRetriever()
        self.schema: DatabaseSchema | None = None

    @property
    def llm(self) -> OpenAICompatibleAdapter:
        """Create the provider client on first use, after app startup."""
        if self._llm is None:
            if not settings.GENERATOR_API_KEY:
                raise RuntimeError("GENERATOR_API_KEY is not configured.")
            self._llm = OpenAICompatibleAdapter(
                api_key=settings.GENERATOR_API_KEY,
                base_url=settings.GENERATOR_BASE_URL,
                model=settings.GENERATOR_MODEL,
            )
        return self._llm

    @llm.setter
    def llm(self, client: OpenAICompatibleAdapter) -> None:
        """Allow tests and callers to provide a compatible client."""
        self._llm = client

    def get_schema(self) -> DatabaseSchema:
        if not self.schema:
            introspector = SchemaIntrospector()
            self.schema = introspector.extract_schema()
        return self.schema

    def generate(
        self,
        question: str,
        strategy: str = "schema_glossary",
        database_error: str | None = None,
        temperature: float = 0.0,
    ) -> tuple[SQLGenerationResponse, str]:
        """Generate SQL at a sampling temperature, then enforce policy."""
        schema = self.get_schema()
        relevant_schema = self.schema_retriever.filter_schema(question, schema)
        prompt = self.prompt_builder.build_generation_prompt(
            question,
            relevant_schema,
            strategy=strategy,
            database_error=database_error,
        )

        try:
            llm_response = self.llm.generate_json(
                prompt, SQLGenerationResponse, temperature=temperature
            )
        except ProviderRefusalError:
            logger.info("Model provider refused the request; abstaining safely.")
            return (
                SQLGenerationResponse(
                    sql="",
                    explanation="The model provider refused this request.",
                    tables_used=[],
                    self_reported_confidence=0.0,
                    answerable=False,
                    ambiguity_flag=True,
                ),
                "",
            )
        if not llm_response.answerable:
            logger.warning("LLM flagged the question as unanswerable.")
            return llm_response, ""
        if not llm_response.sql.strip():
            raise ValueError("The model marked the question answerable but returned no SQL.")

        try:
            safe_sql = self.policy_engine.validate(llm_response.sql)
            return llm_response, safe_sql
        except Exception:
            logger.exception("LLM generated unsafe SQL")
            raise
