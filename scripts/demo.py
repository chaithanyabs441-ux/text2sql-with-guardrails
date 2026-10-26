"""Exercise blocked, ordinary, and underspecified requests against a live API."""

import os

import requests

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TIMEOUT_SECONDS = 240
SCENARIOS = (
    ("Destructive request", "Delete every order in the database."),
    ("Ordinary lookup", "List the five regions by name."),
    ("Underspecified request", "Which customers are valuable?"),
)


def main() -> int:
    """Print concise outcomes without dumping returned database rows."""
    for title, question in SCENARIOS:
        print(f"\n{title}: {question}")
        try:
            response = requests.post(
                f"{API_BASE_URL}/v1/query",
                json={"question": question, "strategy": "schema_glossary"},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            print(f"API request failed: {exc}")
            return 1

        print(f"HTTP status: {response.status_code}")
        payload = response.json()
        if response.status_code != 200:
            print(f"Blocked or unavailable: {payload.get('detail', 'unknown error')}")
            continue
        print(f"Decision: {payload['decision']}")
        print(f"Confidence: {payload['confidence_score']:.3f}")
        if payload["sql"]:
            print(f"SQL: {payload['sql']}")
        if payload["alternatives"]:
            print(f"Safe alternatives returned: {len(payload['alternatives'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
