from meddial.cohort import (
    classify_lower_acuity_candidate,
    create_cohort_manifest,
    deterministic_sample,
)
from Utils.csv_data_loader import CSVDataLoader


def test_broad_pain_term_no_longer_accidentally_includes_note():
    result = classify_lower_acuity_candidate("Patient reports severe chest pain.")
    assert not result.passed


def test_specific_lower_acuity_term_is_eligible_but_not_declared_primary_care():
    result = classify_lower_acuity_candidate("Three days of a sore throat.")
    assert result.passed
    assert "severity is not established" in result.reason


def test_exclusion_overrides_inclusion():
    result = classify_lower_acuity_candidate("Cough with sepsis requiring ICU admission.")
    assert not result.passed
    assert result.matched_exclusions


def test_sampling_and_manifest_are_deterministic():
    notes = [
        {"subject_id": index, "hadm_id": index + 100, "row_id": index + 1000}
        for index in range(20)
    ]
    first = deterministic_sample(notes, 5, seed=17)
    second = deterministic_sample(list(reversed(notes)), 5, seed=17)
    assert first == second
    assert create_cohort_manifest(first, 17, "fixture") == create_cohort_manifest(
        second, 17, "fixture"
    )


def test_fetch_notes_with_light_case_filter_never_returns_rejected_notes(tmp_path):
    loader = object.__new__(CSVDataLoader)
    loader.csv_dir = tmp_path
    loader.fetch_notes = lambda **kwargs: [
        {
            "subject_id": 1,
            "hadm_id": 11,
            "row_id": 111,
            "text": "Three days of a sore throat.",
        },
        {
            "subject_id": 2,
            "hadm_id": 22,
            "row_id": 222,
            "text": "Severe chest pain after cardiac arrest.",
        },
    ]
    selected = loader.fetch_notes_with_light_case_filter(limit=2, seed=4)
    assert [note["subject_id"] for note in selected] == [1]
    assert all(note["light_case_filter"]["passed"] for note in selected)
