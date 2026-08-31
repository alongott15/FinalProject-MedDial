from __future__ import annotations

from datetime import datetime, timezone

import pytest

from meddial.grounding import (
    AgreementError,
    CodedCase,
    CodedEntity,
    EntityKind,
    ExtractedCase,
    ExtractedEntity,
    FamilyExtraction,
    Granularity,
    Matcher,
    MatcherFixture,
    MatcherNotFrozenError,
    MatcherRegistry,
    evaluate_matcher,
    evaluate_structured_matches,
    match_field,
    measure_extraction_agreement,
)


@pytest.fixture(scope="module")
def instruments():
    registry = MatcherRegistry()
    diagnosis = Matcher(registry.get("icd9_diagnosis@1.0"))
    medication = Matcher(registry.get("prescription_medication@1.0"))
    fixtures = {fixture.matcher_key: fixture for fixture in MatcherFixture.load_all()}
    return diagnosis, medication, fixtures


def test_matcher_fixture_precision_recall(instruments):
    diagnosis, medication, fixtures = instruments
    diagnosis_rate = evaluate_matcher(diagnosis, fixtures[diagnosis.spec.key])
    medication_rate = evaluate_matcher(medication, fixtures[medication.spec.key])

    # The misses are intentionally retained known-hard cases, so the fixture
    # measures the matcher instead of merely restating its lookup tables.
    assert diagnosis_rate.precision == 1.0
    assert diagnosis_rate.recall == pytest.approx(14 / 18)
    assert medication_rate.precision == 1.0
    assert medication_rate.recall == pytest.approx(15 / 17)


def test_generic_brand_mapping(instruments):
    _, medication, _ = instruments
    result = medication.match_one("Lasix 40 mg PO", CodedEntity("Furosemide"))
    assert result.matched
    assert result.granularity is Granularity.GENERIC_EQUIVALENT


def test_matcher_version_frozen_before_study_run(instruments):
    diagnosis, medication, fixtures = instruments
    diagnosis_rate = evaluate_matcher(diagnosis, fixtures[diagnosis.spec.key])
    medication_rate = evaluate_matcher(medication, fixtures[medication.spec.key])
    extraction = ExtractedCase(case_id="c1")
    coded = CodedCase(case_id="c1")

    with pytest.raises(MatcherNotFrozenError):
        evaluate_structured_matches(
            [extraction],
            [coded],
            diagnosis_matcher=diagnosis,
            medication_matcher=medication,
            diagnosis_matcher_error=diagnosis_rate,
            medication_matcher_error=medication_rate,
            run_started_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
            resamples=10,
        )


def test_structured_match_is_one_to_one_and_reports_instrument_error(instruments):
    diagnosis, medication, fixtures = instruments
    diagnosis_rate = evaluate_matcher(diagnosis, fixtures[diagnosis.spec.key])
    medication_rate = evaluate_matcher(medication, fixtures[medication.spec.key])
    extraction = ExtractedCase(
        case_id="c1",
        diagnoses=(
            ExtractedEntity("CHF"),
            ExtractedEntity("congestive heart failure"),
        ),
        medications=(ExtractedEntity("Lasix 40mg"),),
    )
    coded = CodedCase(
        case_id="c1",
        diagnoses=(CodedEntity("Congestive heart failure, unspecified", "4280"),),
        medications=(CodedEntity("Furosemide"),),
    )
    report = evaluate_structured_matches(
        [extraction],
        [coded],
        diagnosis_matcher=diagnosis,
        medication_matcher=medication,
        diagnosis_matcher_error=diagnosis_rate,
        medication_matcher_error=medication_rate,
        run_started_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        resamples=20,
    )

    assert report.diagnoses.true_positives == 1
    assert report.diagnoses.false_positives == 1
    assert report.medications.f1.estimate == 1.0
    record = report.as_record()
    assert record["matcher_validation"][EntityKind.DIAGNOSIS.value]["error_rate"] > 0


def test_cross_family_agreement_is_chance_corrected(instruments):
    diagnosis, medication, _ = instruments
    records = [
        FamilyExtraction("c1", "family-a", ("CHF",), ("Lasix",)),
        FamilyExtraction("c1", "family-b", ("congestive heart failure",), ("furosemide",)),
        FamilyExtraction("c1", "family-c", ("CHF", "hypertension"), ("furosemide",)),
        FamilyExtraction("c2", "family-a", ("UTI",), ()),
        FamilyExtraction("c2", "family-b", ("urinary tract infection",), ()),
        FamilyExtraction("c2", "family-c", ("UTI",), ("aspirin",)),
    ]
    report = measure_extraction_agreement(
        records, diagnosis_matcher=diagnosis, medication_matcher=medication
    )
    assert report.diagnoses.fleiss_kappa is not None
    assert -1.0 <= report.diagnoses.fleiss_kappa <= 1.0
    assert report.diagnoses.mean_pairwise_jaccard > 0.5
    assert report.medications.n_units == 2


def test_agreement_requires_same_cases_for_every_family(instruments):
    diagnosis, medication, _ = instruments
    with pytest.raises(AgreementError, match="same cases"):
        measure_extraction_agreement(
            [
                FamilyExtraction("c1", "family-a"),
                FamilyExtraction("c1", "family-b"),
                FamilyExtraction("c2", "family-a"),
            ],
            diagnosis_matcher=diagnosis,
            medication_matcher=medication,
        )


def test_match_field_counts_missing_coded_entity_as_false_negative(instruments):
    diagnosis, _, _ = instruments
    result = match_field(
        ["CHF"],
        [
            CodedEntity("Congestive heart failure, unspecified", "4280"),
            CodedEntity("Atrial fibrillation", "42731"),
        ],
        diagnosis,
    )
    assert (result.true_positives, result.false_positives, result.false_negatives) == (
        1,
        0,
        1,
    )
