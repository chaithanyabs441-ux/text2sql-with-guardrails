"""Order-insensitive comparison helpers for tabular query results."""

import json
from typing import Any

import pandas as pd


def _normalize_cell(value: Any) -> Any:
    """Normalize pandas and NumPy scalar values for stable comparison."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        value = value.item()
    return value


def result_fingerprint(frame: pd.DataFrame) -> tuple[int, tuple[str, ...]]:
    """Return a stable multiset fingerprint that ignores row order and labels."""
    rows = [
        json.dumps(
            [_normalize_cell(value) for value in row],
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        for row in frame.itertuples(index=False, name=None)
    ]
    return frame.shape[1], tuple(sorted(rows))


def result_sets_equal(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    """Compare result rows as multisets, ignoring row order and column labels."""
    return result_fingerprint(left) == result_fingerprint(right)
