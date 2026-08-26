from meddial.cohort import (
    classify_lower_acuity_candidate,
    create_cohort_manifest,
    create_release_manifest,
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


def test_v3_excludes_stemi_even_when_mild_term_is_present():
    result = classify_lower_acuity_candidate("Mild nausea during an acute STEMI.")
    assert not result.passed
    assert result.filter_version == "lexical-v3"


def test_v3_excludes_minor_and_hospital_death_from_metadata():
    assert not classify_lower_acuity_candidate("Sore throat", metadata={"age": 16}).passed
    assert not classify_lower_acuity_candidate(
        "Sore throat", metadata={"age": 40, "hospital_expire_flag": True}
    ).passed


def test_sampling_and_manifest_are_deterministic():
    notes = [
        {"subject_id": index, "hadm_id": index + 100, "row_id": index + 1000} for index in range(20)
    ]
    first = deterministic_sample(notes, 5, seed=17)
    second = deterministic_sample(list(reversed(notes)), 5, seed=17)
    assert first == second
    assert create_cohort_manifest(first, 17, "fixture") == create_cohort_manifest(
        second, 17, "fixture"
    )


def test_sampling_uses_one_admission_per_patient():
    notes = [
        {"subject_id": 1, "hadm_id": 10, "row_id": 100},
        {"subject_id": 1, "hadm_id": 11, "row_id": 101},
        {"subject_id": 2, "hadm_id": 20, "row_id": 200},
    ]
    selected = deterministic_sample(notes, 3, seed=4)
    assert len(selected) == 2
    assert len({note["subject_id"] for note in selected}) == 2


def test_release_manifest_contains_no_mimic_identifiers():
    private = create_cohort_manifest(
        [{"subject_id": 1, "hadm_id": 2, "row_id": 3}], 42, "private/path"
    )
    release = create_release_manifest(private, publication_salt="a-long-private-salt")
    serialized = str(release)
    assert "subject_id" not in serialized
    assert "hadm_id" not in serialized
    assert "row_id" not in serialized
    assert release["publishable"] is True


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
