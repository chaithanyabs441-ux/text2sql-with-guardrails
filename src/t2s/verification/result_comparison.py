"""Order-insensitive comparison helpers for tabular query results."""

import json
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

RELATIVE_TOLERANCE = Decimal("1e-6")


def _normalize_cell(value: Any) -> Any:
    """Normalize pandas and NumPy scalar values for strict comparison."""
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
    """Return the historical exact-value fingerprint, ignoring labels and row order."""
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


def _is_null(value: Any) -> bool:
    """Return whether a SQL/Pandas value represents NULL."""
    if value is None:
        return True
    try:
        is_null = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(is_null) if isinstance(is_null, bool) else False


def _as_decimal(value: Any) -> Decimal | None:
    """Convert ordinary SQL numeric values to Decimal without losing text precision."""
    value = _normalize_cell(value)
    if isinstance(value, bool):
        return None
    if not isinstance(value, int | float | Decimal):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _values_equal(left: Any, right: Any) -> bool:
    """Compare cells with SQL NULL and relative numeric tolerance semantics."""
    left = _normalize_cell(left)
    right = _normalize_cell(right)
    left_null = _is_null(left)
    right_null = _is_null(right)
    if left_null or right_null:
        return left_null and right_null

    # bool is an int subclass in Python, but SQL booleans are not numbers.
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right

    left_number = _as_decimal(left)
    right_number = _as_decimal(right)
    if left_number is not None or right_number is not None:
        if left_number is None or right_number is None:
            return False
        if left_number == right_number:
            return True
        if not left_number.is_finite() or not right_number.is_finite():
            return False
        scale = max(abs(left_number), abs(right_number))
        return abs(left_number - right_number) <= RELATIVE_TOLERANCE * scale

    try:
        result = left == right
        return bool(result)
    except (TypeError, ValueError):
        return False


def _multiset_equal(
    left: list[Any], right: list[Any], equal: Callable[[Any, Any], bool]
) -> bool:
    """Compare values as multisets using one-to-one matching."""
    if len(left) != len(right):
        return False

    # Fast path handles the common case and long runs of duplicate values
    # without recursive augmenting paths.
    available = set(range(len(right)))
    greedy_succeeded = True
    for left_value in left:
        match_index = next(
            (index for index in available if equal(left_value, right[index])), None
        )
        if match_index is None:
            greedy_succeeded = False
            break
        available.remove(match_index)
    if greedy_succeeded:
        return True

    matched_left = [-1] * len(right)

    def match(left_index: int, seen: list[bool]) -> bool:
        for right_index, right_value in enumerate(right):
            if seen[right_index] or not equal(left[left_index], right_value):
                continue
            seen[right_index] = True
            if matched_left[right_index] == -1 or match(matched_left[right_index], seen):
                matched_left[right_index] = left_index
                return True
        return False

    return all(match(index, [False] * len(right)) for index in range(len(left)))


def _row_equal(left: tuple[Any, ...], right: tuple[Any, ...]) -> bool:
    return len(left) == len(right) and all(
        _values_equal(left_value, right_value)
        for left_value, right_value in zip(left, right, strict=True)
    )


def _flexible_results_equal(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    """Match every left column to one right column, then compare projected rows."""
    if len(left) != len(right) or left.shape[1] > right.shape[1]:
        return False

    left_columns = [left.iloc[:, index].tolist() for index in range(left.shape[1])]
    right_columns = [right.iloc[:, index].tolist() for index in range(right.shape[1])]
    candidates = [
        [
            right_index
            for right_index, right_column in enumerate(right_columns)
            if _multiset_equal(left_column, right_column, _values_equal)
        ]
        for left_column in left_columns
    ]
    if any(not options for options in candidates):
        return False

    # Try the most constrained columns first. Distinct generated columns are
    # required so a single column cannot satisfy two gold columns by itself.
    order = sorted(range(len(candidates)), key=lambda index: len(candidates[index]))
    mapping: dict[int, int] = {}

    def find_mapping(position: int, used: set[int]) -> bool:
        if position == len(order):
            projected = [
                tuple(row[mapping[index]] for index in range(len(left_columns)))
                for row in right.itertuples(index=False, name=None)
            ]
            gold_rows = list(left.itertuples(index=False, name=None))
            return _multiset_equal(gold_rows, projected, _row_equal)

        left_index = order[position]
        for right_index in candidates[left_index]:
            if right_index in used:
                continue
            mapping[left_index] = right_index
            if find_mapping(position + 1, used | {right_index}):
                return True
            del mapping[left_index]
        return False

    return find_mapping(0, set())


def result_sets_equal(
    left: pd.DataFrame, right: pd.DataFrame, *, strict: bool = False
) -> bool:
    """Compare result rows without relying on labels or row order.

    Flexible mode requires every column in ``left`` (the gold result) to match
    a distinct generated column by values. Extra generated columns are allowed;
    duplicate rows are preserved, NULLs match NULLs, and numeric values use a
    relative tolerance of 1e-6. Set ``strict=True`` for the previous exact
    comparison, including its equal-column-count requirement.

    ORDER BY never makes physical row order significant. Different rows chosen
    at an ORDER BY ... LIMIT tie boundary are not assumed equivalent because
    this tabular comparator cannot identify the query's ordering expressions.
    Use a deterministic secondary ORDER BY key when evaluating top-k queries;
    this avoids claiming correctness for a different tied subset.
    """
    if strict:
        return result_fingerprint(left) == result_fingerprint(right)
    return _flexible_results_equal(left, right)
