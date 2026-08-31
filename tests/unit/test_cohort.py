"""W5: structured, reproducible, review-free cohort selection."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from meddial.cohort import (
    DEFAULT_CRITERIA_SQL,
    AdmissionRecord,
    AuditStatus,
    CriterionCode,
    InsufficientEligibleCasesError,
    create_private_manifest,
    create_release_manifest,
    evaluate_admission,
    select_cohort,
    verify_manifest,
)


def _candidate(
    subject_id: int,
    hadm_id: int,
    *,
    day: int = 1,
    age: float = 60,
    los_days: float = 2,
    admission_type: str = "ELECTIVE",
    icu: bool = False,
    icu_days: float | None = None,
    died: bool = False,
    procedures: tuple[str, ...] = (),
    diagnoses: tuple[str, ...] = (),
    note_text: str = "x" * 600,
) -> AdmissionRecord:
    admitted = datetime(2012, 1, day, 10, 0, 0, tzinfo=timezone.utc)
    return AdmissionRecord(
        subject_id=subject_id,
        hadm_id=hadm_id,
        admittime=admitted,
        dischtime=admitted + timedelta(days=los_days),
        age_years=age,
        admission_type=admission_type,
        has_icu_stay=icu,
        # Default a flagged ICU stay to one that actually trips E1.
        icu_days=(icu_days if icu_days is not None else (1.0 if icu else 0.0)),
        hospital_expire_flag=died,
        procedure_icd9_codes=procedures,
        diagnosis_icd9_codes=diagnoses,
        note_text=note_text,
        note_category="Discharge summary",
    )


def test_cohort_hash_stable_across_runs() -> None:
    candidates = [_candidate(index, 10_000 + index) for index in range(1, 11)]

    first = select_cohort(
        candidates,
        source_snapshot_hash="snapshot-a",
        n_cases=5,
        seed=17,
    )
    second = select_cohort(
        reversed(candidates),
        source_snapshot_hash="snapshot-a",
        n_cases=5,
        seed=17,
    )

    assert first.cohort_hash == second.cohort_hash
    assert [row.identity for row in first.selected] == [row.identity for row in second.selected]
    assert create_private_manifest(first) == create_private_manifest(second)


def test_appendix_a_case_is_excluded() -> None:
    """Subject 10446's CHF/PVD/TIA/diabetes admission cannot enter the cohort."""
    appendix_a_case = _candidate(
        10446,
        900001,  # synthetic admission id; only the plan-named subject is retained
        admission_type="URGENT",
        diagnoses=("428.0", "427.31", "443.9", "435.9", "250.00"),
    )

    result = evaluate_admission(appendix_a_case)

    assert CriterionCode.HIGH_ACUITY_DIAGNOSIS in result.fired
    assert CriterionCode.CHARLSON_COMORBIDITY in result.fired
    assert result.charlson.score == 4
    assert not result.eligible


@pytest.mark.parametrize(
    ("candidate", "criterion"),
    [
        (_candidate(1, 101, procedures=("96.70",)), CriterionCode.MECHANICAL_VENTILATION),
        (_candidate(2, 201, age=11), CriterionCode.PAEDIATRIC_OR_NEWBORN),
        (_candidate(3, 301, age=90), CriterionCode.AGE_90_OR_OVER),
        (_candidate(4, 401, note_text="too short"), CriterionCode.INSUFFICIENT_NOTE),
    ],
)
def test_remaining_structured_exclusions_fire(
    candidate: AdmissionRecord,
    criterion: CriterionCode,
) -> None:
    assert criterion in evaluate_admission(candidate).fired


@pytest.mark.parametrize(
    ("age", "excluded"),
    [(11, True), (12, False), (17, False), (89, False), (90, True)],
)
def test_the_age_band_is_twelve_inclusive_to_ninety_exclusive(
    age: int, excluded: bool
) -> None:
    """Criteria 1.1 admits [12, 90). E4 holds the floor, E5 the ceiling.

    Pinned at the boundaries because both are off-by-one prone and the floor
    has already moved once: a threshold that drifts silently invalidates every
    cohort selected under the old one.
    """
    fired = evaluate_admission(_candidate(9, 901, age=age)).fired
    age_criteria = {
        CriterionCode.PAEDIATRIC_OR_NEWBORN,
        CriterionCode.AGE_90_OR_OVER,
    }
    assert bool(age_criteria & set(fired)) is excluded


def test_a_newborn_admission_is_excluded_by_type_not_only_by_age() -> None:
    """MIMIC neonates carry shifted dates, so the type is checked as well."""
    newborn = _candidate(10, 1001, age=30, admission_type="NEWBORN")

    assert CriterionCode.PAEDIATRIC_OR_NEWBORN in evaluate_admission(newborn).fired


@pytest.mark.parametrize(
    ("icu_days", "excluded"),
    [(0.0, False), (0.5, False), (0.99, False), (1.0, True), (1.69, True), (21.5, True)],
)
def test_e1_bounds_icu_duration_rather_than_its_existence(
    icu_days: float, excluded: bool
) -> None:
    """Criteria 1.2 excludes an ICU stay of a day or more, not any ICU stay.

    An ICU bed overnight and critical illness are different claims: a fifth of
    MIMIC ICU stays are under a day, many protocol-driven post-operative
    observation. The clinical acuity filter is E6 and E8; E1 bounds how long
    intensive care was needed.
    """
    candidate = _candidate(7, 701, icu=True, icu_days=icu_days)

    fired = evaluate_admission(candidate).fired
    assert (CriterionCode.ICU_STAY in fired) is excluded


def test_an_icu_stay_of_unknown_duration_fails_e1() -> None:
    """Unknown cannot be shown to be brief, so it does not pass as brief."""
    candidate = _candidate(8, 801, icu=True, icu_days=float("inf"))

    assert CriterionCode.ICU_STAY in evaluate_admission(candidate).fired


def test_an_admission_with_no_icu_stay_never_fires_e1() -> None:
    """The threshold must not turn a zero-day non-stay into an exclusion."""
    candidate = _candidate(8, 802, icu=False)

    assert CriterionCode.ICU_STAY not in evaluate_admission(candidate).fired


def test_appendix_a_case_stays_excluded_under_the_graded_e1() -> None:
    """Relaxing E1 must not readmit the case the PRD holds up as wrong.

    Subject 10446's three admissions carry ICU stays of 1.69, 2.88 and 21.48
    days in MIMIC-III v1.4, so every one of them still trips E1 on duration
    alone -- before E2, E6 or E8 are consulted.
    """
    for icu_days in (1.69, 2.88, 21.48):
        case = _candidate(10446, 196578, icu=True, icu_days=icu_days)
        assert CriterionCode.ICU_STAY in evaluate_admission(case).fired


def test_one_admission_per_subject() -> None:
    earlier = _candidate(1, 101, day=1)
    later = _candidate(1, 102, day=4)
    other_subject = _candidate(2, 201, day=2)

    selection = select_cohort(
        [later, other_subject, earlier],
        source_snapshot_hash="snapshot-a",
        n_cases=2,
        seed=9,
    )

    assert {row.identity for row in selection.selected} == {(1, 101), (2, 201)}
    assert len({row.subject_id for row in selection.selected}) == selection.n_cases
    later_audit = next(row for row in selection.audit if row.hadm_id == 102)
    assert later_audit.fired_criteria == (CriterionCode.LATER_QUALIFYING_ADMISSION,)


def test_no_reviewer_file_required() -> None:
    """The complete selection/manifest flow has no reviewer input or state."""
    selection_parameters = inspect.signature(select_cohort).parameters
    manifest_parameters = inspect.signature(create_private_manifest).parameters
    assert not any("review" in name.casefold() for name in selection_parameters)
    assert not any("review" in name.casefold() for name in manifest_parameters)

    selection = select_cohort(
        [_candidate(1, 101)],
        source_snapshot_hash="snapshot-a",
        n_cases=1,
    )
    manifest = create_private_manifest(selection)
    verify_manifest(manifest)
    assert manifest["n_cases"] == 1


def test_exact_sample_size_and_insufficient_pool_fail_closed() -> None:
    candidates = [_candidate(index, 100 + index) for index in range(1, 5)]
    selection = select_cohort(
        candidates,
        source_snapshot_hash="snapshot-a",
        n_cases=3,
        seed=3,
    )
    assert selection.n_cases == 3

    with pytest.raises(InsufficientEligibleCasesError, match="exactly 5"):
        select_cohort(
            candidates,
            source_snapshot_hash="snapshot-a",
            n_cases=5,
            seed=3,
        )


def test_per_case_audit_and_sequential_stage_counts() -> None:
    multi_excluded = _candidate(1, 101, icu=True, died=True)
    long_stay = _candidate(2, 201, los_days=8)
    eligible = [_candidate(3, 301), _candidate(4, 401)]

    selection = select_cohort(
        [multi_excluded, long_stay, *eligible],
        source_snapshot_hash="snapshot-a",
        n_cases=1,
    )

    audit = next(row for row in selection.audit if row.hadm_id == 101)
    assert audit.status is AuditStatus.EXCLUDED
    assert audit.fired_criteria[:2] == (
        CriterionCode.ICU_STAY,
        CriterionCode.IN_HOSPITAL_DEATH,
    )
    counts = {row.criterion: row for row in selection.stage_counts}
    assert counts[CriterionCode.ICU_STAY].excluded == 1
    # The same admission has already left the sequential flow at E1.
    assert counts[CriterionCode.IN_HOSPITAL_DEATH].excluded == 0
    assert counts[CriterionCode.LENGTH_OF_STAY].excluded == 1
    assert selection.eligible_pool_size == 2


def test_salted_release_manifest_contains_no_source_identifiers() -> None:
    selection = select_cohort(
        [_candidate(10446, 900001), _candidate(10447, 900002)],
        source_snapshot_hash="restricted-snapshot-hash",
        n_cases=2,
        seed=8,
    )
    private = create_private_manifest(selection)
    release_a = create_release_manifest(
        private,
        publication_salt="publication-secret-a",
    )
    release_b = create_release_manifest(
        private,
        publication_salt="publication-secret-b",
    )

    verify_manifest(release_a)
    rendered = json.dumps(release_a, sort_keys=True)
    assert "10446" not in rendered
    assert "900001" not in rendered
    assert "restricted-snapshot-hash" not in rendered
    assert "subject_id" not in rendered
    assert "hadm_id" not in rendered
    assert "row_id" not in rendered
    assert "cohort_hash" not in release_a
    assert release_a["selected"][0]["study_id"] != release_b["selected"][0]["study_id"]


def test_versioned_sql_uses_every_structured_source() -> None:
    sql_path = Path(DEFAULT_CRITERIA_SQL)
    sql = sql_path.read_text().casefold()

    assert sql_path.name == "criteria_v1.sql"
    for table in (
        "icustays",
        "admissions",
        "patients",
        "procedures_icd",
        "diagnoses_icd",
        "noteevents",
    ):
        assert table in sql
    for code in range(1, 11):
        assert f"e{code}" in sql
