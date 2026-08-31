"""Deterministic, auditable cohort selection (COH-3--COH-6)."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from meddial.cohort.criteria import (
    CRITERION_LABELS,
    CRITERION_ORDER,
    DEFAULT_CRITERIA,
    AdmissionRecord,
    CohortCriteria,
    CriteriaEvaluation,
    CriterionCode,
    evaluate_admission,
)

DEFAULT_COHORT_SIZE = 200
DEFAULT_SAMPLING_SEED = 20260914


class CohortSelectionError(ValueError):
    """Base class for deterministic selection failures."""


class DuplicateAdmissionError(CohortSelectionError):
    """The candidate pool contains the same admission more than once."""


class InsufficientEligibleCasesError(CohortSelectionError):
    """The exclusions leave fewer cases than the requested powered cohort."""

    def __init__(self, requested: int, available: int) -> None:
        self.requested = requested
        self.available = available
        super().__init__(f"requested exactly {requested} cases, but only {available} are eligible")


class AuditStatus(str, Enum):
    SELECTED = "selected"
    ELIGIBLE_NOT_SAMPLED = "eligible_not_sampled"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class StageCount:
    """One row in the CONSORT-style sequential exclusion flow."""

    criterion: CriterionCode
    label: str
    entering: int
    excluded: int
    remaining: int

    def to_dict(self) -> dict[str, int | str]:
        return {
            "criterion": self.criterion.value,
            "label": self.label,
            "entering": self.entering,
            "excluded": self.excluded,
            "remaining": self.remaining,
        }


@dataclass(frozen=True)
class CaseAuditRecord:
    """Why one admission was excluded, selected, or left in the pool."""

    subject_id: int
    hadm_id: int
    criteria_version: str
    fired_criteria: tuple[CriterionCode, ...]
    charlson_score: int
    charlson_conditions: tuple[str, ...]
    length_of_stay_days: float
    status: AuditStatus
    selection_key: str | None = None
    selection_rank: int | None = None

    @property
    def excluded(self) -> bool:
        return bool(self.fired_criteria)

    def to_private_dict(self) -> dict[str, object]:
        return {
            "subject_id": self.subject_id,
            "hadm_id": self.hadm_id,
            "criteria_version": self.criteria_version,
            "fired_criteria": [code.value for code in self.fired_criteria],
            "charlson_score": self.charlson_score,
            "charlson_conditions": list(self.charlson_conditions),
            "length_of_stay_days": round(self.length_of_stay_days, 6),
            "status": self.status.value,
            "selection_key": self.selection_key,
            "selection_rank": self.selection_rank,
        }


@dataclass(frozen=True)
class CohortSelection:
    """All deterministic outputs needed to create a run manifest."""

    criteria: CohortCriteria
    source_snapshot_hash: str
    seed: int
    requested_n: int
    candidate_pool_size: int
    eligible_pool_size: int
    selected: tuple[AdmissionRecord, ...]
    audit: tuple[CaseAuditRecord, ...]
    stage_counts: tuple[StageCount, ...]
    cohort_hash: str

    @property
    def n_cases(self) -> int:
        return len(self.selected)


def select_cohort(
    records: Iterable[AdmissionRecord],
    *,
    source_snapshot_hash: str,
    n_cases: int = DEFAULT_COHORT_SIZE,
    seed: int = DEFAULT_SAMPLING_SEED,
    criteria: CohortCriteria = DEFAULT_CRITERIA,
) -> CohortSelection:
    """Apply E1--E10 and take an exactly-sized seeded sample.

    Sampling is SHA-256 ranking over a canonically sorted eligible pool.  It is
    random with respect to the recorded seed but does not depend on input order,
    global RNG state, hash randomisation, or the Python version.
    """
    if not source_snapshot_hash.strip():
        raise CohortSelectionError("source_snapshot_hash must be recorded")
    if n_cases <= 0:
        raise CohortSelectionError("n_cases must be positive")
    criteria.validate()

    candidates = tuple(records)
    _reject_duplicate_admissions(candidates)
    evaluations = [evaluate_admission(record, criteria) for record in candidates]
    evaluations = _apply_one_admission_per_subject(evaluations)
    stage_counts = _build_stage_counts(evaluations)

    eligible = sorted(
        (evaluation for evaluation in evaluations if evaluation.eligible),
        key=lambda item: item.record.identity,
    )
    if len(eligible) < n_cases:
        raise InsufficientEligibleCasesError(n_cases, len(eligible))

    ranked = sorted(
        eligible,
        key=lambda item: (
            _selection_key(item.record, seed, source_snapshot_hash),
            item.record.identity,
        ),
    )
    chosen = ranked[:n_cases]
    chosen_rank = {item.record.identity: rank for rank, item in enumerate(chosen, start=1)}
    eligible_identities = {item.record.identity for item in eligible}

    audit: list[CaseAuditRecord] = []
    for evaluation in sorted(evaluations, key=_evaluation_order):
        identity = evaluation.record.identity
        rank = chosen_rank.get(identity)
        if evaluation.fired:
            status = AuditStatus.EXCLUDED
        elif rank is not None:
            status = AuditStatus.SELECTED
        else:
            status = AuditStatus.ELIGIBLE_NOT_SAMPLED
        audit.append(
            CaseAuditRecord(
                subject_id=evaluation.record.subject_id,
                hadm_id=evaluation.record.hadm_id,
                criteria_version=criteria.version,
                fired_criteria=evaluation.fired,
                charlson_score=evaluation.charlson.score,
                charlson_conditions=evaluation.charlson.conditions,
                length_of_stay_days=evaluation.length_of_stay_days,
                status=status,
                selection_key=(
                    _selection_key(evaluation.record, seed, source_snapshot_hash)
                    if identity in eligible_identities
                    else None
                ),
                selection_rank=rank,
            )
        )

    selected = tuple(item.record for item in chosen)
    cohort_hash = _cohort_hash(
        selected,
        criteria=criteria,
        source_snapshot_hash=source_snapshot_hash,
        seed=seed,
        requested_n=n_cases,
    )
    return CohortSelection(
        criteria=criteria,
        source_snapshot_hash=source_snapshot_hash,
        seed=seed,
        requested_n=n_cases,
        candidate_pool_size=len(candidates),
        eligible_pool_size=len(eligible),
        selected=selected,
        audit=tuple(audit),
        stage_counts=stage_counts,
        cohort_hash=cohort_hash,
    )


def _apply_one_admission_per_subject(
    evaluations: list[CriteriaEvaluation],
) -> list[CriteriaEvaluation]:
    """Apply E10 after E1--E9, keeping the first *qualifying* admission."""
    qualifying: dict[int, list[CriteriaEvaluation]] = defaultdict(list)
    for evaluation in evaluations:
        if evaluation.eligible:
            qualifying[evaluation.record.subject_id].append(evaluation)

    later_identities: set[tuple[int, int]] = set()
    for group in qualifying.values():
        ordered = sorted(
            group,
            key=lambda item: (item.record.admittime, item.record.hadm_id),
        )
        later_identities.update(item.record.identity for item in ordered[1:])

    return [
        evaluation.add(CriterionCode.LATER_QUALIFYING_ADMISSION)
        if evaluation.record.identity in later_identities
        else evaluation
        for evaluation in evaluations
    ]


def _build_stage_counts(
    evaluations: list[CriteriaEvaluation],
) -> tuple[StageCount, ...]:
    """Count sequential exclusions without losing multi-criterion audit detail."""
    remaining = set(range(len(evaluations)))
    rows: list[StageCount] = []
    for criterion in CRITERION_ORDER:
        entering = len(remaining)
        excluded = {index for index in remaining if criterion in evaluations[index].fired}
        remaining -= excluded
        rows.append(
            StageCount(
                criterion=criterion,
                label=CRITERION_LABELS[criterion],
                entering=entering,
                excluded=len(excluded),
                remaining=len(remaining),
            )
        )
    return tuple(rows)


def _selection_key(
    record: AdmissionRecord,
    seed: int,
    source_snapshot_hash: str,
) -> str:
    payload = (
        f"meddial-cohort-v1\0{source_snapshot_hash}\0{seed}\0{record.subject_id}\0{record.hadm_id}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cohort_hash(
    selected: tuple[AdmissionRecord, ...],
    *,
    criteria: CohortCriteria,
    source_snapshot_hash: str,
    seed: int,
    requested_n: int,
) -> str:
    body = {
        "criteria_key": criteria.key,
        "criteria_hash": criteria.content_hash,
        "source_snapshot_hash": source_snapshot_hash,
        "seed": seed,
        "requested_n": requested_n,
        "selected": [{"subject_id": row.subject_id, "hadm_id": row.hadm_id} for row in selected],
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reject_duplicate_admissions(records: tuple[AdmissionRecord, ...]) -> None:
    seen: set[tuple[int, int]] = set()
    duplicates: set[tuple[int, int]] = set()
    for record in records:
        if record.identity in seen:
            duplicates.add(record.identity)
        seen.add(record.identity)
    if duplicates:
        rendered = ", ".join(f"{subject}/{hadm}" for subject, hadm in sorted(duplicates))
        raise DuplicateAdmissionError(f"candidate pool has duplicate admission rows: {rendered}")


def _evaluation_order(evaluation: CriteriaEvaluation) -> tuple[int, datetime, int]:
    return (
        evaluation.record.subject_id,
        evaluation.record.admittime,
        evaluation.record.hadm_id,
    )
