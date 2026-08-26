"""Independent, structured, fail-closed evaluator ensemble."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from meddial.evaluation.claims import RuleBasedClaimExtractor
from meddial.evaluation.models import EvaluationStatus, MetricResult
from meddial.knowledge import EvaluatorContext
from meddial.llm import (
    ChatMessage,
    DataClassification,
    LLMProvider,
    ensure_provider_compatible,
)

INDEPENDENT_DIMENSIONS = (
    "patient_factuality",
    "doctor_factuality",
    "clinical_plausibility",
    "knowledge_boundary",
)


class ClaimVerdict(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNVERIFIABLE = "unverifiable"


class IndependentEvaluator(Protocol):
    @property
    def evaluator_id(self) -> str: ...

    @property
    def model_family(self) -> str: ...

    def evaluate(
        self, dialogue: Sequence[Mapping[str, str]], context: EvaluatorContext
    ) -> MetricResult: ...


@dataclass(frozen=True)
class EvaluatorModelConfig:
    evaluator_id: str
    provider: str
    model: str
    model_family: str
    enabled: bool = True
    local_only: bool = True


@dataclass(frozen=True)
class EnsembleConfig:
    enabled: bool = False
    aggregation: str = "median"
    minimum_evaluators: int = 3
    pass_threshold: float = 0.70
    dimension_thresholds: Mapping[str, float] = field(
        default_factory=lambda: {
            "patient_factuality": 0.70,
            "doctor_factuality": 0.70,
            "clinical_plausibility": 0.70,
            "knowledge_boundary": 1.0,
        }
    )
    evaluators: tuple[EvaluatorModelConfig, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.enabled and self.minimum_evaluators < 3:
            raise ValueError("Publication ensembles require at least three evaluators")
        if self.aggregation not in {"median", "mean", "minimum"}:
            raise ValueError(f"Unsupported ensemble aggregation: {self.aggregation}")
        missing = set(INDEPENDENT_DIMENSIONS) - set(self.dimension_thresholds)
        if missing:
            raise ValueError(f"Missing independent dimension thresholds: {sorted(missing)}")


def _json_object(text: str) -> Mapping[str, Any]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("missing JSON object")
    value = json.loads(match.group(0))
    if not isinstance(value, Mapping):
        raise ValueError("evaluator output was not a JSON object")
    return value


def _aggregate(values: Sequence[float], operation: str) -> float:
    ordered = sorted(values)
    if operation == "mean":
        return sum(ordered) / len(ordered)
    if operation == "minimum":
        return min(ordered)
    midpoint = len(ordered) // 2
    return (
        ordered[midpoint] if len(ordered) % 2 else (ordered[midpoint - 1] + ordered[midpoint]) / 2
    )


class ProviderIndependentEvaluator:
    """One model-family judge with strict claim- and dimension-level JSON output."""

    def __init__(
        self,
        evaluator_id: str,
        provider: LLMProvider,
        threshold: float = 0.70,
        dimension_thresholds: Mapping[str, float] | None = None,
        data_classification: DataClassification = DataClassification.RESTRICTED_CLINICAL,
        model_family: str | None = None,
    ) -> None:
        ensure_provider_compatible(provider, data_classification)
        self._evaluator_id = evaluator_id
        self.provider = provider
        self._model_family = model_family or provider.model_name.split("/", maxsplit=1)[0]
        self.threshold = threshold
        self.dimension_thresholds = dict(
            dimension_thresholds
            or {
                "patient_factuality": threshold,
                "doctor_factuality": threshold,
                "clinical_plausibility": threshold,
                "knowledge_boundary": 1.0,
            }
        )
        self.extractor = RuleBasedClaimExtractor()

    @property
    def evaluator_id(self) -> str:
        return self._evaluator_id

    @property
    def model_family(self) -> str:
        return self._model_family

    def _claim_payload(
        self, dialogue: Sequence[Mapping[str, str]]
    ) -> tuple[list[dict[str, Any]], set[str]]:
        extraction = self.extractor.extract(dialogue)
        if extraction.status is not EvaluationStatus.PASS:
            raise ValueError(extraction.reason)
        claims: list[dict[str, Any]] = []
        expected_ids: set[str] = set()
        for ordinal, claim in enumerate(extraction.claims):
            claim_id = f"t{claim.turn_index}-c{ordinal}"
            expected_ids.add(claim_id)
            claims.append({"claim_id": claim_id, **claim.to_dict()})
        return claims, expected_ids

    def evaluate(
        self, dialogue: Sequence[Mapping[str, str]], context: EvaluatorContext
    ) -> MetricResult:
        try:
            claims, expected_claim_ids = self._claim_payload(dialogue)
        except Exception as exc:
            return MetricResult(
                name="independent_evaluator",
                status=EvaluationStatus.UNSCORABLE,
                score=None,
                reason=f"Independent evaluator could not prepare claims: {exc}",
                evaluator=self.evaluator_id,
            )
        payload = {
            "dialogue": list(dialogue),
            "claims": claims,
            "patient_policy": context.patient_context.policy.description,
            "patient_view": context.patient_context.as_dict(),
            "doctor_initial_view": context.doctor_context.as_dict(),
            "clinical_reference": context.reference_dict(),
        }
        messages = [
            ChatMessage(
                role="system",
                content=(
                    "Independently evaluate the supplied atomic claims. Return only one JSON "
                    "object with: dimensions (patient_factuality, doctor_factuality, "
                    "clinical_plausibility, knowledge_boundary; each numeric in [0,1]); "
                    "claim_verdicts (one item for every claim_id, with claim_id, verdict equal "
                    "to supported, unsupported, or unverifiable, evidence_ids as a JSON array, "
                    "and a short reason); and summary. Do not omit claims, infer another "
                    "evaluator's result, or treat a diagnostic hypothesis as an established fact."
                ),
            ),
            ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ]
        try:
            parsed = _json_object(self.provider.generate(messages).content)
            raw_dimensions = parsed["dimensions"]
            if not isinstance(raw_dimensions, Mapping):
                raise ValueError("dimensions must be an object")
            dimensions = {name: float(raw_dimensions[name]) for name in INDEPENDENT_DIMENSIONS}
            if any(not 0.0 <= score <= 1.0 for score in dimensions.values()):
                raise ValueError("dimension score outside [0,1]")
            raw_verdicts = parsed["claim_verdicts"]
            if not isinstance(raw_verdicts, list):
                raise ValueError("claim_verdicts must be an array")
            verdicts: list[dict[str, Any]] = []
            returned_ids: set[str] = set()
            for item in raw_verdicts:
                if not isinstance(item, Mapping):
                    raise ValueError("claim verdict must be an object")
                claim_id = str(item["claim_id"])
                if claim_id in returned_ids:
                    raise ValueError(f"duplicate verdict for {claim_id}")
                returned_ids.add(claim_id)
                evidence_ids = item.get("evidence_ids", [])
                if not isinstance(evidence_ids, list):
                    raise ValueError("evidence_ids must be an array")
                verdicts.append(
                    {
                        "claim_id": claim_id,
                        "verdict": ClaimVerdict(str(item["verdict"])).value,
                        "evidence_ids": [str(value) for value in evidence_ids],
                        "reason": str(item.get("reason", "")),
                    }
                )
            if returned_ids != expected_claim_ids:
                missing = sorted(expected_claim_ids - returned_ids)
                extra = sorted(returned_ids - expected_claim_ids)
                raise ValueError(
                    f"claim verdict coverage mismatch; missing={missing}, extra={extra}"
                )
            summary = str(parsed.get("summary", "No summary supplied"))
        except Exception as exc:
            return MetricResult(
                name="independent_evaluator",
                status=EvaluationStatus.ERROR,
                score=None,
                reason=f"Independent evaluator failed: {exc}",
                evaluator=self.evaluator_id,
            )
        failed_dimensions = [
            name
            for name, threshold in self.dimension_thresholds.items()
            if dimensions[name] < threshold
        ]
        score = min(dimensions.values())
        return MetricResult(
            name="independent_evaluator",
            status=EvaluationStatus.FAIL if failed_dimensions else EvaluationStatus.PASS,
            score=score,
            reason=summary,
            evaluator=self.evaluator_id,
            details={
                "model": self.provider.model_name,
                "dimensions": dimensions,
                "failed_dimensions": failed_dimensions,
                "claim_verdicts": verdicts,
            },
        )


class IndependentEvaluatorEnsemble:
    def __init__(
        self,
        evaluators: Sequence[IndependentEvaluator],
        config: EnsembleConfig | None = None,
    ) -> None:
        self.evaluators = tuple(evaluators)
        self.config = config or EnsembleConfig(enabled=bool(evaluators))
        if self.config.enabled and len(self.evaluators) < self.config.minimum_evaluators:
            raise ValueError(
                f"Configured {len(self.evaluators)} evaluators; "
                f"{self.config.minimum_evaluators} required"
            )
        identifiers = [evaluator.evaluator_id for evaluator in self.evaluators]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Independent evaluator IDs must be unique")
        families = [evaluator.model_family.lower() for evaluator in self.evaluators]
        if self.config.enabled and len(set(families)) < self.config.minimum_evaluators:
            raise ValueError("Publication evaluators must use distinct model families")

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
        try:
            dimensions = {
                name: _aggregate(
                    [float(result.details["dimensions"][name]) for result in complete],
                    self.config.aggregation,
                )
                for name in INDEPENDENT_DIMENSIONS
            }
        except (KeyError, TypeError, ValueError) as exc:
            return MetricResult(
                name="independent_ensemble",
                status=EvaluationStatus.ERROR,
                score=None,
                reason=f"Independent dimension aggregation failed: {exc}",
                details={"members": [result.to_dict() for result in results]},
            )

        verdict_groups: dict[str, list[Mapping[str, Any]]] = {}
        for result in complete:
            for verdict in result.details.get("claim_verdicts", []):
                verdict_groups.setdefault(str(verdict["claim_id"]), []).append(verdict)
        consensus: list[dict[str, Any]] = []
        for claim_id, verdicts in sorted(verdict_groups.items()):
            counts = Counter(str(item["verdict"]) for item in verdicts)
            verdict, votes = counts.most_common(1)[0]
            if votes < 2:
                verdict = ClaimVerdict.UNVERIFIABLE.value
            consensus.append(
                {
                    "claim_id": claim_id,
                    "verdict": verdict,
                    "votes": dict(counts),
                    "requires_adjudication": len(counts) > 1,
                }
            )

        failed_dimensions = [
            name
            for name, threshold in self.config.dimension_thresholds.items()
            if dimensions[name] < threshold
        ]
        score = min(dimensions.values())
        return MetricResult(
            name="independent_ensemble",
            status=EvaluationStatus.FAIL if failed_dimensions else EvaluationStatus.PASS,
            score=score,
            reason=(
                f"Aggregated {len(complete)} independent evaluators; "
                f"failed dimensions: {failed_dimensions or 'none'}"
            ),
            details={
                "dimensions": dimensions,
                "dimension_thresholds": dict(self.config.dimension_thresholds),
                "failed_dimensions": failed_dimensions,
                "claim_consensus": consensus,
                "members": [result.to_dict() for result in results],
                "deterministic_validators_remain_authoritative": True,
            },
        )
