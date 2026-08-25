"""Failure classification and deterministic targeted recovery strategies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class FailureClass(str, Enum):
    KNOWLEDGE_LEAKAGE = "knowledge_leakage"
    UNSUPPORTED_PATIENT_CLAIM = "unsupported_patient_claim"
    UNSUPPORTED_DOCTOR_CLAIM = "unsupported_doctor_claim"
    STRUCTURAL_FAILURE = "structural_failure"
    LOW_NATURALNESS = "low_naturalness"
    EVALUATION_ERROR = "evaluation_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RecoveryStrategy:
    failure_class: FailureClass
    patient_improvements: str
    doctor_improvements: str
    orchestration_improvements: str

    def to_legacy_dict(self) -> dict[str, str]:
        return {
            "failure_class": self.failure_class.value,
            "patient_improvements": self.patient_improvements,
            "doctor_improvements": self.doctor_improvements,
            "general_improvements": self.orchestration_improvements,
        }


class FailureClassifier:
    def classify(self, evaluation: Mapping[str, Any]) -> FailureClass:
        status = evaluation.get("evaluation_status")
        if status in {"ERROR", "UNSCORABLE"}:
            return FailureClass.EVALUATION_ERROR
        metrics = evaluation.get("metrics", {})
        boundary = metrics.get("knowledge_boundary", {})
        if boundary.get("status") == "FAIL":
            return FailureClass.KNOWLEDGE_LEAKAGE
        structural = metrics.get("structural_validity", {})
        if structural.get("status") == "FAIL":
            return FailureClass.STRUCTURAL_FAILURE
        faith = metrics.get("role_aware_clinical_faithfulness", {})
        if faith.get("status") == "FAIL":
            unsupported = faith.get("details", {}).get("unsupported_claims", [])
            if any(str(claim.get("role", "")).lower() == "doctor" for claim in unsupported):
                return FailureClass.UNSUPPORTED_DOCTOR_CLAIM
            return FailureClass.UNSUPPORTED_PATIENT_CLAIM
        naturalness = metrics.get("naturalness", {})
        if naturalness.get("status") == "FAIL":
            return FailureClass.LOW_NATURALNESS
        return FailureClass.UNKNOWN


class TargetedRecoveryAgent:
    """Maps measured failure classes to bounded prompt changes; no extra LLM call."""

    def __init__(self, classifier: FailureClassifier | None = None, llm=None) -> None:
        self.classifier = classifier or FailureClassifier()
        self.llm = llm  # retained for legacy construction; deliberately unused

    def improve_prompts(
        self,
        judge_feedback: Mapping[str, Any],
        dialogue: list,
        current_prompts: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        del dialogue, current_prompts
        failure = self.classifier.classify(judge_feedback)
        strategies = {
            FailureClass.KNOWLEDGE_LEAKAGE: RecoveryStrategy(
                failure,
                "Use only the masked patient context; do not name hidden diagnoses, treatments, or medications before the doctor discloses them.",
                "Do not use hidden clinical-reference facts that have not appeared in patient turns; ask an open question instead.",
                "Recreate both agents from their role contexts before the next attempt.",
            ),
            FailureClass.UNSUPPORTED_PATIENT_CLAIM: RecoveryStrategy(
                failure,
                "Replace unsupported details with explicit uncertainty and remain within documented patient facts.",
                "Ask for clarification instead of supplying missing patient history.",
                "Target only the unsupported patient claims identified by the evaluator.",
            ),
            FailureClass.UNSUPPORTED_DOCTOR_CLAIM: RecoveryStrategy(
                failure,
                "Answer questions without confirming unsupported clinician assertions.",
                "Frame uncertain diagnoses as hypotheses and avoid asserting tests, findings, or history not revealed in dialogue.",
                "Target only unsupported doctor claims; preserve supported content.",
            ),
            FailureClass.STRUCTURAL_FAILURE: RecoveryStrategy(
                failure,
                "Return one non-empty patient turn at a time.",
                "Return one non-empty doctor turn at a time and preserve alternating roles.",
                "Enforce turn alternation, minimum length, and error-sentinel checks before evaluation.",
            ),
            FailureClass.LOW_NATURALNESS: RecoveryStrategy(
                failure,
                "Use shorter, varied responses and disclose one relevant detail at a time.",
                "Ask one focused question per turn and avoid repeated acknowledgements.",
                "Preserve factual content while varying phrasing and pacing.",
            ),
            FailureClass.EVALUATION_ERROR: RecoveryStrategy(
                failure,
                "Do not modify patient behavior based on an incomplete evaluation.",
                "Do not modify doctor behavior based on an incomplete evaluation.",
                "Retry only the failed evaluator or mark the attempt unscorable; never accept it.",
            ),
            FailureClass.UNKNOWN: RecoveryStrategy(
                failure,
                "Preserve the knowledge policy and improve clarity conservatively.",
                "Preserve the doctor knowledge boundary and improve clarity conservatively.",
                "Apply no broad prompt rewrite without a classified failure.",
            ),
        }
        return strategies[failure].to_legacy_dict()
