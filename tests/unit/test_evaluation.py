from __future__ import annotations

from Agents.JudgeAgent import JudgeAgent
from meddial.evaluation.acceptance import AcceptanceCriteria
from meddial.evaluation.boundary import KnowledgeBoundaryValidator
from meddial.evaluation.claims import (
    LLMClaimExtractor,
    RoleAwareClinicalFaithfulness,
    RuleBasedClaimExtractor,
)
from meddial.evaluation.models import EvaluationStatus, MetricResult
from meddial.evaluation.structural import DeterministicStructuralValidator
from meddial.knowledge import build_conversation_contexts
from meddial.llm import MockLLMProvider


def test_claim_extraction_failure_is_error_not_perfect_score(clinical_reference):
    extractor = LLMClaimExtractor(MockLLMProvider(["not json"]))
    context = build_conversation_contexts(
        clinical_reference, "NO_DIAGNOSIS_NO_TREATMENT"
    ).evaluator
    metric = RoleAwareClinicalFaithfulness(extractor=extractor).evaluate(
        [{"role": "Patient", "content": "I have a cough."}], context
    )
    assert metric.status is EvaluationStatus.ERROR
    assert metric.score is None


def test_empty_extraction_is_unscorable_not_perfect(clinical_reference):
    extractor = LLMClaimExtractor(MockLLMProvider(["[]"]))
    context = build_conversation_contexts(clinical_reference, "FULL").evaluator
    metric = RoleAwareClinicalFaithfulness(extractor=extractor).evaluate(
        [{"role": "Patient", "content": "I have a cough."}], context
    )
    assert metric.status is EvaluationStatus.UNSCORABLE
    assert metric.score is None


def test_doctor_claims_are_included_in_factuality(clinical_reference):
    context = build_conversation_contexts(clinical_reference, "FULL").evaluator
    dialogue = [
        {"role": "Doctor", "content": "Hello."},
        {"role": "Patient", "content": "I have a dry cough."},
        {"role": "Doctor", "content": "Your MRI proved you have a brain tumour."},
        {"role": "Patient", "content": "I understand."},
    ]
    metric = RoleAwareClinicalFaithfulness(
        extractor=RuleBasedClaimExtractor()
    ).evaluate(dialogue, context)
    assert metric.status is EvaluationStatus.FAIL
    assert metric.details["doctor_claim_count"] > 0
    assert any(
        claim["role"] == "Doctor" for claim in metric.details["unsupported_claims"]
    )


def test_incomplete_dimension_cannot_pass_even_with_high_composite():
    criteria = AcceptanceCriteria(
        minimum_scores={"a": 0.5, "b": 0.5},
        report_weights={"a": 1.0, "b": 1.0},
    )
    decision = criteria.decide(
        {
            "a": MetricResult("a", EvaluationStatus.PASS, 1.0, "ok"),
            "b": MetricResult("b", EvaluationStatus.ERROR, None, "failed"),
        }
    )
    assert not decision.accepted
    assert decision.status is EvaluationStatus.ERROR
    assert decision.composite_score == 1.0


def test_knowledge_boundary_detects_doctor_hidden_symptom(clinical_reference):
    context = build_conversation_contexts(
        clinical_reference, "NO_DIAGNOSIS_NO_TREATMENT"
    ).evaluator
    metric = KnowledgeBoundaryValidator().validate(
        [
            {"role": "Doctor", "content": "Tell me about your dry cough."},
            {"role": "Patient", "content": "How did you know that?"},
        ],
        context,
    )
    assert metric.status is EvaluationStatus.FAIL
    assert metric.details["leakage_event_count"] == 1


def test_diagnostic_hypothesis_is_not_treated_as_reference_leakage(clinical_reference):
    context = build_conversation_contexts(
        clinical_reference, "NO_DIAGNOSIS_NO_TREATMENT"
    ).evaluator
    metric = KnowledgeBoundaryValidator().validate(
        [
            {"role": "Doctor", "content": "What brings you in?"},
            {"role": "Patient", "content": "I have a dry cough."},
            {
                "role": "Doctor",
                "content": "This sounds like a viral upper respiratory infection.",
            },
            {"role": "Patient", "content": "That makes sense."},
        ],
        context,
    )
    assert metric.status is EvaluationStatus.PASS


def test_structural_validator_known_failures():
    metric = DeterministicStructuralValidator(min_turns=2).validate(
        [
            {"role": "Doctor", "content": "Hello"},
            {"role": "Doctor", "content": ""},
        ]
    )
    assert metric.status is EvaluationStatus.FAIL
    assert len(metric.details["violations"]) >= 2


def test_legacy_judge_invalid_output_cannot_pass_at_low_threshold(clinical_reference):
    judge = JudgeAgent(llm=MockLLMProvider(["realistic score: 1.0"]), threshold=0.1)
    result = judge.evaluate_dialogue(
        [{"role": "Doctor", "content": "Hello"}], clinical_reference
    )
    assert result["decision"] == "UNSCORABLE"
    assert result["evaluation_status"] == "UNSCORABLE"
