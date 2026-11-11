"""Adjusted document validity (implementation.md §4.7.5).

The model's own ``document_validity_score`` is self-assessed and uncalibrated, so we never return
it alone. The adjusted score combines three signals:

    adjusted = model_validity
               x product(penalty of each failed business rule)
               x min(1, mean(confidence of critical fields) / CRITICAL_CONFIDENCE_TARGET)

Penalty weights and thresholds are deliberately simple constants: they are calibrated against the
golden set in Phase 8, and the raw model score is returned alongside so the adjustment is
auditable.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import prod

from docintel.schemas.common import RuleViolation

CRITICAL_CONFIDENCE_TARGET = 0.9


@dataclass(frozen=True)
class ValidityScore:
    adjusted: float
    model_reported: float
    low_confidence_fields: tuple[str, ...]


def score(
    *,
    model_validity: float,
    violations: Sequence[RuleViolation],
    critical_confidences: Iterable[float],
    field_confidences: dict[str, float],
    low_confidence_threshold: float,
) -> ValidityScore:
    critical = list(critical_confidences)
    critical_factor = (
        min(1.0, (sum(critical) / len(critical)) / CRITICAL_CONFIDENCE_TARGET) if critical else 1.0
    )
    adjusted = model_validity * prod(v.penalty for v in violations) * critical_factor
    low = tuple(
        sorted(name for name, conf in field_confidences.items() if conf < low_confidence_threshold)
    )
    return ValidityScore(
        adjusted=round(max(0.0, min(1.0, adjusted)), 3),
        model_reported=round(model_validity, 3),
        low_confidence_fields=low,
    )
