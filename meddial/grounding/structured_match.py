"""Compare extracted clinical entities with coded ground truth (GRND-1/2).

The matcher itself only answers whether two strings agree.  This module adds
the study semantics around that primitive:

* matches are one-to-one, so repeating one correct diagnosis cannot inflate
  recall or precision;
* counts are kept per case, and intervals resample cases rather than entities;
* the frozen matcher's independently measured fixture error accompanies every
  aggregate result.

The code never assumes that a low coded match rate means extraction failure.
It records unmatched strings and match granularity so disagreement between
clinical narrative and billing-oriented coding remains inspectable.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from meddial.grounding.evaluate import MatcherErrorRate
from meddial.grounding.matcher import CodedEntity, Matcher, MatchResult
from meddial.grounding.spec import EntityKind, Granularity, ensure_frozen_before
from meddial.stats import Interval


class GroundingError(ValueError):
    """Inputs cannot support an auditable structured comparison."""


@dataclass(frozen=True)
class ExtractedEntity:
    """One entity produced by an extractor.

    ``code`` is optional and is only used when the extractor actually emitted
    it.  Codes are never inferred from descriptions, which would make an exact
    code match circular.
    """

    text: str
    code: str = ""

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise GroundingError("an extracted entity cannot be empty")


@dataclass(frozen=True)
class ExtractedCase:
    case_id: str
    diagnoses: tuple[ExtractedEntity, ...] = ()
    medications: tuple[ExtractedEntity, ...] = ()


@dataclass(frozen=True)
class CodedCase:
    case_id: str
    diagnoses: tuple[CodedEntity, ...] = ()
    medications: tuple[CodedEntity, ...] = ()


@dataclass(frozen=True)
class EntityPair:
    extracted: ExtractedEntity
    coded: CodedEntity
    result: MatchResult


@dataclass(frozen=True)
class FieldCaseResult:
    """One field group's one-to-one matches for a single case."""

    matches: tuple[EntityPair, ...]
    unmatched_extracted: tuple[ExtractedEntity, ...]
    unmatched_coded: tuple[CodedEntity, ...]

    @property
    def true_positives(self) -> int:
        return len(self.matches)

    @property
    def false_positives(self) -> int:
        return len(self.unmatched_extracted)

    @property
    def false_negatives(self) -> int:
        return len(self.unmatched_coded)

    @property
    def precision(self) -> float:
        return _precision(self.true_positives, self.false_positives)

    @property
    def recall(self) -> float:
        return _recall(self.true_positives, self.false_negatives)

    @property
    def f1(self) -> float:
        return _f1(self.true_positives, self.false_positives, self.false_negatives)


@dataclass(frozen=True)
class CaseGroundingResult:
    case_id: str
    diagnoses: FieldCaseResult
    medications: FieldCaseResult


@dataclass(frozen=True)
class FieldGroundingSummary:
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: Interval
    recall: Interval
    f1: Interval
    by_granularity: Mapping[Granularity, int] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return {
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "precision": self.precision.as_record(),
            "recall": self.recall.as_record(),
            "f1": self.f1.as_record(),
            "by_granularity": {
                granularity.value: count
                for granularity, count in sorted(
                    self.by_granularity.items(), key=lambda item: item[0].value
                )
            },
        }


@dataclass(frozen=True)
class GroundingReport:
    """GRND-1/2 result with the instrument validation attached."""

    cases: tuple[CaseGroundingResult, ...]
    diagnoses: FieldGroundingSummary
    medications: FieldGroundingSummary
    matcher_error_rates: Mapping[EntityKind, MatcherErrorRate]

    @property
    def n_cases(self) -> int:
        return len(self.cases)

    def as_record(self) -> dict[str, Any]:
        return {
            "n_cases": self.n_cases,
            "diagnoses": self.diagnoses.as_record(),
            "medications": self.medications.as_record(),
            "matcher_validation": {
                kind.value: {
                    "matcher": rate.matcher_key,
                    "spec_hash": rate.spec_hash,
                    "fixture": rate.fixture_id,
                    "precision": rate.precision,
                    "recall": rate.recall,
                    "f1": rate.f1,
                    "error_rate": 1.0 - rate.f1,
                    "n_cases": rate.n_cases,
                    "summary": rate.summary(),
                }
                for kind, rate in sorted(
                    self.matcher_error_rates.items(), key=lambda item: item[0].value
                )
            },
        }


def match_field(
    extracted: Sequence[ExtractedEntity | str],
    coded: Sequence[CodedEntity],
    matcher: Matcher,
) -> FieldCaseResult:
    """Find a deterministic maximum-cardinality one-to-one matching.

    A standard augmenting-path bipartite matching is used rather than choosing
    each extraction's best row independently.  The latter lets several surface
    forms consume the same coded row and overstates accuracy.
    """

    extracted_entities = tuple(_as_extracted(value) for value in extracted)
    coded_entities = tuple(coded)
    candidates: dict[int, list[tuple[int, MatchResult]]] = {}
    pair_results: dict[tuple[int, int], MatchResult] = {}
    for extracted_index, entity in enumerate(extracted_entities):
        available: list[tuple[int, MatchResult]] = []
        for coded_index, coded_entity in enumerate(coded_entities):
            result = matcher.match_one(
                entity.text, coded_entity, extracted_code=entity.code
            )
            if result.matched:
                available.append((coded_index, result))
                pair_results[(extracted_index, coded_index)] = result
        available.sort(key=lambda item: (-item[1].rank, -item[1].score, item[0]))
        candidates[extracted_index] = available

    # Constrained entities go first.  Augmenting paths still reassign an
    # earlier entity when doing so permits one more total match.
    order = sorted(candidates, key=lambda index: (len(candidates[index]), index))
    coded_owner: dict[int, int] = {}

    def augment(extracted_index: int, seen: set[int]) -> bool:
        for coded_index, _ in candidates[extracted_index]:
            if coded_index in seen:
                continue
            seen.add(coded_index)
            owner = coded_owner.get(coded_index)
            if owner is None or augment(owner, seen):
                coded_owner[coded_index] = extracted_index
                return True
        return False

    for extracted_index in order:
        augment(extracted_index, set())

    extracted_to_coded = {owner: coded for coded, owner in coded_owner.items()}
    pairs = tuple(
        EntityPair(
            extracted=extracted_entities[extracted_index],
            coded=coded_entities[coded_index],
            result=pair_results[(extracted_index, coded_index)],
        )
        for extracted_index, coded_index in sorted(extracted_to_coded.items())
    )
    return FieldCaseResult(
        matches=pairs,
        unmatched_extracted=tuple(
            entity
            for index, entity in enumerate(extracted_entities)
            if index not in extracted_to_coded
        ),
        unmatched_coded=tuple(
            entity
            for index, entity in enumerate(coded_entities)
            if index not in coded_owner
        ),
    )


def match_case(
    extracted: ExtractedCase,
    coded: CodedCase,
    *,
    diagnosis_matcher: Matcher,
    medication_matcher: Matcher,
) -> CaseGroundingResult:
    if extracted.case_id != coded.case_id:
        raise GroundingError(
            f"case mismatch: extraction {extracted.case_id!r}, coded {coded.case_id!r}"
        )
    _require_kind(diagnosis_matcher, EntityKind.DIAGNOSIS)
    _require_kind(medication_matcher, EntityKind.MEDICATION)
    return CaseGroundingResult(
        case_id=extracted.case_id,
        diagnoses=match_field(extracted.diagnoses, coded.diagnoses, diagnosis_matcher),
        medications=match_field(
            extracted.medications, coded.medications, medication_matcher
        ),
    )


def evaluate_structured_matches(
    extractions: Sequence[ExtractedCase],
    coded_cases: Sequence[CodedCase],
    *,
    diagnosis_matcher: Matcher,
    medication_matcher: Matcher,
    diagnosis_matcher_error: MatcherErrorRate,
    medication_matcher_error: MatcherErrorRate,
    run_started_at: datetime,
    resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
) -> GroundingReport:
    """Aggregate GRND-1/2 with case-clustered intervals.

    Cases present on only one side are retained: an extraction-only case
    contributes false positives, while a coded-only case contributes false
    negatives.  Silently intersecting the identifiers would flatter both.
    """

    if resamples < 1:
        raise GroundingError("resamples must be positive")
    _require_kind(diagnosis_matcher, EntityKind.DIAGNOSIS)
    _require_kind(medication_matcher, EntityKind.MEDICATION)
    _require_validation(diagnosis_matcher, diagnosis_matcher_error)
    _require_validation(medication_matcher, medication_matcher_error)
    ensure_frozen_before(diagnosis_matcher.spec, run_started_at)
    ensure_frozen_before(medication_matcher.spec, run_started_at)

    extracted_by_case = _index_unique(extractions, "extraction")
    coded_by_case = _index_unique(coded_cases, "coded ground truth")
    case_ids = sorted(set(extracted_by_case) | set(coded_by_case))
    if not case_ids:
        raise GroundingError("no cases were supplied")

    results = []
    for case_id in case_ids:
        extraction = extracted_by_case.get(case_id, ExtractedCase(case_id=case_id))
        coded = coded_by_case.get(case_id, CodedCase(case_id=case_id))
        results.append(
            match_case(
                extraction,
                coded,
                diagnosis_matcher=diagnosis_matcher,
                medication_matcher=medication_matcher,
            )
        )

    return GroundingReport(
        cases=tuple(results),
        diagnoses=_summarise(
            [result.diagnoses for result in results],
            resamples=resamples,
            confidence=confidence,
            seed=seed,
        ),
        medications=_summarise(
            [result.medications for result in results],
            resamples=resamples,
            confidence=confidence,
            seed=seed + 1,
        ),
        matcher_error_rates={
            EntityKind.DIAGNOSIS: diagnosis_matcher_error,
            EntityKind.MEDICATION: medication_matcher_error,
        },
    )


# A concise name for callers that already know they are in the grounding layer.
structured_match = evaluate_structured_matches


def _summarise(
    cases: Sequence[FieldCaseResult],
    *,
    resamples: int,
    confidence: float,
    seed: int,
) -> FieldGroundingSummary:
    counts = [_counts(case) for case in cases]
    tp, fp, fn = _sum_counts(counts)
    rng = random.Random(seed)
    precision_values: list[float] = []
    recall_values: list[float] = []
    f1_values: list[float] = []
    for _ in range(resamples):
        sampled = [rng.choice(counts) for _ in counts]
        sample_tp, sample_fp, sample_fn = _sum_counts(sampled)
        precision_values.append(_precision(sample_tp, sample_fp))
        recall_values.append(_recall(sample_tp, sample_fn))
        f1_values.append(_f1(sample_tp, sample_fp, sample_fn))

    by_granularity: dict[Granularity, int] = {}
    for case in cases:
        for pair in case.matches:
            granularity = pair.result.granularity
            by_granularity[granularity] = by_granularity.get(granularity, 0) + 1

    method = f"case-clustered percentile bootstrap (B={resamples})"
    return FieldGroundingSummary(
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=_interval(
            _precision(tp, fp), precision_values, confidence=confidence, method=method
        ),
        recall=_interval(
            _recall(tp, fn), recall_values, confidence=confidence, method=method
        ),
        f1=_interval(
            _f1(tp, fp, fn), f1_values, confidence=confidence, method=method
        ),
        by_granularity=by_granularity,
    )


def _interval(
    estimate: float,
    values: Sequence[float],
    *,
    confidence: float,
    method: str,
) -> Interval:
    ordered = sorted(values)
    tail = (1.0 - confidence) / 2.0
    return Interval(
        estimate=estimate,
        low=_quantile(ordered, tail),
        high=_quantile(ordered, 1.0 - tail),
        method=method,
        confidence=confidence,
    )


def _quantile(ordered: Sequence[float], q: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _precision(tp: int, fp: int) -> float:
    denominator = tp + fp
    return tp / denominator if denominator else 0.0


def _recall(tp: int, fn: int) -> float:
    denominator = tp + fn
    return tp / denominator if denominator else 0.0


def _f1(tp: int, fp: int, fn: int) -> float:
    precision = _precision(tp, fp)
    recall = _recall(tp, fn)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _counts(result: FieldCaseResult) -> tuple[int, int, int]:
    return result.true_positives, result.false_positives, result.false_negatives


def _sum_counts(counts: Sequence[tuple[int, int, int]]) -> tuple[int, int, int]:
    return (
        sum(count[0] for count in counts),
        sum(count[1] for count in counts),
        sum(count[2] for count in counts),
    )


def _as_extracted(value: ExtractedEntity | str) -> ExtractedEntity:
    return value if isinstance(value, ExtractedEntity) else ExtractedEntity(text=value)


def _require_kind(matcher: Matcher, expected: EntityKind) -> None:
    if matcher.spec.entity_kind is not expected:
        raise GroundingError(
            f"{matcher.spec.key} is for {matcher.spec.entity_kind.value}, "
            f"not {expected.value}"
        )


def _require_validation(matcher: Matcher, rate: MatcherErrorRate) -> None:
    if rate.matcher_key != matcher.spec.key or rate.spec_hash != matcher.spec.content_hash:
        raise GroundingError(
            f"validation result {rate.matcher_key}@{rate.spec_hash[:12]} does not "
            f"validate {matcher.spec.key}@{matcher.spec.content_hash[:12]}"
        )


def _index_unique(
    values: Sequence[ExtractedCase] | Sequence[CodedCase], label: str
) -> dict[str, ExtractedCase] | dict[str, CodedCase]:
    indexed: dict[str, Any] = {}
    for value in values:
        if value.case_id in indexed:
            raise GroundingError(f"duplicate {label} case {value.case_id!r}")
        indexed[value.case_id] = value
    return indexed


__all__ = [
    "CaseGroundingResult",
    "CodedCase",
    "EntityPair",
    "ExtractedCase",
    "ExtractedEntity",
    "FieldCaseResult",
    "FieldGroundingSummary",
    "GroundingError",
    "GroundingReport",
    "evaluate_structured_matches",
    "match_case",
    "match_field",
    "structured_match",
]
