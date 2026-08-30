"""Score provenance and the score type every evaluator returns.

Implements Implementation Plan §3.3 and §4.2. Three properties matter:

* **EVAL-3** — a :class:`Score` cannot be constructed without a
  :class:`ScoreProvenance`. A number with no record of which weights, which
  reference and which prompt produced it is not evidence, so the type system
  refuses to represent one.
* **EVAL-5** — ``INCOMPLETE`` is a first-class outcome, not a zero. A
  dimension that could not be measured reports ``value=None`` and says why;
  it never contributes a default number to an aggregate.
* **EVAL-2** — ``reference_mode`` is recorded on every score, because the
  same dialogue scored against the policy context and against the full
  reference yields different numbers, and E0 exists to measure that gap.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from meddial.llm import CallMetadata


class EvaluationStatus(str, Enum):
    """Outcome of one measurement."""

    PASS = "pass"
    FAIL = "fail"
    INCOMPLETE = "incomplete"


class ReferenceMode(str, Enum):
    """Which reference a claim is checked against.

    ``POLICY_CONTEXT`` scores against what the patient was allowed to see,
    which shrinks as disclosure is restricted. ``FULL_REFERENCE`` scores
    against the whole clinical reference regardless of policy. Confound (1)
    of the E0 gate is that the thesis used the first and reported the trend
    as a property of generation.
    """

    POLICY_CONTEXT = "policy_context"
    FULL_REFERENCE = "full_reference"


class TurnScope(str, Enum):
    """Which turns a score covers.

    The thesis scored patient turns only, so a doctor who invented a lab
    value was never penalised — confound (2) of the E0 gate.
    """

    PATIENT = "patient"
    DOCTOR = "doctor"
    ALL = "all"


@dataclass(frozen=True)
class ScoreProvenance:
    """Everything needed to attribute a score to the run that produced it."""

    scorer_id: str
    model_family: str
    model_id: str
    model_digest: str
    quantisation: str
    reference_mode: ReferenceMode
    turn_scope: TurnScope
    prompt_version: str
    sampling: Mapping[str, Any]
    fallback_used: bool = False
    incomplete_reason: str | None = None

    @classmethod
    def from_call(
        cls,
        metadata: CallMetadata,
        *,
        scorer_id: str,
        reference_mode: ReferenceMode,
        turn_scope: TurnScope,
        prompt_version: str,
        incomplete_reason: str | None = None,
    ) -> ScoreProvenance:
        """Build provenance from the metadata a provider returned."""
        return cls(
            scorer_id=scorer_id,
            model_family=metadata.model_family,
            model_id=metadata.model_id,
            model_digest=metadata.model_digest,
            quantisation=metadata.quantisation,
            reference_mode=reference_mode,
            turn_scope=turn_scope,
            prompt_version=prompt_version,
            sampling={"temperature": metadata.temperature, "seed": metadata.seed},
            incomplete_reason=incomplete_reason,
        )

    @classmethod
    def unmeasured(
        cls,
        *,
        scorer_id: str,
        reference_mode: ReferenceMode,
        turn_scope: TurnScope,
        prompt_version: str,
        reason: str,
        model_family: str = "none",
        model_id: str = "none",
        model_digest: str = "none",
        quantisation: str = "none",
    ) -> ScoreProvenance:
        """Provenance for a score no model call produced.

        Used when a dimension is unmeasurable before any call is made (an
        empty factual claim set) or when every call failed. The reason is
        carried forward so a reader can tell *why* a cell is empty.
        """
        return cls(
            scorer_id=scorer_id,
            model_family=model_family,
            model_id=model_id,
            model_digest=model_digest,
            quantisation=quantisation,
            reference_mode=reference_mode,
            turn_scope=turn_scope,
            prompt_version=prompt_version,
            sampling={},
            incomplete_reason=reason,
        )


@dataclass(frozen=True)
class Score:
    """One measured dimension.

    ``provenance`` deliberately has no default: omitting it is a
    :class:`TypeError`, not a score with unknown origin (EVAL-3).
    """

    value: float | None
    status: EvaluationStatus
    provenance: ScoreProvenance
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status is EvaluationStatus.INCOMPLETE:
            if self.value is not None:
                raise ValueError("an INCOMPLETE score must carry value=None")
            if not self.provenance.incomplete_reason:
                raise ValueError("an INCOMPLETE score must record incomplete_reason")
        else:
            if self.value is None:
                raise ValueError(f"a {self.status.value} score must carry a value")
            if not 0.0 <= self.value <= 1.0:
                raise ValueError(f"score value {self.value} outside [0, 1]")

    @classmethod
    def incomplete(
        cls, provenance: ScoreProvenance, *, detail: Mapping[str, Any] | None = None
    ) -> Score:
        """A dimension that could not be measured. The reason comes from provenance."""
        return cls(
            value=None,
            status=EvaluationStatus.INCOMPLETE,
            provenance=provenance,
            detail=dict(detail or {}),
        )

    @classmethod
    def measured(
        cls,
        value: float,
        provenance: ScoreProvenance,
        *,
        threshold: float | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> Score:
        """A measured value.

        ``threshold`` decides ``PASS``/``FAIL``. When it is ``None`` the score
        is reported as ``PASS`` meaning *measured*; gating against per-dimension
        thresholds is ``acceptance.py``'s job, not the scorer's.
        """
        status = EvaluationStatus.PASS
        if threshold is not None and value < threshold:
            status = EvaluationStatus.FAIL
        return cls(
            value=value,
            status=status,
            provenance=provenance,
            detail=dict(detail or {}),
        )
