import json
import logging
import time
from typing import Any, TypeVar

from openai import BadRequestError, OpenAI
from pydantic import BaseModel, ValidationError

from t2s.llm.base import BaseLLM
from t2s.llm.cache import get_cache_key, read_cache, write_cache
from t2s.llm.cost_tracker import cost_tracker
from t2s.llm.exceptions import ProviderRefusalError

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

class OpenAICompatibleAdapter(BaseLLM):
    """
    Adapter for any OpenAI-compatible endpoint (Groq, DeepSeek, Ollama).
    Includes disk caching, JSON mode, and a Pydantic repair-retry loop.
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        super().__init__(api_key, base_url, model)
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        self.last_usage: dict[str, Any] = {}

    def generate_json(
        self, 
        prompt: str,
        response_model: type[T],
        temperature: float = 0.0,
        max_retries: int = 3,
    ) -> T:
        
        cache_key = get_cache_key(self.model, prompt, temperature)
        
        # 1. Check Cache
        cached_data = read_cache(cache_key)
        if cached_data:
            logger.info(f"Cache HIT for model {self.model}")
            self.last_usage = {
                "model": self.model,
                "cache_hit": True,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "latency_sec": 0.0,
                "estimated_cost_usd": 0.0,
            }
            return response_model.model_validate(cached_data)

        logger.info(f"Cache MISS. Calling {self.model} API...")
        
        current_prompt = prompt
        
        # 2. Retry Loop (JSON Repair)
        for attempt in range(max_retries):
            start_time = time.time()
            try:
                # Enforce JSON mode
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": current_prompt}],
                    temperature=temperature,
                    response_format={"type": "json_object"},
                )
                
                latency = time.time() - start_time
                raw_content = response.choices[0].message.content
                
                usage = response.usage
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                self.last_usage = cost_tracker.record(
                    model=self.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_sec=latency,
                )

                # Parse JSON
                parsed_json = json.loads(raw_content)
                if self._is_refusal_payload(parsed_json):
                    raise ProviderRefusalError(
                        "The model provider refused this request."
                    )
                
                # Validate with Pydantic
                validated_model = response_model.model_validate(parsed_json)
                
                # Save to cache
                write_cache(cache_key, parsed_json)
                return validated_model

            except BadRequestError as exc:
                if self._is_provider_refusal(exc):
                    raise ProviderRefusalError(
                        "The model provider refused this request."
                    ) from exc
                raise
                
            except (json.JSONDecodeError, ValidationError) as e:
                logger.warning(f"Attempt {attempt + 1} failed. Error: {e}")
                if attempt == max_retries - 1:
                    message = (
                        f"Failed to generate valid JSON after {max_retries} attempts."
                    )
                    raise ValueError(message) from e
                
                # Repair prompt
                current_prompt = (
                    "The JSON you previously provided was invalid or failed validation.\n"
                    f"Error: {e}\n\n"
                    "Please correct the JSON and provide it again. Do not include markdown "
                    "formatting or explanations; return only the raw JSON object.\n\n"
                    "Original Request:\n"
                    f"{prompt}"
                )

    @classmethod
    def _is_provider_refusal(cls, error: BadRequestError) -> bool:
        """Recognize providers that encode a safety refusal as invalid JSON."""
        parts = [str(error)]
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            error_body = body.get("error", body)
            if isinstance(error_body, dict):
                parts.extend(
                    str(error_body.get(key, ""))
                    for key in ("message", "failed_generation", "code")
                )
        response_text = " ".join(parts).lower().replace("’", "'")
        return cls._contains_refusal(response_text)

    @classmethod
    def _is_refusal_payload(cls, payload: Any) -> bool:
        """Recognize JSON-wrapped refusal strings before schema validation."""
        if not isinstance(payload, dict):
            return False
        error = payload.get("error")
        if isinstance(error, dict):
            refusal_text = " ".join(str(value) for value in error.values())
        elif isinstance(error, str):
            refusal_text = error
        else:
            return False
        return cls._contains_refusal(refusal_text.lower())

    @staticmethod
    def _contains_refusal(text: str) -> bool:
        refusal_phrases = (
            "i'm sorry",
            "i’m sorry",
            "i cannot comply",
            "i can't comply",
            "i can’t comply",
            "cannot help with that",
            "can't help with that",
            "can’t help with that",
            "cannot assist with that",
            "can't assist with that",
            "can’t assist with that",
        )
        return any(phrase in text for phrase in refusal_phrases)

    def get_usage(self) -> dict[str, Any]:
        return self.last_usage
