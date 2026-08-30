"""Per-participant views of a reference.

Each participant gets its own object holding only what it may know. The
patient agent is handed a :class:`PatientContext` and has no route back to
the reference — masking is structural, not a prompt instruction.

:class:`DoctorContext` carries a ``guidance_id`` rather than the patient's
``policy_id`` (KNOW-6). Under the thesis code the same string drove both,
so tightening what the patient knew also rewrote the doctor's prompt and
the two effects could not be separated (defect D-05). The default keeps
``guidance_id == policy_id`` so current behaviour is reproducible, but the
two are now independent inputs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from meddial.knowledge.policy import KnowledgePolicy, ParticipantRole
from meddial.knowledge.redaction import RedactionReport
from meddial.knowledge.reference import StructuredClinicalReference


@dataclass(frozen=True)
class PatientContext:
    """What the simulated patient knows."""

    policy: KnowledgePolicy
    visible: Mapping[str, Any]
    redactions: RedactionReport = field(default_factory=RedactionReport)

    @property
    def policy_id(self) -> str:
        return self.policy.policy_id


@dataclass(frozen=True)
class DoctorContext:
    """What the simulated doctor is told, independent of the patient policy."""

    guidance_id: str
    visible: Mapping[str, Any]


@dataclass(frozen=True)
class EvaluatorContext:
    """Privileged: the full reference, held only by the evaluator (KNOW-7)."""

    reference: StructuredClinicalReference
    policy: KnowledgePolicy

    @property
    def case_id(self) -> str:
        return self.reference.case_id


@dataclass(frozen=True)
class CaseContexts:
    """The three views of one case, built together so they cannot diverge."""

    patient: PatientContext
    doctor: DoctorContext
    evaluator: EvaluatorContext


def to_legacy_profile(context: PatientContext) -> dict[str, Any]:
    """The patient view in the PascalCase shape the prompt builders expect.

    Round-tripping through the model turns an absent field into its empty
    default rather than a missing key, which is what those builders assume.
    Masked content cannot reappear this way — only defaults do.
    """
    profile = StructuredClinicalReference.model_validate(context.visible)
    payload = profile.model_dump(by_alias=True)
    payload["profile_type"] = context.policy.policy_id
    payload["policy_version"] = context.policy.version
    return payload


def build_contexts(
    reference: StructuredClinicalReference,
    policy: KnowledgePolicy,
    *,
    guidance_id: str | None = None,
) -> CaseContexts:
    """Apply ``policy`` to ``reference`` and return all three views.

    ``guidance_id`` defaults to the policy id, reproducing the coupled
    behaviour; pass it explicitly to vary the doctor's instructions
    independently of the patient's disclosure condition.
    """
    visible, redactions = policy.mask_with_report(reference)
    return CaseContexts(
        patient=PatientContext(policy=policy, visible=visible, redactions=redactions),
        doctor=DoctorContext(
            guidance_id=guidance_id if guidance_id is not None else policy.policy_id,
            visible=policy.mask(reference, ParticipantRole.DOCTOR),
        ),
        evaluator=EvaluatorContext(reference=reference, policy=policy),
    )
