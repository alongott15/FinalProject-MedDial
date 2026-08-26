from __future__ import annotations

import pytest

from meddial.benchmarks import CorruptionType, InjectedErrorBenchmark, benchmark_metrics
from meddial.evaluation.ensemble import (
    EnsembleConfig,
    IndependentEvaluatorEnsemble,
    ProviderIndependentEvaluator,
)
from meddial.evaluation.models import EvaluationStatus
from meddial.knowledge import build_conversation_contexts
from meddial.llm import MockLLMProvider
from meddial.recovery import FailureClass, TargetedRecoveryAgent


def evaluator_response(score: float, verdict: str = "supported") -> str:
    return (
        "{"
        f'"dimensions": {{"patient_factuality": {score}, '
        f'"doctor_factuality": {score}, "clinical_plausibility": {score}, '
        '"knowledge_boundary": 1.0}, '
        '"claim_verdicts": ['
        '{"claim_id": "t0-c0", "verdict": "unverifiable", '
        '"evidence_ids": [], "reason": "greeting"}, '
        f'{{"claim_id": "t1-c1", "verdict": "{verdict}", '
        '"evidence_ids": [], "reason": "greeting"}], '
        '"summary": "complete"}'
    )


def test_independent_multi_model_ensemble_uses_separate_configured_members(clinical_reference):
    context = build_conversation_contexts(clinical_reference, "FULL").evaluator
    evaluators = [
        ProviderIndependentEvaluator("judge-a", MockLLMProvider([evaluator_response(0.8)], "a")),
        ProviderIndependentEvaluator(
            "judge-b", MockLLMProvider([evaluator_response(0.6, "unsupported")], "b")
        ),
        ProviderIndependentEvaluator("judge-c", MockLLMProvider([evaluator_response(0.9)], "c")),
    ]
    ensemble = IndependentEvaluatorEnsemble(
        evaluators,
        EnsembleConfig(enabled=True, minimum_evaluators=3, pass_threshold=0.7),
    )
    result = ensemble.evaluate(
        [{"role": "Doctor", "content": "Hello"}, {"role": "Patient", "content": "Hi"}],
        context,
    )
    assert result.score == 0.8
    assert result.status is EvaluationStatus.PASS
    assert len(result.details["members"]) == 3
    assert result.details["dimensions"]["doctor_factuality"] == 0.8
    assert result.details["claim_consensus"][1]["verdict"] == "supported"


def test_publication_ensemble_rejects_two_judges():
    with pytest.raises(ValueError, match="at least three"):
        EnsembleConfig(enabled=True, minimum_evaluators=2)


def test_targeted_recovery_classifies_knowledge_failure():
    result = TargetedRecoveryAgent().improve_prompts(
        {
            "evaluation_status": "FAIL",
            "metrics": {"knowledge_boundary": {"status": "FAIL"}},
        },
        [],
    )
    assert result["failure_class"] == FailureClass.KNOWLEDGE_LEAKAGE.value
    assert "hidden" in result["doctor_improvements"].lower()


def test_injected_error_benchmark_and_metrics(clinical_reference):
    dialogue = [
        {"role": "Doctor", "content": "Hello"},
        {"role": "Patient", "content": "I have a cough"},
    ]
    corrupted = InjectedErrorBenchmark(seed=3).inject(
        dialogue, CorruptionType.EMPTY_TURN, clinical_reference
    )
    assert corrupted.errors[0].expected_detector == "structural_validity"
    assert "" in [turn["content"] for turn in corrupted.dialogue]
    metrics = benchmark_metrics(["empty_turn"], ["empty_turn", "extra"])
    assert metrics["true_positive"] == 1
    assert metrics["precision"] == 0.5
