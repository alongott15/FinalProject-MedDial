from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from meddial.benchmarks import (
    CorruptionType,
    DetectorObservation,
    DialoguePolicyRecord,
    RetentionCase,
    case_split,
    evaluate_detector,
    evaluate_policy_discrimination,
    evaluate_retention,
    inject_suite,
    recover_injected_error,
)
from meddial.evaluation import Turn
from meddial.grounding import (
    CodedCase,
    CodedEntity,
    Matcher,
    MatcherFixture,
    MatcherRegistry,
    evaluate_matcher,
)
from meddial.knowledge import StructuredClinicalReference
from meddial.llm import MockProvider

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "meddial"
    / "benchmarks"
    / "fixtures"
    / "synthetic_dialogue.v1.json"
)


def _synthetic_fixture():
    payload = json.loads(FIXTURE.read_text())
    reference = StructuredClinicalReference.model_validate(payload["reference"])
    turns = tuple(Turn(**turn) for turn in payload["dialogue"])
    return payload, reference, turns


def _matchers():
    registry = MatcherRegistry()
    diagnosis = Matcher(registry.get("icd9_diagnosis@1.0"))
    medication = Matcher(registry.get("prescription_medication@1.0"))
    fixtures = {fixture.matcher_key: fixture for fixture in MatcherFixture.load_all()}
    return (
        diagnosis,
        medication,
        evaluate_matcher(diagnosis, fixtures[diagnosis.spec.key]),
        evaluate_matcher(medication, fixtures[medication.spec.key]),
    )


def test_all_seven_injected_faults_are_seeded_and_recoverable():
    _, reference, turns = _synthetic_fixture()
    first = inject_suite(turns, reference, seed=41)
    second = inject_suite(turns, reference, seed=41)
    assert first == second
    assert {result.error.corruption_type for result in first} == set(CorruptionType)
    assert all(recover_injected_error(result) for result in first)
    assert all(result.clean != result.corrupted for result in first)


def test_injected_fault_detected_at_expected_turn():
    observations = []
    for case_number in range(3):
        for kind in CorruptionType:
            observations.extend(
                [
                    DetectorObservation(
                        case_id=f"synthetic-{case_number}",
                        corruption_type=kind,
                        is_corrupted=False,
                        score=0.05,
                        predicted=False,
                    ),
                    DetectorObservation(
                        case_id=f"synthetic-{case_number}",
                        corruption_type=kind,
                        is_corrupted=True,
                        score=0.95,
                        predicted=True,
                        predicted_turns=(1,),
                        ground_truth_turn_index=1,
                    ),
                ]
            )
    report = evaluate_detector(observations, resamples=30, seed=7)
    assert set(report.by_fault_class) == set(CorruptionType)
    for metrics in report.by_fault_class.values():
        assert metrics.f1.estimate == 1.0
        assert metrics.auc.estimate == 1.0
        assert metrics.localisation_accuracy.estimate == 1.0


def test_retention_extractor_prompt_excludes_reference():
    _, _, turns = _synthetic_fixture()
    diagnosis, medication, diagnosis_rate, medication_rate = _matchers()
    secret = "REFERENCE-ONLY-SENTINEL-NEVER-IN-DIALOGUE"
    case = RetentionCase(
        case_id="synthetic-a",
        policy_id="FULL@2.0",
        turns=turns,
        coded=CodedCase(
            case_id="synthetic-a",
            diagnoses=(CodedEntity(secret, "9999"),),
            medications=(CodedEntity("Furosemide"),),
        ),
        generator_families=frozenset({"mistral", "gemma"}),
    )
    provider = MockProvider(
        ['{"diagnoses": [], "medications": ["Lasix"]}'],
        model_family="llama",
    )
    report = evaluate_retention(
        [case],
        provider=provider,
        diagnosis_matcher=diagnosis,
        medication_matcher=medication,
        diagnosis_matcher_error=diagnosis_rate,
        medication_matcher_error=medication_rate,
        run_started_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        resamples=10,
    )
    rendered = "\n".join(
        message.content for call in provider.calls for message in call.messages
    )
    assert secret not in rendered
    assert report.by_policy["FULL@2.0"].grounding.medications.f1.estimate == 1.0


def test_retention_family_must_differ_from_generator():
    _, _, turns = _synthetic_fixture()
    diagnosis, medication, diagnosis_rate, medication_rate = _matchers()
    case = RetentionCase(
        case_id="synthetic-a",
        policy_id="FULL@2.0",
        turns=turns,
        coded=CodedCase(case_id="synthetic-a"),
        generator_families=frozenset({"llama"}),
    )
    with pytest.raises(ValueError, match="independent family"):
        evaluate_retention(
            [case],
            provider=MockProvider(model_family="llama"),
            diagnosis_matcher=diagnosis,
            medication_matcher=medication,
            diagnosis_matcher_error=diagnosis_rate,
            medication_matcher_error=medication_rate,
            run_started_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            resamples=10,
        )


def test_discriminator_split_is_by_case():
    records = [
        DialoguePolicyRecord(
            case_id=f"synthetic-{case}",
            policy_id=policy,
            text=f"shared case words {marker} {marker} {case}",
        )
        for case in range(12)
        for policy, marker in (
            ("FULL", "diagnosis medication treatment"),
            ("NO_DIAGNOSIS", "symptoms questions uncertainty"),
            ("NO_DIAGNOSIS_NO_TREATMENT", "symptoms only unsure"),
        )
    ]
    split = case_split(records, seed=9)
    assert split.train_case_ids.isdisjoint(split.test_case_ids)
    for case_id in {record.case_id for record in records}:
        locations = {
            "train" if record in split.train else "test"
            for record in records
            if record.case_id == case_id
        }
        assert len(locations) == 1

    report = evaluate_policy_discrimination(records, seed=9, top_k=3)
    assert report.macro_auc > 0.9
    assert set(report.top_features) == {
        "FULL",
        "NO_DIAGNOSIS",
        "NO_DIAGNOSIS_NO_TREATMENT",
    }


def test_public_fixture_contains_no_real_identifiers():
    payload = json.loads(FIXTURE.read_text())
    body = FIXTURE.read_text()
    assert payload["synthetic"] is True
    assert "mimic" not in body.lower()
    assert "subject_id" not in body
    assert "hadm_id" not in body
    assert not re.search(r"\b\d{5,}\b", body)
