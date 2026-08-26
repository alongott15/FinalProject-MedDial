"""Explicit, role-specific knowledge policies and context construction.

The full Structured Clinical Reference is never passed directly to a dialogue
agent.  A caller must first construct the patient, doctor and evaluator views.
This makes accidental information leakage visible and testable.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any


class ProfileType(str, Enum):
    FULL = "FULL"
    NO_DIAGNOSIS = "NO_DIAGNOSIS"
    NO_DIAGNOSIS_NO_TREATMENT = "NO_DIAGNOSIS_NO_TREATMENT"

    @classmethod
    def parse(cls, value: str | ProfileType) -> ProfileType:
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(f"Unknown profile type {value!r}; expected one of: {allowed}") from exc


@dataclass(frozen=True)
class KnowledgePolicy:
    profile_type: ProfileType
    knows_diagnosis: bool
    knows_treatment_options: bool
    knows_current_medications: bool
    knows_discharge_medications: bool
    description: str
    disclosure_rules: str

    @property
    def knows_treatment(self) -> bool:
        """Compatibility name used by legacy prompt code."""
        return self.knows_treatment_options


_POLICIES: Mapping[ProfileType, KnowledgePolicy] = MappingProxyType(
    {
        ProfileType.FULL: KnowledgePolicy(
            profile_type=ProfileType.FULL,
            knows_diagnosis=True,
            knows_treatment_options=True,
            knows_current_medications=True,
            knows_discharge_medications=True,
            description=(
                "The patient knows the documented symptoms, diagnosis, treatment options, "
                "current medications and discharge medications."
            ),
            disclosure_rules=(
                "The patient may discuss documented diagnoses and treatments, but must not "
                "invent facts that are absent from the patient context."
            ),
        ),
        ProfileType.NO_DIAGNOSIS: KnowledgePolicy(
            profile_type=ProfileType.NO_DIAGNOSIS,
            knows_diagnosis=False,
            knows_treatment_options=True,
            knows_current_medications=True,
            knows_discharge_medications=True,
            description=(
                "The patient knows symptoms and documented treatments/medications, but not "
                "the formal diagnosis."
            ),
            disclosure_rules=(
                "The patient may describe documented treatments and medications, but must not "
                "state a formal diagnosis before it is disclosed in the conversation."
            ),
        ),
        ProfileType.NO_DIAGNOSIS_NO_TREATMENT: KnowledgePolicy(
            profile_type=ProfileType.NO_DIAGNOSIS_NO_TREATMENT,
            knows_diagnosis=False,
            knows_treatment_options=False,
            knows_current_medications=False,
            knows_discharge_medications=False,
            description=(
                "The patient knows the documented symptoms and background history, but has no "
                "episode diagnosis, treatment plan or medication exposure in the simulation."
            ),
            disclosure_rules=(
                "The patient must not state a diagnosis, treatment option, current medication or "
                "discharge medication that was removed by this policy."
            ),
        ),
    }
)


def get_knowledge_policy(profile_type: str | ProfileType) -> KnowledgePolicy:
    return _POLICIES[ProfileType.parse(profile_type)]


@dataclass(frozen=True)
class PatientContext:
    profile_type: ProfileType
    profile: Mapping[str, Any]
    policy: KnowledgePolicy

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.profile))


@dataclass(frozen=True)
class DoctorContext:
    """Information available to the doctor before the first utterance.

    Symptoms, diagnoses, treatments, medications, allergies, history and the
    chief complaint are intentionally absent.  They become available only
    through conversation turns.
    """

    profile_type: ProfileType
    demographics: Mapping[str, Any]
    policy_description: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_type": self.profile_type.value,
            "Context_Fields": {
                "Patient_Demographics": copy.deepcopy(dict(self.demographics)),
            },
            "knowledge_scope": "doctor_initial_context",
            "policy_description": self.policy_description,
        }


@dataclass(frozen=True)
class EvaluatorContext:
    """Privileged reference plus the exact views given to dialogue agents."""

    full_reference: Mapping[str, Any]
    patient_context: PatientContext
    doctor_context: DoctorContext

    def reference_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.full_reference))


@dataclass(frozen=True)
class ConversationContexts:
    patient: PatientContext
    doctor: DoctorContext
    evaluator: EvaluatorContext


def mask_profile_for_patient(
    full_reference: Mapping[str, Any], profile_type: str | ProfileType
) -> dict[str, Any]:
    """Return a deep-copied profile containing only patient-visible fields."""
    policy = get_knowledge_policy(profile_type)
    profile = copy.deepcopy(dict(full_reference))
    core = profile.setdefault("Core_Fields", {})
    context = profile.setdefault("Context_Fields", {})

    if not policy.knows_diagnosis:
        core["Diagnoses"] = []
    if not policy.knows_treatment_options:
        core["Treatment_Options"] = []
    if not policy.knows_current_medications:
        context["Current_Medications"] = []
    if not policy.knows_discharge_medications:
        context["Discharge_Medications"] = []

    profile["profile_type"] = policy.profile_type.value
    profile["knowledge_policy"] = {
        "knows_diagnosis": policy.knows_diagnosis,
        "knows_treatment_options": policy.knows_treatment_options,
        "knows_current_medications": policy.knows_current_medications,
        "knows_discharge_medications": policy.knows_discharge_medications,
    }
    return profile


def build_doctor_context(
    full_reference: Mapping[str, Any], profile_type: str | ProfileType
) -> DoctorContext:
    policy = get_knowledge_policy(profile_type)
    demographics = (
        full_reference.get("Context_Fields", {}).get("Patient_Demographics", {})
        if isinstance(full_reference, Mapping)
        else {}
    )
    return DoctorContext(
        profile_type=policy.profile_type,
        demographics=MappingProxyType(copy.deepcopy(dict(demographics or {}))),
        policy_description=(
            "The doctor starts with demographics only. Clinical facts must be learned from "
            "the dialogue; the profile variant describes patient knowledge, not doctor knowledge."
        ),
    )


def build_conversation_contexts(
    full_reference: Mapping[str, Any], profile_type: str | ProfileType
) -> ConversationContexts:
    policy = get_knowledge_policy(profile_type)
    patient_profile = mask_profile_for_patient(full_reference, policy.profile_type)
    patient = PatientContext(
        profile_type=policy.profile_type,
        profile=MappingProxyType(patient_profile),
        policy=policy,
    )
    doctor = build_doctor_context(full_reference, policy.profile_type)
    evaluator = EvaluatorContext(
        full_reference=MappingProxyType(copy.deepcopy(dict(full_reference))),
        patient_context=patient,
        doctor_context=doctor,
    )
    return ConversationContexts(patient=patient, doctor=doctor, evaluator=evaluator)
