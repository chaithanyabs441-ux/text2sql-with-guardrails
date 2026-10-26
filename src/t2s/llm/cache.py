import os
import json
import hashlib
from typing import Any

CACHE_DIR = os.path.join(os.getcwd(), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

def get_cache_key(model: str, prompt: str, temperature: float) -> str:
    """Creates a unique SHA-256 hash for a given prompt and model settings."""
    content = f"{model}-{prompt}-{temperature}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

def read_cache(key: str) -> dict[str, Any] | None:
    """Reads a cached response if it exists."""
    filepath = os.path.join(CACHE_DIR, f"{key}.json")
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def write_cache(key: str, response_data: dict[str, Any]) -> None:
    """Saves a response to the disk cache."""
    filepath = os.path.join(CACHE_DIR, f"{key}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(response_data, f, indent=2)