"""Per-dimension acceptance gates, with the composite kept strictly outside them.

Implements W3 item 7 (EVAL-5, EVAL-7, PRD §9.5).

Two rules define this module:

* **A dialogue failing any mandatory dimension is rejected**, whatever the
  composite says. A high average cannot buy back a leaked diagnosis.
* **``INCOMPLETE`` is not a failure and not a pass.** A dimension that could
  not be measured makes the dialogue's verdict ``INCOMPLETE``, which excludes
  it from aggregates instead of scoring it as bad. A definite failure still
  outranks an unknown: a dialogue with one ``FAIL`` is rejected even if
  another dimension is unmeasured.

The composite is retained only for continuity with the thesis, and its
definition is deliberately preserved unchanged even though its faithfulness
term is now known to be role-partial — it uses patient factuality alone,
exactly as the thesis did. Every place it appears carries the note that it is
reporting only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .provenance import EvaluationStatus, Score

PATIENT_FACTUALITY = "patient_factuality"
DOCTOR_FACTUALITY = "doctor_factuality"
KNOWLEDGE_BOUNDARY = "knowledge_boundary"
NATURALNESS = "naturalness"
STRUCTURAL_VALIDITY = "structural_validity"

MANDATORY_DIMENSIONS: tuple[str, ...] = (
    PATIENT_FACTUALITY,
    DOCTOR_FACTUALITY,
    KNOWLEDGE_BOUNDARY,
    NATURALNESS,
    STRUCTURAL_VALIDITY,
)

DEFAULT_THRESHOLDS: Mapping[str, float] = {
    PATIENT_FACTUALITY: 0.80,
    DOCTOR_FACTUALITY: 0.80,
    KNOWLEDGE_BOUNDARY: 1.0,
    NATURALNESS: 0.60,
    STRUCTURAL_VALIDITY: 1.0,
}
"""Module defaults. A run overrides these from its config and records what it used."""

COMPOSITE_WEIGHTS: Mapping[str, float] = {
    NATURALNESS: 0.4,
    KNOWLEDGE_BOUNDARY: 0.3,
    PATIENT_FACTUALITY: 0.3,
}
"""PRD §9.5, preserved unchanged: 0.4 naturalness + 0.3 compliance + 0.3 faithfulness."""

COMPOSITE_NOTE = "reporting only; not used for acceptance"


class Acceptance(str, Enum):
    """The verdict for one dialogue."""

    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class Composite:
    """The thesis composite. Computed, reported, and never consulted for acceptance."""

    value: float | None
    terms: Mapping[str, float | None]
    note: str = COMPOSITE_NOTE

    def as_record(self) -> dict[str, Any]:
        return {"value": self.value, "note": self.note, "terms": dict(self.terms)}


@dataclass(frozen=True)
class AcceptanceResult:
    """Per-dimension verdicts and the overall decision they imply."""

    overall: Acceptance
    per_dimension: Mapping[str, EvaluationStatus]
    composite: Composite
    thresholds: Mapping[str, float]
    missing: tuple[str, ...] = ()

    def as_record(self) -> dict[str, Any]:
        """The shape PRD §6.3 stores under ``acceptance``."""
        return {
            "overall": self.overall.value,
            "per_dimension": {name: status.value for name, status in self.per_dimension.items()},
            "thresholds": dict(self.thresholds),
            "missing": list(self.missing),
        }


def gate(score: Score | None, threshold: float) -> EvaluationStatus:
    """Apply one threshold. An unmeasured dimension stays ``INCOMPLETE``."""
    if score is None or score.status is EvaluationStatus.INCOMPLETE or score.value is None:
        return EvaluationStatus.INCOMPLETE
    return EvaluationStatus.PASS if score.value >= threshold else EvaluationStatus.FAIL


def compute_composite(scores: Mapping[str, Score]) -> Composite:
    """The thesis composite, or ``None`` when any of its terms is unmeasured."""
    terms: dict[str, float | None] = {}
    for dimension in COMPOSITE_WEIGHTS:
        score = scores.get(dimension)
        terms[dimension] = None if score is None else score.value

    if any(value is None for value in terms.values()):
        return Composite(value=None, terms=terms)

    total = sum(COMPOSITE_WEIGHTS[name] * float(value) for name, value in terms.items())
    return Composite(value=total, terms=terms)


def decide(
    scores: Mapping[str, Score],
    *,
    thresholds: Mapping[str, float] | None = None,
    mandatory: tuple[str, ...] = MANDATORY_DIMENSIONS,
) -> AcceptanceResult:
    """Gate each mandatory dimension, then combine (EVAL-7).

    A missing dimension is treated exactly like an unmeasured one: absent
    evidence is not evidence of a pass.
    """
    gates = dict(DEFAULT_THRESHOLDS)
    gates.update(thresholds or {})

    per_dimension: dict[str, EvaluationStatus] = {}
    missing: list[str] = []
    for dimension in mandatory:
        score = scores.get(dimension)
        if score is None:
            missing.append(dimension)
        per_dimension[dimension] = gate(score, gates.get(dimension, 1.0))

    statuses = per_dimension.values()
    if EvaluationStatus.FAIL in statuses:
        overall = Acceptance.REJECT
    elif EvaluationStatus.INCOMPLETE in statuses:
        overall = Acceptance.INCOMPLETE
    else:
        overall = Acceptance.ACCEPT

    return AcceptanceResult(
        overall=overall,
        per_dimension=per_dimension,
        composite=compute_composite(scores),
        thresholds={name: gates.get(name, 1.0) for name in mandatory},
        missing=tuple(missing),
    )
