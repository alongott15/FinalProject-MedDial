"""Independent evaluator ensemble architecture and configuration hooks."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from meddial.evaluation.models import EvaluationStatus, MetricResult
from meddial.knowledge import EvaluatorContext
from meddial.llm import ChatMessage, LLMProvider


class IndependentEvaluator(Protocol):
    @property
    def evaluator_id(self) -> str: ...

    def evaluate(
        self, dialogue: Sequence[Mapping[str, str]], context: EvaluatorContext
    ) -> MetricResult: ...


@dataclass(frozen=True)
class EvaluatorModelConfig:
    evaluator_id: str
    provider: str
    model: str
    enabled: bool = True


@dataclass(frozen=True)
class EnsembleConfig:
    enabled: bool = False
    aggregation: str = "median"
    minimum_evaluators: int = 2
    pass_threshold: float = 0.70
    evaluators: tuple[EvaluatorModelConfig, ...] = field(default_factory=tuple)


class ProviderIndependentEvaluator:
    """One independent model judge with strict JSON output parsing."""

    def __init__(
        self,
        evaluator_id: str,
        provider: LLMProvider,
        threshold: float = 0.70,
    ) -> None:
        self._evaluator_id = evaluator_id
        self.provider = provider
        self.threshold = threshold

    @property
    def evaluator_id(self) -> str:
        return self._evaluator_id

    def evaluate(
        self, dialogue: Sequence[Mapping[str, str]], context: EvaluatorContext
    ) -> MetricResult:
        payload = {
            "dialogue": list(dialogue),
            "patient_policy": context.patient_context.policy.description,
            "patient_view": context.patient_context.as_dict(),
            "doctor_initial_view": context.doctor_context.as_dict(),
            "clinical_reference": context.reference_dict(),
        }
        messages = [
            ChatMessage(
                role="system",
                content=(
                    "Independently evaluate clinical plausibility, role-aware factuality and "
                    "knowledge-boundary compliance. Return only JSON with numeric score in [0,1] "
                    "and a short reason. Do not assume another evaluator's result."
                ),
            ),
            ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ]
        try:
            response = self.provider.generate(messages).content
            match = re.search(r"\{.*\}", response, re.DOTALL)
            if not match:
                raise ValueError("missing JSON object")
            parsed = json.loads(match.group(0))
            score = float(parsed["score"])
            if not 0.0 <= score <= 1.0:
                raise ValueError("score outside [0,1]")
            reason = str(parsed.get("reason", "No reason supplied"))
        except Exception as exc:
            return MetricResult(
                name="independent_evaluator",
                status=EvaluationStatus.ERROR,
                score=None,
                reason=f"Independent evaluator failed: {exc}",
                evaluator=self.evaluator_id,
            )
        return MetricResult(
            name="independent_evaluator",
            status=EvaluationStatus.PASS if score >= self.threshold else EvaluationStatus.FAIL,
            score=score,
            reason=reason,
            evaluator=self.evaluator_id,
            details={"model": self.provider.model_name},
        )


class IndependentEvaluatorEnsemble:
    def __init__(
        self,
        evaluators: Sequence[IndependentEvaluator],
        config: EnsembleConfig | None = None,
    ) -> None:
        self.evaluators = tuple(evaluators)
        self.config = config or EnsembleConfig(enabled=bool(evaluators))

    def evaluate(
        self, dialogue: Sequence[Mapping[str, str]], context: EvaluatorContext
    ) -> MetricResult:
        if not self.config.enabled:
            return MetricResult(
                name="independent_ensemble",
                status=EvaluationStatus.UNSCORABLE,
                score=None,
                reason="Independent ensemble is disabled",
            )
        results = [evaluator.evaluate(dialogue, context) for evaluator in self.evaluators]
        complete = [result for result in results if result.complete and result.score is not None]
        if len(complete) < self.config.minimum_evaluators:
            status = (
                EvaluationStatus.ERROR
                if any(result.status is EvaluationStatus.ERROR for result in results)
                else EvaluationStatus.UNSCORABLE
            )
            return MetricResult(
                name="independent_ensemble",
                status=status,
                score=None,
                reason=(
                    f"Only {len(complete)} complete independent evaluations; "
                    f"{self.config.minimum_evaluators} required"
                ),
                details={"members": [result.to_dict() for result in results]},
            )
        scores: list[float] = []
        for result in complete:
            if result.score is not None:
                scores.append(result.score)
        scores.sort()
        if self.config.aggregation == "mean":
            score = sum(scores) / len(scores)
        elif self.config.aggregation == "minimum":
            score = min(scores)
        else:
            midpoint = len(scores) // 2
            score = (
                scores[midpoint]
                if len(scores) % 2
                else (scores[midpoint - 1] + scores[midpoint]) / 2
            )
        return MetricResult(
            name="independent_ensemble",
            status=(
                EvaluationStatus.PASS
                if score >= self.config.pass_threshold
                else EvaluationStatus.FAIL
            ),
            score=score,
            reason=f"Aggregated {len(scores)} independent evaluator scores",
            details={"members": [result.to_dict() for result in results]},
        )
