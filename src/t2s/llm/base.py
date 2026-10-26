from abc import ABC, abstractmethod
from typing import Type, TypeVar, Any
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

class BaseLLM(ABC):
    """
    Abstract base class for all LLM providers.
    Ensures consistent JSON-mode structured output across different providers.
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model

    @abstractmethod
    def generate_json(
        self, 
        prompt: str, 
        response_model: Type[T], 
        temperature: float = 0.0,
        max_retries: int = 3
    ) -> T:
        """
        Generates text, parses it into the provided Pydantic model.
        If parsing fails, it should trigger a repair-retry prompt.
        """
        pass

    @abstractmethod
    def get_usage(self) -> dict[str, Any]:
        """Returns token usage, cost, and latency of the last call."""
        pass