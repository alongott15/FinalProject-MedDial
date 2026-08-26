"""Knowledge-boundary validation and leakage metrics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from meddial.evaluation.claims import ClaimType, classify_claim
from meddial.evaluation.models import EvaluationStatus, MetricResult
from meddial.knowledge import EvaluatorContext


@dataclass(frozen=True)
class LeakageEvent:
    role: str
    turn_index: int
    category: str
    reference_value: str
    utterance: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _values(items: Sequence[Any], keys: Sequence[str]) -> list[str]:
    values: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            for key in keys:
                value = item.get(key)
                if value and value != "not provided":
                    values.append(str(value).strip())
            for medication in item.get("medications", []):
                if isinstance(medication, Mapping) and medication.get("name"):
                    values.append(str(medication["name"]).strip())
        elif item:
            values.append(str(item).strip())
    return [value for value in values if value]


class KnowledgeBoundaryValidator:
    """Detect verbatim use of reference facts before they are conversationally revealed."""

    def validate(
        self, dialogue: Sequence[Mapping[str, str]], context: EvaluatorContext
    ) -> MetricResult:
        if not dialogue:
            return MetricResult(
                name="knowledge_boundary",
                status=EvaluationStatus.UNSCORABLE,
                score=None,
                reason="Dialogue contains no turns",
            )
        reference = context.reference_dict()
        core = reference.get("Core_Fields", {})
        ctx = reference.get("Context_Fields", {})
        policy = context.patient_context.policy

        symptom_values = _values(core.get("Symptoms", []), ("description",))
        diagnosis_values = _values(core.get("Diagnoses", []), ("primary",))
        treatment_values = _values(core.get("Treatment_Options", []), ("procedure", "treatment"))
        medication_values = _values(
            list(ctx.get("Current_Medications", [])) + list(ctx.get("Discharge_Medications", [])),
            ("name",),
        )
        allergy_values = [str(value).strip() for value in ctx.get("Allergies", []) if value]
        history = ctx.get("Medical_History", {})
        history_values = (
            [str(history.get("Past_Medical_History")).strip()]
            if isinstance(history, Mapping)
            and history.get("Past_Medical_History") not in (None, "", "not provided")
            else []
        )

        patient_forbidden: list[tuple[str, str]] = []
        if not policy.knows_diagnosis:
            patient_forbidden.extend(("diagnosis", value) for value in diagnosis_values)
        if not policy.knows_treatment_options:
            patient_forbidden.extend(("treatment", value) for value in treatment_values)
        if not policy.knows_current_medications:
            patient_forbidden.extend(("medication", value) for value in medication_values)

        # The doctor begins without any clinical facts. Patient disclosures update this set.
        doctor_hidden = [
            *(("symptom", value) for value in symptom_values),
            *(("diagnosis", value) for value in diagnosis_values),
            *(("treatment", value) for value in treatment_values),
            *(("medication", value) for value in medication_values),
            *(("allergy", value) for value in allergy_values),
            *(("medical_history", value) for value in history_values),
        ]
        patient_disclosed: set[str] = set()
        doctor_disclosed: set[str] = set()
        events: list[LeakageEvent] = []
        relevant_turns = 0

        for index, turn in enumerate(dialogue):
            role = str(turn.get("role", "Unknown"))
            content = str(turn.get("content", ""))
            normalized = content.lower()
            if role.lower() not in {"patient", "doctor"}:
                continue
            relevant_turns += 1
            if role.lower() == "patient":
                for category, value in patient_forbidden:
                    term = value.lower()
                    if term in normalized and term not in doctor_disclosed:
                        events.append(
                            LeakageEvent(
                                role=role,
                                turn_index=index,
                                category=category,
                                reference_value=value,
                                utterance=content,
                                reason="Patient used a policy-hidden reference fact before disclosure",
                            )
                        )
                for _, value in doctor_hidden:
                    if value.lower() in normalized:
                        patient_disclosed.add(value.lower())
            else:
                claim_type = classify_claim(role, content)
                for category, value in doctor_hidden:
                    term = value.lower()
                    allowed_inference = (
                        category == "diagnosis" and claim_type is ClaimType.DIAGNOSTIC_HYPOTHESIS
                    ) or (
                        category in {"treatment", "medication"}
                        and claim_type in {ClaimType.RECOMMENDATION, ClaimType.ADVICE}
                    )
                    if (
                        term in normalized
                        and term not in patient_disclosed
                        and not allowed_inference
                    ):
                        events.append(
                            LeakageEvent(
                                role=role,
                                turn_index=index,
                                category=category,
                                reference_value=value,
                                utterance=content,
                                reason="Doctor used a clinical reference fact before patient disclosure",
                            )
                        )
                    if term in normalized:
                        doctor_disclosed.add(term)

        leakage_rate = len(events) / relevant_turns if relevant_turns else 0.0
        return MetricResult(
            name="knowledge_boundary",
            status=EvaluationStatus.FAIL if events else EvaluationStatus.PASS,
            score=max(0.0, 1.0 - leakage_rate),
            reason=(
                f"Detected {len(events)} potential leakage event(s) across "
                f"{relevant_turns} evaluable turns"
            ),
            details={
                "leakage_events": [event.to_dict() for event in events],
                "leakage_event_count": len(events),
                "leakage_rate": leakage_rate,
                "evaluable_turn_count": relevant_turns,
            },
        )
