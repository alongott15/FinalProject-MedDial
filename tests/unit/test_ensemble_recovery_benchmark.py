from __future__ import annotations

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


def test_independent_multi_model_ensemble_uses_separate_configured_members(clinical_reference):
    context = build_conversation_contexts(clinical_reference, "FULL").evaluator
    evaluators = [
        ProviderIndependentEvaluator("judge-a", MockLLMProvider(['{"score": 0.8, "reason": "ok"}'], "a")),
        ProviderIndependentEvaluator("judge-b", MockLLMProvider(['{"score": 0.6, "reason": "mixed"}'], "b")),
    ]
    ensemble = IndependentEvaluatorEnsemble(
        evaluators,
        EnsembleConfig(enabled=True, minimum_evaluators=2, pass_threshold=0.7),
    )
    result = ensemble.evaluate(
        [{"role": "Doctor", "content": "Hello"}, {"role": "Patient", "content": "Hi"}],
        context,
    )
    assert result.score == 0.7
    assert result.status is EvaluationStatus.PASS
    assert len(result.details["members"]) == 2


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
