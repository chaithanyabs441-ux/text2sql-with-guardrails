"""Policy-check and execute answerable gold SQL using the read-only sandbox."""

import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from t2s.guardrails.exceptions import UnsafeQueryError  # noqa: E402
from t2s.guardrails.policy import SQLPolicyEngine  # noqa: E402
from t2s.sandbox.executor import ExecutionSandbox  # noqa: E402


def load_questions(path: Path) -> list[dict[str, Any]]:
    """Load and minimally validate the golden-set JSON structure."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    questions = payload.get("questions")
    if not isinstance(questions, list):
        raise ValueError("Golden set must contain a 'questions' array")
    return questions


def verify_gold_set(path: Path, policy_only: bool = False) -> int:
    """Check gold SQL policy and optionally execute each answerable query."""
    questions = load_questions(path)
    policy = SQLPolicyEngine()
    sandbox = ExecutionSandbox()
    failures = 0
    skipped = 0

    for item in questions:
        question_id = item.get("id", "unknown")
        gold_sql = item.get("gold_sql")
        if gold_sql is None:
            skipped += 1
            print(f"[SKIP] {question_id}: no executable gold SQL")
            continue

        try:
            safe_sql = policy.validate(gold_sql)
        except UnsafeQueryError as exc:
            failures += 1
            print(f"[FAIL] {question_id}: policy rejected gold SQL: {exc}")
            continue

        if policy_only:
            print(f"[POLICY OK] {question_id}")
            continue

        dataframe, latency_ms, error = sandbox.execute(safe_sql)
        if error:
            failures += 1
            print(f"[FAIL] {question_id}: execution error: {error}")
        else:
            print(
                f"[OK] {question_id}: rows={len(dataframe)}, "
                f"latency_ms={latency_ms:.2f}"
            )

    print(
        f"Summary: total={len(questions)}, "
        f"verified={len(questions) - skipped - failures}, "
        f"skipped={skipped}, failed={failures}"
    )
    return failures


if __name__ == "__main__":
    arguments = sys.argv[1:]
    policy_only = "--policy-only" in arguments
    arguments = [argument for argument in arguments if argument != "--policy-only"]
    golden_set_path = Path(arguments[0]) if arguments else (
        PROJECT_ROOT / "eval" / "golden_set.json"
    )
    raise SystemExit(1 if verify_gold_set(golden_set_path, policy_only) else 0)
