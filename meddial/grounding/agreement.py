"""Chance-corrected agreement between independent extractor families (GRND-3)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from statistics import fmean
from typing import Any

from meddial.grounding.matcher import Matcher


class AgreementError(ValueError):
    """Extraction records do not form a valid cross-family comparison."""


@dataclass(frozen=True)
class FamilyExtraction:
    """One model family's extracted field sets for one case."""

    case_id: str
    model_family: str
    diagnoses: tuple[str, ...] = ()
    medications: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldAgreement:
    field_group: str
    families: tuple[str, ...]
    n_cases: int
    n_units: int
    observed_agreement: float
    expected_agreement: float
    fleiss_kappa: float | None
    mean_pairwise_jaccard: float

    def as_record(self) -> dict[str, Any]:
        return {
            "field_group": self.field_group,
            "families": list(self.families),
            "n_cases": self.n_cases,
            "n_units": self.n_units,
            "observed_agreement": self.observed_agreement,
            "expected_agreement": self.expected_agreement,
            "fleiss_kappa": self.fleiss_kappa,
            "mean_pairwise_jaccard": self.mean_pairwise_jaccard,
        }


@dataclass(frozen=True)
class ExtractionAgreement:
    diagnoses: FieldAgreement
    medications: FieldAgreement

    def as_record(self) -> dict[str, Any]:
        return {
            "diagnoses": self.diagnoses.as_record(),
            "medications": self.medications.as_record(),
        }


def measure_extraction_agreement(
    records: Sequence[FamilyExtraction],
    *,
    diagnosis_matcher: Matcher,
    medication_matcher: Matcher,
) -> ExtractionAgreement:
    """Measure agreement on the same cases extracted by at least two families.

    Each statistical unit is ``case × normalised entity``.  A family rates that
    unit present or absent.  Fleiss' kappa then corrects the observed pairwise
    agreement for the positive/negative prevalence.  Jaccard is returned as an
    intuitive, non-chance-corrected companion rather than substituted for it.
    """

    matrix, families, case_ids = _complete_matrix(records)
    return ExtractionAgreement(
        diagnoses=_measure_field(
            "diagnoses",
            matrix,
            families,
            case_ids,
            normalise=lambda value: diagnosis_matcher.normalise(value).text,
        ),
        medications=_measure_field(
            "medications",
            matrix,
            families,
            case_ids,
            normalise=lambda value: medication_matcher.normalise(value).text,
        ),
    )


def _measure_field(
    field_group: str,
    matrix: Mapping[tuple[str, str], FamilyExtraction],
    families: tuple[str, ...],
    case_ids: tuple[str, ...],
    *,
    normalise: Any,
) -> FieldAgreement:
    sets: dict[tuple[str, str], frozenset[str]] = {}
    for case_id in case_ids:
        for family in families:
            values = getattr(matrix[(case_id, family)], field_group)
            sets[(case_id, family)] = frozenset(
                value for raw in values if (value := normalise(raw))
            )

    ratings: list[list[int]] = []
    jaccards: list[float] = []
    for case_id in case_ids:
        universe = sorted(
            set().union(*(sets[(case_id, family)] for family in families))
        )
        for entity in universe:
            ratings.append(
                [int(entity in sets[(case_id, family)]) for family in families]
            )
        for left, right in combinations(families, 2):
            jaccards.append(_jaccard(sets[(case_id, left)], sets[(case_id, right)]))

    if not ratings:
        return FieldAgreement(
            field_group=field_group,
            families=families,
            n_cases=len(case_ids),
            n_units=0,
            observed_agreement=1.0,
            expected_agreement=1.0,
            fleiss_kappa=None,
            mean_pairwise_jaccard=fmean(jaccards) if jaccards else 1.0,
        )

    rater_count = len(families)
    pair_count = rater_count * (rater_count - 1) / 2
    unit_agreement = []
    positives = 0
    for unit in ratings:
        positive = sum(unit)
        negative = rater_count - positive
        positives += positive
        agreeing_pairs = positive * (positive - 1) / 2 + negative * (negative - 1) / 2
        unit_agreement.append(agreeing_pairs / pair_count)

    observed = fmean(unit_agreement)
    positive_rate = positives / (len(ratings) * rater_count)
    expected = positive_rate**2 + (1.0 - positive_rate) ** 2
    denominator = 1.0 - expected
    kappa = (observed - expected) / denominator if denominator > 1e-12 else None
    return FieldAgreement(
        field_group=field_group,
        families=families,
        n_cases=len(case_ids),
        n_units=len(ratings),
        observed_agreement=observed,
        expected_agreement=expected,
        fleiss_kappa=kappa,
        mean_pairwise_jaccard=fmean(jaccards),
    )


def _complete_matrix(
    records: Sequence[FamilyExtraction],
) -> tuple[
    dict[tuple[str, str], FamilyExtraction], tuple[str, ...], tuple[str, ...]
]:
    if not records:
        raise AgreementError("no extraction records were supplied")
    families = tuple(sorted({record.model_family for record in records}))
    case_ids = tuple(sorted({record.case_id for record in records}))
    if len(families) < 2:
        raise AgreementError("GRND-3 requires at least two model families")
    matrix: dict[tuple[str, str], FamilyExtraction] = {}
    for record in records:
        key = (record.case_id, record.model_family)
        if key in matrix:
            raise AgreementError(
                f"duplicate extraction for case {record.case_id!r}, "
                f"family {record.model_family!r}"
            )
        matrix[key] = record
    missing = [
        (case_id, family)
        for case_id in case_ids
        for family in families
        if (case_id, family) not in matrix
    ]
    if missing:
        preview = ", ".join(f"{case}/{family}" for case, family in missing[:5])
        raise AgreementError(
            f"families must extract the same cases; missing {preview}"
        )
    return matrix, families, case_ids


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


__all__ = [
    "AgreementError",
    "ExtractionAgreement",
    "FamilyExtraction",
    "FieldAgreement",
    "measure_extraction_agreement",
]
