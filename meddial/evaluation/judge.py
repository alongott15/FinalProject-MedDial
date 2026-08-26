"""Role-aware clinical judge with fail-closed acceptance."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from meddial.evaluation.acceptance import AcceptanceCriteria
from meddial.evaluation.boundary import KnowledgeBoundaryValidator
from meddial.evaluation.claims import RoleAwareClinicalFaithfulness
from meddial.evaluation.ensemble import IndependentEvaluatorEnsemble
from meddial.evaluation.models import EvaluationStatus, MetricResult
from meddial.evaluation.structural import DeterministicStructuralValidator
from meddial.knowledge import EvaluatorContext, build_conversation_contexts
from meddial.llm import (
    ChatMessage,
    DataClassification,
    LLMProvider,
    ensure_provider_compatible,
    load_restricted_clinical_model,
)


class RoleAwareJudgeAgent:
    def __init__(
        self,
        llm: LLMProvider | None = None,
        threshold: float = 0.70,
        acceptance_criteria: AcceptanceCriteria | None = None,
        faithfulness: RoleAwareClinicalFaithfulness | None = None,
        boundary_validator: KnowledgeBoundaryValidator | None = None,
        structural_validator: DeterministicStructuralValidator | None = None,
        ensemble: IndependentEvaluatorEnsemble | None = None,
        data_classification: DataClassification = DataClassification.RESTRICTED_CLINICAL,
    ) -> None:
        self.llm = llm or load_restricted_clinical_model(temperature=0.1, max_tokens=800)
        ensure_provider_compatible(self.llm, data_classification)
        self.threshold = threshold  # legacy/reporting compatibility only
        self.faithfulness = faithfulness or RoleAwareClinicalFaithfulness()
        self.boundary_validator = boundary_validator or KnowledgeBoundaryValidator()
        self.structural_validator = structural_validator or DeterministicStructuralValidator()
        self.ensemble = ensemble
        self.acceptance = acceptance_criteria or AcceptanceCriteria()

    def _naturalness(self, transcript: str) -> MetricResult:
        messages = [
            ChatMessage(
                role="system",
                content=(
                    "Rate only the conversational naturalness of this simulated clinician-patient "
                    "dialogue. Return only JSON with score in [0,1] and reason. Do not score "
                    "factuality in this dimension."
                ),
            ),
            ChatMessage(role="user", content=transcript),
        ]
        try:
            response = self.llm.generate(messages).content
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if not match:
                raise ValueError("response did not contain JSON")
            parsed = json.loads(match.group(0))
            score = float(parsed["score"])
            if not 0.0 <= score <= 1.0:
                raise ValueError("score outside [0,1]")
            reason = str(parsed.get("reason", "No reason supplied"))
        except Exception as exc:
            return MetricResult(
                name="naturalness",
                status=EvaluationStatus.ERROR,
                score=None,
                reason=f"Naturalness evaluation failed: {exc}",
            )
        return MetricResult(
            name="naturalness",
            status=EvaluationStatus.PASS if score >= 0.60 else EvaluationStatus.FAIL,
            score=score,
            reason=reason,
            details={"model": self.llm.model_name},
        )

    def evaluate_dialogue(
        self,
        dialogue: Sequence[Mapping[str, str]],
        patient_profile: Mapping[str, Any] | None = None,
        dialogue_transcript: str | None = None,
        evaluator_context: EvaluatorContext | None = None,
    ) -> dict[str, Any]:
        if dialogue_transcript is None:
            dialogue_transcript = "\n".join(
                f"{turn.get('role', 'Unknown')}: {turn.get('content', '')}" for turn in dialogue
            )
        if evaluator_context is None:
            if patient_profile is None:
                return self._incomplete_result("No evaluator context or profile was supplied")
            profile_type = patient_profile.get("profile_type", "NO_DIAGNOSIS_NO_TREATMENT")
            evaluator_context = build_conversation_contexts(
                patient_profile, str(profile_type)
            ).evaluator

        metric_list = [
            self._naturalness(dialogue_transcript),
            self.faithfulness.evaluate(dialogue, evaluator_context),
            self.boundary_validator.validate(dialogue, evaluator_context),
            self.structural_validator.validate(dialogue),
        ]
        if self.ensemble and self.ensemble.config.enabled:
            metric_list.append(self.ensemble.evaluate(dialogue, evaluator_context))
            minimum_scores = {
                **self.acceptance.minimum_scores,
                "independent_ensemble": self.ensemble.config.pass_threshold,
            }
            acceptance = AcceptanceCriteria(
                minimum_scores=minimum_scores,
                report_weights={
                    **self.acceptance.report_weights,
                    "independent_ensemble": 0.20,
                },
            )
        else:
            acceptance = self.acceptance
        metrics = {metric.name: metric for metric in metric_list}
        decision = acceptance.decide(metrics)
        label = {
            EvaluationStatus.PASS: "REALISTIC",
            EvaluationStatus.FAIL: "UNREALISTIC",
            EvaluationStatus.ERROR: "ERROR",
            EvaluationStatus.UNSCORABLE: "UNSCORABLE",
        }[decision.status]
        faith = metrics["role_aware_clinical_faithfulness"]
        boundary = metrics["knowledge_boundary"]
        return {
            "decision": label,
            "evaluation_status": decision.status.value,
            "score": decision.composite_score or 0.0,
            "composite_score": decision.composite_score,
            "accepted": decision.accepted,
            "justification": decision.reason,
            "acceptance": decision.to_dict(),
            "metrics": {name: metric.to_dict() for name, metric in metrics.items()},
            "deepeval_scores": {
                # Compatibility keys; the custom metric is deliberately not called RAGAS.
                "naturalness": metrics["naturalness"].score,
                "profile_compliance": boundary.score,
                "claim_faithfulness": faith.score,
                "role_aware_clinical_faithfulness": faith.score,
                "knowledge_boundary": boundary.score,
                "structural_validity": metrics["structural_validity"].score,
                "profile_type": evaluator_context.patient_context.profile_type.value,
            },
            "feedback_for_improvement": self._feedback(metrics),
        }

    def _feedback(self, metrics: Mapping[str, MetricResult]) -> dict[str, str]:
        return {
            "patient_side": metrics["role_aware_clinical_faithfulness"].reason,
            "doctor_side": metrics["knowledge_boundary"].reason,
            "conversation_flow": metrics["naturalness"].reason,
            "structure": metrics["structural_validity"].reason,
        }

    def _incomplete_result(self, reason: str) -> dict[str, Any]:
        return {
            "decision": "UNSCORABLE",
            "evaluation_status": EvaluationStatus.UNSCORABLE.value,
            "score": 0.0,
            "composite_score": None,
            "accepted": False,
            "justification": reason,
            "metrics": {},
            "deepeval_scores": {},
            "feedback_for_improvement": {},
        }

    def set_threshold(self, threshold: float) -> None:
        self.threshold = threshold

    def set_few_shot_examples(self, examples: list) -> None:
        del examples
