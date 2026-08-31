"""Per-fault detector metrics with case-clustered intervals (BENCH-2)."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from meddial.benchmarks.injection import CorruptionType
from meddial.stats import Interval


class DetectorEvaluationError(ValueError):
    """Observations cannot support the requested detector measurement."""


@dataclass(frozen=True)
class DetectorObservation:
    """One clean or corrupted dialogue judged for one fault class."""

    case_id: str
    corruption_type: CorruptionType
    is_corrupted: bool
    score: float
    predicted: bool
    predicted_turns: tuple[int, ...] = ()
    ground_truth_turn_index: int | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise DetectorEvaluationError("detector score must be in [0, 1]")
        if self.is_corrupted and self.ground_truth_turn_index is None:
            raise DetectorEvaluationError(
                "a corrupted observation requires ground_truth_turn_index"
            )
        if not self.is_corrupted and self.ground_truth_turn_index is not None:
            raise DetectorEvaluationError(
                "a clean observation cannot carry a ground-truth fault turn"
            )


@dataclass(frozen=True)
class ConfusionMatrix:
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int

    def as_record(self) -> dict[str, int]:
        return {
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "true_negatives": self.true_negatives,
        }


@dataclass(frozen=True)
class DetectorMetrics:
    corruption_type: CorruptionType
    n_cases: int
    n_observations: int
    confusion: ConfusionMatrix
    precision: Interval
    recall: Interval
    f1: Interval
    auc: Interval
    localisation_accuracy: Interval

    def as_record(self) -> dict[str, Any]:
        return {
            "corruption_type": self.corruption_type.value,
            "n_cases": self.n_cases,
            "n_observations": self.n_observations,
            "confusion_matrix": self.confusion.as_record(),
            "precision": self.precision.as_record(),
            "recall": self.recall.as_record(),
            "f1": self.f1.as_record(),
            "auc": self.auc.as_record(),
            "localisation_accuracy": self.localisation_accuracy.as_record(),
        }


@dataclass(frozen=True)
class DetectorReport:
    by_fault_class: Mapping[CorruptionType, DetectorMetrics]

    def as_record(self) -> dict[str, Any]:
        return {
            kind.value: metrics.as_record()
            for kind, metrics in sorted(
                self.by_fault_class.items(), key=lambda item: item[0].value
            )
        }


def evaluate_detector(
    observations: Sequence[DetectorObservation],
    *,
    resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
) -> DetectorReport:
    """Report P/R/F1/AUC and correct-turn localisation for every fault class."""

    if not observations:
        raise DetectorEvaluationError("no detector observations were supplied")
    if resamples < 1:
        raise DetectorEvaluationError("resamples must be positive")
    grouped: dict[CorruptionType, list[DetectorObservation]] = defaultdict(list)
    for observation in observations:
        grouped[observation.corruption_type].append(observation)

    return DetectorReport(
        by_fault_class={
            kind: _evaluate_class(
                kind,
                values,
                resamples=resamples,
                confidence=confidence,
                seed=seed + offset,
            )
            for offset, (kind, values) in enumerate(
                sorted(grouped.items(), key=lambda item: item[0].value)
            )
        }
    )


def _evaluate_class(
    kind: CorruptionType,
    observations: Sequence[DetectorObservation],
    *,
    resamples: int,
    confidence: float,
    seed: int,
) -> DetectorMetrics:
    by_case: dict[str, list[DetectorObservation]] = defaultdict(list)
    for observation in observations:
        by_case[observation.case_id].append(observation)
    case_ids = sorted(by_case)
    if not any(observation.is_corrupted for observation in observations):
        raise DetectorEvaluationError(f"{kind.value} has no positive observations")
    if not any(not observation.is_corrupted for observation in observations):
        raise DetectorEvaluationError(f"{kind.value} has no clean observations for AUC")

    point = _metrics(observations)
    rng = random.Random(seed)
    replicates: dict[str, list[float]] = defaultdict(list)
    for _ in range(resamples):
        sample = []
        for _ in case_ids:
            sample.extend(by_case[rng.choice(case_ids)])
        values = _metrics(sample)
        for metric in ("precision", "recall", "f1", "auc", "localisation"):
            value = values[metric]
            if value is not None:
                replicates[metric].append(float(value))

    method = f"case-clustered percentile bootstrap (B={resamples})"
    return DetectorMetrics(
        corruption_type=kind,
        n_cases=len(case_ids),
        n_observations=len(observations),
        confusion=point["confusion"],
        precision=_interval(point["precision"], replicates["precision"], confidence, method),
        recall=_interval(point["recall"], replicates["recall"], confidence, method),
        f1=_interval(point["f1"], replicates["f1"], confidence, method),
        auc=_interval(point["auc"], replicates["auc"], confidence, method),
        localisation_accuracy=_interval(
            point["localisation"], replicates["localisation"], confidence, method
        ),
    )


def _metrics(observations: Sequence[DetectorObservation]) -> dict[str, Any]:
    tp = sum(observation.is_corrupted and observation.predicted for observation in observations)
    fp = sum(not observation.is_corrupted and observation.predicted for observation in observations)
    fn = sum(observation.is_corrupted and not observation.predicted for observation in observations)
    tn = sum(not observation.is_corrupted and not observation.predicted for observation in observations)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    detected = [
        observation
        for observation in observations
        if observation.is_corrupted and observation.predicted
    ]
    localisation = (
        sum(
            observation.ground_truth_turn_index in observation.predicted_turns
            for observation in detected
        )
        / len(detected)
        if detected
        else 0.0
    )
    return {
        "confusion": ConfusionMatrix(tp, fp, fn, tn),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "auc": _auc(observations),
        "localisation": localisation,
    }


def _auc(observations: Sequence[DetectorObservation]) -> float | None:
    positive = [observation.score for observation in observations if observation.is_corrupted]
    negative = [observation.score for observation in observations if not observation.is_corrupted]
    if not positive or not negative:
        return None
    wins = 0.0
    for positive_score in positive:
        for negative_score in negative:
            if positive_score > negative_score:
                wins += 1.0
            elif positive_score == negative_score:
                wins += 0.5
    return wins / (len(positive) * len(negative))


def _interval(
    estimate: float | None,
    replicates: Sequence[float],
    confidence: float,
    method: str,
) -> Interval:
    if estimate is None:
        raise DetectorEvaluationError("point estimate is undefined")
    if not replicates:
        # A tiny case bootstrap can sample only one class every time. The point
        # remains defined; reporting a zero-width interval is clearer than
        # silently dropping the metric.
        replicates = [estimate]
    ordered = sorted(replicates)
    tail = (1.0 - confidence) / 2.0
    return Interval(
        estimate=float(estimate),
        low=_quantile(ordered, tail),
        high=_quantile(ordered, 1.0 - tail),
        method=method,
        confidence=confidence,
    )


def _quantile(values: Sequence[float], q: float) -> float:
    if len(values) == 1:
        return values[0]
    position = q * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


__all__ = [
    "ConfusionMatrix",
    "DetectorEvaluationError",
    "DetectorMetrics",
    "DetectorObservation",
    "DetectorReport",
    "evaluate_detector",
]
