"""Per-dimension acceptance criteria; composite score is reporting-only."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

from meddial.evaluation.models import EvaluationStatus, MetricResult


@dataclass(frozen=True)
class AcceptanceDecision:
    accepted: bool
    status: EvaluationStatus
    failed_dimensions: tuple[str, ...]
    incomplete_dimensions: tuple[str, ...]
    composite_score: float | None
    reason: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass(frozen=True)
class AcceptanceCriteria:
    minimum_scores: Mapping[str, float] = field(
        default_factory=lambda: {
            "naturalness": 0.60,
            "role_aware_clinical_faithfulness": 0.70,
            "knowledge_boundary": 1.0,
            "structural_validity": 1.0,
        }
    )
    report_weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "naturalness": 0.30,
            "role_aware_clinical_faithfulness": 0.35,
            "knowledge_boundary": 0.25,
            "structural_validity": 0.10,
        }
    )

    def decide(self, metrics: Mapping[str, MetricResult]) -> AcceptanceDecision:
        incomplete: list[str] = []
        failed: list[str] = []
        for name, threshold in self.minimum_scores.items():
            result = metrics.get(name)
            if result is None or not result.complete or result.score is None:
                incomplete.append(name)
            elif result.status is EvaluationStatus.FAIL or result.score < threshold:
                failed.append(name)

        weighted: list[tuple[float, float]] = []
        for name, weight in self.report_weights.items():
            result = metrics.get(name)
            if result is not None and result.score is not None:
                weighted.append((result.score, weight))
        composite = (
            sum(score * weight for score, weight in weighted)
            / sum(weight for _, weight in weighted)
            if weighted
            else None
        )
        if incomplete:
            statuses = {metrics[name].status for name in incomplete if name in metrics}
            status = (
                EvaluationStatus.ERROR
                if EvaluationStatus.ERROR in statuses
                else EvaluationStatus.UNSCORABLE
            )
            return AcceptanceDecision(
                False,
                status,
                tuple(failed),
                tuple(incomplete),
                composite,
                "Evaluation is incomplete; incomplete dimensions cannot pass",
            )
        if failed:
            return AcceptanceDecision(
                False,
                EvaluationStatus.FAIL,
                tuple(failed),
                (),
                composite,
                "One or more mandatory dimensions failed",
            )
        return AcceptanceDecision(
            True,
            EvaluationStatus.PASS,
            (),
            (),
            composite,
            "All mandatory dimensions passed",
        )
