"""The E0 driver: corpus loading, re-scoring, resumption, decomposition.

Every case, dialogue and reference in this file is invented. No MIMIC-III
content appears here or may be added (constraint C2).

The properties under test are the ones E0's answer depends on: that both
reference modes see an identical claim set, that an interrupted run resumes
rather than paying twice, that a dialogue whose claims could not be extracted
is reported as INCOMPLETE rather than scored 0, and that the report presents
the decomposition without concluding anything from it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meddial.evaluation import (
    EvaluationStatus,
    ReferenceMode,
    Score,
    ScoreProvenance,
    TurnScope,
)
from meddial.evaluation.acceptance import DOCTOR_FACTUALITY, PATIENT_FACTUALITY
from meddial.experiments import (
    POLICY_ORDER,
    CorpusError,
    ScoredDialogue,
    analyse,
    load_corpus,
    read_results,
    render_report,
    score_corpus,
)
from meddial.knowledge import (
    Core,
    Demographics,
    Diagnosis,
    Medication,
    StructuredClinicalReference,
    Symptom,
)
from meddial.llm import MockProvider

DIALOGUE = [
    {"role": "Doctor", "content": "What brings you in today?"},
    {"role": "Patient", "content": "I have been short of breath for three days."},
    {"role": "Doctor", "content": "Are your ankles swollen as well?"},
    {"role": "Patient", "content": "Yes, and I take metoprolol every morning."},
]

EXTRACTED = json.dumps(
    [
        {
            "turn_index": 1,
            "role": "Patient",
            "type": "patient_fact",
            "text": "The patient has shortness of breath.",
        },
        {
            "turn_index": 2,
            "role": "Doctor",
            "type": "doctor_fact",
            "text": "The patient has ankle swelling.",
        },
    ]
)

SUPPORTED = json.dumps(
    [{"claim_index": 0, "verdict": "supported", "justification": "Core_Fields.Symptoms."}]
)


def _reference() -> StructuredClinicalReference:
    """A synthetic case, deliberately not derived from any real record."""
    return StructuredClinicalReference(
        subject_id=1,
        hadm_id=1,
        core=Core(
            symptoms=[Symptom(description="Shortness of breath", duration="three days")],
            diagnoses=[Diagnosis(primary="Congestive Heart Failure")],
        ),
        context={
            "Patient_Demographics": Demographics(age="64", sex="F"),
            "Current_Medications": [Medication(name="Metoprolol", purpose="rate control")],
        },
    )


def _write_corpus(tmp_path: Path, *, cases: int = 1, policies=POLICY_ORDER) -> tuple[Path, Path]:
    dialogues_path = tmp_path / "dialogues.jsonl"
    references_path = tmp_path / "references.jsonl"

    reference_lines = []
    dialogue_lines = []
    for index in range(cases):
        case_id = f"case-{index}"
        reference_lines.append(
            json.dumps({"case_id": case_id, "reference": _reference().model_dump(mode="json")})
        )
        for policy in policies:
            dialogue_lines.append(
                json.dumps(
                    {
                        "case_id": case_id,
                        "dialogue_id": f"{case_id}-{policy}",
                        "policy": policy,
                        "dialogue": DIALOGUE,
                    }
                )
            )

    references_path.write_text("\n".join(reference_lines) + "\n", encoding="utf-8")
    dialogues_path.write_text("\n".join(dialogue_lines) + "\n", encoding="utf-8")
    return dialogues_path, references_path


def _provenance(mode: ReferenceMode = ReferenceMode.POLICY_CONTEXT) -> ScoreProvenance:
    return ScoreProvenance(
        scorer_id="test",
        model_family="mock",
        model_id="mock-model",
        model_digest="sha256:test",
        quantisation="none",
        reference_mode=mode,
        turn_scope=TurnScope.PATIENT,
        prompt_version="1.0",
        sampling={"temperature": 0.0, "seed": 1},
    )


# --------------------------------------------------------------------------
# Corpus loading
# --------------------------------------------------------------------------


def test_load_corpus_pairs_every_dialogue_with_its_case_reference(tmp_path):
    dialogues_path, references_path = _write_corpus(tmp_path, cases=2)

    records = load_corpus(dialogues_path, references_path)

    assert len(records) == 6
    assert {record.policy_key for record in records} == set(POLICY_ORDER)
    # The three arms of a case must score against one identical reference, or
    # test 1 measures reference drift instead of reference scope.
    assert len({record.case_id for record in records}) == 2


def test_a_dialogue_with_no_reference_is_refused(tmp_path):
    dialogues_path, references_path = _write_corpus(tmp_path)
    with dialogues_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "case_id": "case-unknown",
                    "dialogue_id": "orphan",
                    "policy": "FULL",
                    "dialogue": DIALOGUE,
                }
            )
            + "\n"
        )

    with pytest.raises(CorpusError, match="no reference"):
        load_corpus(dialogues_path, references_path)


def test_a_duplicate_dialogue_id_is_refused(tmp_path):
    dialogues_path, references_path = _write_corpus(tmp_path)
    dialogues_path.write_text(dialogues_path.read_text(encoding="utf-8") * 2, encoding="utf-8")

    with pytest.raises(CorpusError, match="duplicate"):
        load_corpus(dialogues_path, references_path)


def test_an_unknown_policy_is_refused(tmp_path):
    dialogues_path, references_path = _write_corpus(tmp_path, policies=("SOMETHING_ELSE",))

    with pytest.raises(CorpusError, match="not one of"):
        load_corpus(dialogues_path, references_path)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def test_claims_are_extracted_once_and_reused_across_reference_modes(tmp_path):
    """Both modes must see one claim set, or test 1 compares different claims."""
    dialogues_path, references_path = _write_corpus(tmp_path, policies=("FULL",))
    records = load_corpus(dialogues_path, references_path)
    provider = MockProvider([EXTRACTED] + [SUPPORTED] * 4)

    scored = score_corpus(records, provider=provider, seed=1)

    # One extraction, then patient and doctor under each of the two modes.
    assert len(provider.calls) == 5
    assert len(scored) == 2
    assert {result.reference_mode for result in scored} == set(ReferenceMode)


def test_both_roles_are_scored_and_carry_their_own_turn_scope(tmp_path):
    """Confound 2: the thesis scored patient turns only."""
    dialogues_path, references_path = _write_corpus(tmp_path, policies=("FULL",))
    records = load_corpus(dialogues_path, references_path)
    provider = MockProvider([EXTRACTED] + [SUPPORTED] * 4)

    result = score_corpus(records, provider=provider, seed=1)[0]

    assert set(result.scores) == {PATIENT_FACTUALITY, DOCTOR_FACTUALITY}
    assert result.scores[PATIENT_FACTUALITY].provenance.turn_scope is TurnScope.PATIENT
    assert result.scores[DOCTOR_FACTUALITY].provenance.turn_scope is TurnScope.DOCTOR
    assert result.scores[PATIENT_FACTUALITY].provenance.reference_mode is result.reference_mode


def test_failed_extraction_reports_incomplete_not_zero(tmp_path):
    """EVAL-5: a dialogue that could not be measured is not one that scored 0."""
    dialogues_path, references_path = _write_corpus(tmp_path, policies=("FULL",))
    records = load_corpus(dialogues_path, references_path)
    provider = MockProvider(["not json at all"] * 8)

    scored = score_corpus(records, provider=provider, seed=1)

    for result in scored:
        for score in result.scores.values():
            assert score.status is EvaluationStatus.INCOMPLETE
            assert score.value is None
            assert score.provenance.incomplete_reason == "claim_extraction_failed"


def test_an_interrupted_run_resumes_instead_of_paying_twice(tmp_path):
    dialogues_path, references_path = _write_corpus(tmp_path, policies=("FULL",))
    records = load_corpus(dialogues_path, references_path)
    results_path = tmp_path / "results.jsonl"

    first = MockProvider([EXTRACTED] + [SUPPORTED] * 4)
    score_corpus(records, provider=first, results_path=results_path, seed=1)

    second = MockProvider([EXTRACTED] + [SUPPORTED] * 4)
    resumed = score_corpus(records, provider=second, results_path=results_path, seed=1)

    assert resumed == []
    assert second.calls == []
    assert len(results_path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_results_round_trip_through_the_file(tmp_path):
    """A resumed run's completed work exists only on disk, so it must reload."""
    dialogues_path, references_path = _write_corpus(tmp_path, policies=("FULL",))
    records = load_corpus(dialogues_path, references_path)
    results_path = tmp_path / "results.jsonl"
    provider = MockProvider([EXTRACTED] + [SUPPORTED] * 4)

    written = score_corpus(records, provider=provider, results_path=results_path, seed=1)
    reloaded = read_results(results_path)

    assert [result.as_record() for result in reloaded] == [
        result.as_record() for result in written
    ]


def test_a_corrupted_results_line_is_refused_not_scored(tmp_path):
    results_path = tmp_path / "results.jsonl"
    results_path.write_text(
        json.dumps(
            {
                "dialogue_id": "d1",
                "case_id": "c1",
                "policy": "FULL",
                "reference_mode": "policy_context",
                "scores": {
                    PATIENT_FACTUALITY: {
                        "value": 4.2,  # outside [0, 1]
                        "status": "pass",
                        "provenance": _provenance().as_record(),
                        "detail": {},
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CorpusError, match="bad score"):
        read_results(results_path)


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


def _scored(case: str, policy: str, mode: ReferenceMode, patient: float, doctor: float):
    return ScoredDialogue(
        dialogue_id=f"{case}-{policy}-{mode.value}",
        case_id=case,
        policy_key=policy,
        reference_mode=mode,
        scores={
            PATIENT_FACTUALITY: Score(
                value=patient, status=EvaluationStatus.PASS, provenance=_provenance(mode)
            ),
            DOCTOR_FACTUALITY: Score(
                value=doctor, status=EvaluationStatus.PASS, provenance=_provenance(mode)
            ),
        },
    )


def _synthetic_corpus():
    """A corpus built to carry the confound: a trend under ``policy_context``
    that vanishes under ``full_reference``. Invented, not a claim about MedDial."""
    rising = {"FULL": 0.74, "NO_DIAGNOSIS": 0.79, "NO_DIAGNOSIS_NO_TREATMENT": 0.84}
    results = []
    for index in range(8):
        case = f"case-{index}"
        jitter = 0.005 * index
        for policy in POLICY_ORDER:
            results.append(
                _scored(
                    case,
                    policy,
                    ReferenceMode.POLICY_CONTEXT,
                    rising[policy] + jitter,
                    0.60 + jitter,
                )
            )
            results.append(
                _scored(case, policy, ReferenceMode.FULL_REFERENCE, 0.74 + jitter, 0.60 + jitter)
            )
    return results


def test_analysis_separates_the_curves_by_reference_mode():
    """Test 1: the trend under one reference mode is not the trend under the other."""
    report = analyse(_synthetic_corpus(), resamples=300, seed=2)

    policy_curve = report.curves[f"{PATIENT_FACTUALITY}::{ReferenceMode.POLICY_CONTEXT.value}"]
    full_curve = report.curves[f"{PATIENT_FACTUALITY}::{ReferenceMode.FULL_REFERENCE.value}"]

    assert policy_curve["FULL"].estimate < policy_curve["NO_DIAGNOSIS_NO_TREATMENT"].estimate
    assert full_curve["FULL"].estimate == pytest.approx(
        full_curve["NO_DIAGNOSIS_NO_TREATMENT"].estimate
    )

    under_policy = report.gradients[f"{PATIENT_FACTUALITY}::{ReferenceMode.POLICY_CONTEXT.value}"]
    under_full = report.gradients[f"{PATIENT_FACTUALITY}::{ReferenceMode.FULL_REFERENCE.value}"]
    assert under_policy.excludes_zero
    assert not under_full.excludes_zero


def test_paired_comparisons_are_within_case():
    report = analyse(_synthetic_corpus(), resamples=300, seed=2)

    comparison = report.reference_scope[f"{PATIENT_FACTUALITY}::NO_DIAGNOSIS_NO_TREATMENT"]
    assert comparison.n_cases == 8
    assert comparison.arm_a == "policy_context"
    assert comparison.difference.estimate == pytest.approx(0.10)


def test_turn_scope_comparison_reports_the_gap_between_roles():
    report = analyse(_synthetic_corpus(), resamples=300, seed=2)

    comparison = report.turn_scope[f"FULL::{ReferenceMode.POLICY_CONTEXT.value}"]
    assert comparison.arm_a == PATIENT_FACTUALITY
    assert comparison.arm_b == DOCTOR_FACTUALITY
    assert comparison.difference.estimate == pytest.approx(0.14)


def test_incomplete_dialogues_are_counted_not_absorbed():
    """E.6: the exclusion rate is reported per dimension per condition."""
    results = _synthetic_corpus()
    results.append(
        ScoredDialogue(
            dialogue_id="case-9-FULL-policy_context",
            case_id="case-9",
            policy_key="FULL",
            reference_mode=ReferenceMode.POLICY_CONTEXT,
            scores={
                PATIENT_FACTUALITY: Score.incomplete(
                    ScoreProvenance.unmeasured(
                        scorer_id="test",
                        reference_mode=ReferenceMode.POLICY_CONTEXT,
                        turn_scope=TurnScope.PATIENT,
                        prompt_version="1.0",
                        reason="claim_extraction_failed",
                    )
                )
            },
        )
    )

    report = analyse(results, resamples=200, seed=2)

    assert report.incomplete[f"{PATIENT_FACTUALITY}::policy_context::FULL"] == 1
    assert report.n_cases == 9
    # The unmeasured case must not drag the estimate toward zero.
    assert report.curves[f"{PATIENT_FACTUALITY}::policy_context"]["FULL"].estimate > 0.7


def test_the_report_decomposes_but_does_not_conclude():
    report = analyse(_synthetic_corpus(), resamples=200, seed=2)
    record = report.as_record()
    markdown = render_report(report)

    assert "test_1_reference_scope" in record
    assert "test_2_turn_scope" in record
    assert "regeneration" in record["note"]
    assert "No manuscript framing follows" in markdown
    # Every figure in the table carries an interval (E.6).
    assert "[0." in markdown
