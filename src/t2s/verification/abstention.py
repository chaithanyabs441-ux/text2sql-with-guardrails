"""Risk-aware response decisions and alternatives for SQL query results."""

from dataclasses import dataclass
from typing import Literal

Decision = Literal["answer", "warn", "abstain_with_alternatives"]


@dataclass(frozen=True)
class AbstentionThresholds:
    """Probability cutoffs selected from a held-out validation split."""

    answer_threshold: float
    warn_threshold: float

    def __post_init__(self) -> None:
        if not 0 <= self.warn_threshold <= self.answer_threshold <= 1.01:
            raise ValueError("Expected 0 <= warn threshold <= answer threshold <= 1.01")


def choose_decision(
    confidence: float,
    thresholds: AbstentionThresholds,
    warning_count: int,
    agreement_ratio: float,
) -> Decision:
    """Select answer, warning, or abstention from calibrated confidence signals."""
    score = min(1.0, max(0.0, confidence))
    if (
        score >= thresholds.answer_threshold
        and warning_count == 0
        and agreement_ratio >= 2 / 3
    ):
        return "answer"
    if score >= thresholds.warn_threshold:
        return "warn"
    return "abstain_with_alternatives"
