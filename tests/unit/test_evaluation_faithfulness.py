"""W3 items 1-3 and 8: claim extraction, provenance, batched faithfulness.

Every fixture here is invented. No MIMIC-III content appears in this file or
may be added to it (constraint C2).

These tests assert the properties the E0 gate depends on: that the doctor is
scored at all, that a hedged differential is not counted as a hallucination,
that an unmeasurable dimension says so instead of returning a number, and
that a batched verdict array is bound to the claims it was asked about.
"""

from __future__ import annotations

import json

import pytest

from meddial.evaluation import (
    DOCTOR_ROLE,
    PATIENT_ROLE,
    ClaimType,
    EvaluationStatus,
    ReferenceMode,
    Score,
    ScoreProvenance,
    TurnScope,
    Verdict,
    build_turns,
    extract_claims,
    reference_payload,
    score_dialogue_faithfulness,
    score_faithfulness,
    verify_claims,
)
from meddial.evaluation.claims import ClaimExtractionError
from meddial.evaluation.faithfulness import VerificationError
from meddial.knowledge import (
    Core,
    Demographics,
    Diagnosis,
    Medication,
    PolicyRegistry,
    StructuredClinicalReference,
    Symptom,
    build_contexts,
)
from meddial.llm import MockProvider
from meddial.llm.errors import ProviderError

# --------------------------------------------------------------------------
# Fixtures — a synthetic case and a synthetic dialogue about it
# --------------------------------------------------------------------------

DIALOGUE = [
    {"role": "Doctor", "content": "Good morning. What brings you in today?"},
    {
        "role": "Patient",
        "content": "I have had shortness of breath and my ankles have been swollen for three days.",
    },
    {
        "role": "Doctor",
        "content": "Your echocardiogram last month showed an ejection fraction of 30 percent.",
    },
    {"role": "Patient", "content": "I take metoprolol every morning."},
    {
        "role": "Doctor",
        "content": "This could be worsening heart failure. I suspect we should check your BNP.",
    },
]

# What a well-behaved extractor returns for DIALOGUE. Turn 2 is a fabricated
# doctor_fact: no echocardiogram appears anywhere in the reference.
EXTRACTED = json.dumps(
    [
        {"turn_index": 0, "role": "Doctor", "type": "non_medical", "text": "Greeting."},
        {"turn_index": 0, "role": "Doctor", "type": "question", "text": "Why did you come in?"},
        {
            "turn_index": 1,
            "role": "Patient",
            "type": "patient_fact",
            "text": "The patient has shortness of breath.",
        },
        {
            "turn_index": 1,
            "role": "Patient",
            "type": "patient_fact",
            "text": "The patient has ankle swelling for three days.",
        },
        {
            "turn_index": 2,
            "role": "Doctor",
            "type": "doctor_fact",
            "text": "An echocardiogram last month showed an ejection fraction of 30 percent.",
        },
        {
            "turn_index": 3,
            "role": "Patient",
            "type": "patient_fact",
            "text": "The patient takes metoprolol every morning.",
        },
        {
            "turn_index": 4,
            "role": "Doctor",
            "type": "diagnostic_hypothesis",
            "text": "The presentation could be worsening heart failure.",
        },
        {
            "turn_index": 4,
            "role": "Doctor",
            "type": "recommendation",
            "text": "A BNP should be checked.",
        },
    ]
)

PATIENT_VERDICTS = json.dumps(
    [
        {"claim_index": 0, "verdict": "supported", "justification": "Core_Fields.Symptoms."},
        {"claim_index": 1, "verdict": "supported", "justification": "Core_Fields.Symptoms."},
        {"claim_index": 2, "verdict": "supported", "justification": "Current_Medications."},
    ]
)

DOCTOR_VERDICTS = json.dumps(
    [
        {
            "claim_index": 0,
            "verdict": "unsupported",
            "justification": "No echocardiogram or ejection fraction in the reference.",
        }
    ]
)


@pytest.fixture
def reference() -> StructuredClinicalReference:
    """A synthetic case. Deliberately not derived from any real record."""
    return StructuredClinicalReference(
        subject_id=1,
        hadm_id=1,
        core=Core(
            symptoms=[
                Symptom(description="Shortness of breath", duration="three days"),
                Symptom(description="Ankle swelling", duration="three days"),
            ],
            diagnoses=[Diagnosis(primary="Congestive Heart Failure")],
        ),
        context={
            "Patient_Demographics": Demographics(age="64", sex="F"),
            "Current_Medications": [Medication(name="Metoprolol", purpose="rate control")],
        },
    )


@pytest.fixture
def full_context(reference: StructuredClinicalReference):
    policy = PolicyRegistry().load("FULL")
    return build_contexts(reference, policy).evaluator


@pytest.fixture
def masked_context(reference: StructuredClinicalReference):
    policy = PolicyRegistry().load("NO_DIAGNOSIS")
    return build_contexts(reference, policy).evaluator


@pytest.fixture
def turns():
    return build_turns(DIALOGUE)


def _provenance(**overrides) -> ScoreProvenance:
    defaults = {
        "scorer_id": "test",
        "model_family": "mock",
        "model_id": "mock-model",
        "model_digest": "deadbeef",
        "quantisation": "none",
        "reference_mode": ReferenceMode.FULL_REFERENCE,
        "turn_scope": TurnScope.PATIENT,
        "prompt_version": "test@0",
        "sampling": {},
    }
    defaults.update(overrides)
    return ScoreProvenance(**defaults)


# --------------------------------------------------------------------------
# EVAL-3 — a score cannot exist without provenance
# --------------------------------------------------------------------------


def test_score_without_provenance_raises():
    with pytest.raises(TypeError):
        Score(value=0.9, status=EvaluationStatus.PASS)  # type: ignore[call-arg]


def test_incomplete_score_cannot_carry_a_value():
    with pytest.raises(ValueError, match="value=None"):
        Score(
            value=0.0,
            status=EvaluationStatus.INCOMPLETE,
            provenance=_provenance(incomplete_reason="whatever"),
        )


def test_incomplete_score_must_say_why():
    with pytest.raises(ValueError, match="incomplete_reason"):
        Score(value=None, status=EvaluationStatus.INCOMPLETE, provenance=_provenance())


# --------------------------------------------------------------------------
# W3 item 1 — extraction covers every turn and is bound to it
# --------------------------------------------------------------------------


def test_claims_are_extracted_from_both_speakers(turns):
    provider = MockProvider([EXTRACTED])
    claim_set = extract_claims(turns, provider=provider)

    assert claim_set.for_role(PATIENT_ROLE), "patient turns produced no claims"
    assert claim_set.for_role(DOCTOR_ROLE), "doctor turns produced no claims"
    assert len(claim_set) == 8
    assert claim_set.prompt_version.startswith("claim_extraction@")


def test_claim_citing_a_turn_outside_the_transcript_is_rejected(turns):
    bogus = json.dumps(
        [{"turn_index": 99, "role": "Patient", "type": "patient_fact", "text": "Something."}]
    )
    provider = MockProvider([bogus, bogus])
    with pytest.raises(ClaimExtractionError, match="not in the transcript"):
        extract_claims(turns, provider=provider)


def test_claim_type_outside_the_enum_is_rejected(turns):
    bogus = json.dumps([{"turn_index": 1, "role": "Patient", "type": "vibes", "text": "Something."}])
    provider = MockProvider([bogus, bogus])
    with pytest.raises(ClaimExtractionError, match="not a ClaimType"):
        extract_claims(turns, provider=provider)


def test_extraction_retries_once_before_giving_up(turns):
    provider = MockProvider(["not json at all", EXTRACTED])
    claim_set = extract_claims(turns, provider=provider)

    assert len(claim_set) == 8
    assert len(provider.calls) == 2


# --------------------------------------------------------------------------
# W3 item 8 — batched verification, aligned by index (EVAL-10)
# --------------------------------------------------------------------------


def test_batched_verdicts_align_with_claim_indices(turns, full_context):
    """Verdicts returned out of order must still land on the right claims."""
    claim_set = extract_claims(turns, provider=MockProvider([EXTRACTED]))
    patient_claims = claim_set.factual_for_role(PATIENT_ROLE)

    shuffled = json.dumps(
        [
            {"claim_index": 2, "verdict": "unsupported", "justification": "third"},
            {"claim_index": 0, "verdict": "supported", "justification": "first"},
            {"claim_index": 1, "verdict": "unverifiable", "justification": "second"},
        ]
    )
    result = verify_claims(
        patient_claims,
        reference_payload(full_context, ReferenceMode.FULL_REFERENCE),
        provider=MockProvider([shuffled]),
    )

    assert [v.claim_index for v in result.verdicts] == [0, 1, 2]
    assert [v.verdict for v in result.verdicts] == [
        Verdict.SUPPORTED,
        Verdict.UNVERIFIABLE,
        Verdict.UNSUPPORTED,
    ]
    assert [v.justification for v in result.verdicts] == ["first", "second", "third"]


def test_batched_verification_costs_one_call_regardless_of_claim_count(turns, full_context):
    claim_set = extract_claims(turns, provider=MockProvider([EXTRACTED]))
    patient_claims = claim_set.factual_for_role(PATIENT_ROLE)
    reference = reference_payload(full_context, ReferenceMode.FULL_REFERENCE)

    batched_provider = MockProvider([PATIENT_VERDICTS])
    batched = verify_claims(patient_claims, reference, provider=batched_provider)

    single = json.dumps([{"claim_index": 0, "verdict": "supported", "justification": "yes"}])
    per_claim_provider = MockProvider([single] * len(patient_claims))
    per_claim = verify_claims(patient_claims, reference, provider=per_claim_provider, batched=False)

    assert batched.calls == 1
    assert per_claim.calls == len(patient_claims) == 3
    assert len(batched_provider.calls) < len(per_claim_provider.calls)


def test_verdict_count_mismatch_retries_once_then_raises(turns, full_context):
    claim_set = extract_claims(turns, provider=MockProvider([EXTRACTED]))
    patient_claims = claim_set.factual_for_role(PATIENT_ROLE)

    too_few = json.dumps([{"claim_index": 0, "verdict": "supported", "justification": "x"}])
    provider = MockProvider([too_few, too_few])

    with pytest.raises(VerificationError, match="expected 3 verdict"):
        verify_claims(
            patient_claims,
            reference_payload(full_context, ReferenceMode.FULL_REFERENCE),
            provider=provider,
        )
    assert len(provider.calls) == 2


def test_duplicate_claim_index_is_rejected(turns, full_context):
    claim_set = extract_claims(turns, provider=MockProvider([EXTRACTED]))
    patient_claims = claim_set.factual_for_role(PATIENT_ROLE)

    duplicated = json.dumps(
        [
            {"claim_index": 0, "verdict": "supported", "justification": "x"},
            {"claim_index": 0, "verdict": "supported", "justification": "x"},
            {"claim_index": 1, "verdict": "supported", "justification": "x"},
        ]
    )
    provider = MockProvider([duplicated, duplicated])
    with pytest.raises(VerificationError, match="more than one verdict"):
        verify_claims(
            patient_claims,
            reference_payload(full_context, ReferenceMode.FULL_REFERENCE),
            provider=provider,
        )


# --------------------------------------------------------------------------
# W3 item 2 — role separation, reference selection, INCOMPLETE
# --------------------------------------------------------------------------


def test_fabricated_doctor_diagnosis_fails_doctor_factuality_only(turns, full_context):
    provider = MockProvider([EXTRACTED, PATIENT_VERDICTS, DOCTOR_VERDICTS])
    scores = score_dialogue_faithfulness(
        turns,
        full_context,
        provider=provider,
        reference_mode=ReferenceMode.FULL_REFERENCE,
        threshold=0.8,
    )

    patient = scores["patient_factuality"]
    doctor = scores["doctor_factuality"]

    assert patient.status is EvaluationStatus.PASS
    assert patient.value == 1.0
    assert doctor.status is EvaluationStatus.FAIL
    assert doctor.value == 0.0

    assert patient.provenance.turn_scope is TurnScope.PATIENT
    assert doctor.provenance.turn_scope is TurnScope.DOCTOR
    assert doctor.detail["unsupported"] == 1


def test_diagnostic_hypothesis_not_scored_as_hallucination(turns, full_context):
    """The doctor hedges in turn 4. Only the turn-2 assertion may be scored."""
    provider = MockProvider([EXTRACTED, PATIENT_VERDICTS, DOCTOR_VERDICTS])
    scores = score_dialogue_faithfulness(
        turns, full_context, provider=provider, reference_mode=ReferenceMode.FULL_REFERENCE
    )
    doctor = scores["doctor_factuality"]

    assert doctor.detail["claims_for_role"] == 5
    assert doctor.detail["factual_claims"] == 1
    scored_turns = {entry["turn_index"] for entry in doctor.detail["verdicts"]}
    assert scored_turns == {2}
    assert all(entry["type"] == ClaimType.DOCTOR_FACT.value for entry in doctor.detail["verdicts"])


def test_empty_dialogue_returns_incomplete(full_context):
    """A dialogue with nothing assertable has no faithfulness, not a zero."""
    small_talk = build_turns(
        [
            {"role": "Doctor", "content": "Good morning."},
            {"role": "Patient", "content": "Morning."},
        ]
    )
    provider = MockProvider(["[]"])
    scores = score_dialogue_faithfulness(
        small_talk, full_context, provider=provider, reference_mode=ReferenceMode.FULL_REFERENCE
    )

    for score in scores.values():
        assert score.status is EvaluationStatus.INCOMPLETE
        assert score.value is None
        assert score.provenance.incomplete_reason == "no_factual_claims"


def test_raising_scorer_returns_incomplete_not_default(turns, full_context):
    """A judge that cannot produce alignable verdicts yields no number at all."""
    garbage = "I am unable to comply with this request."
    provider = MockProvider([EXTRACTED, garbage, garbage, garbage, garbage])
    scores = score_dialogue_faithfulness(
        turns, full_context, provider=provider, reference_mode=ReferenceMode.FULL_REFERENCE
    )

    for score in scores.values():
        assert score.status is EvaluationStatus.INCOMPLETE
        assert score.value is None
        assert score.provenance.incomplete_reason.startswith("verification_failed")
        assert score.provenance.fallback_used is False


def test_all_unverifiable_verdicts_yield_incomplete(turns, full_context):
    claim_set = extract_claims(turns, provider=MockProvider([EXTRACTED]))
    unverifiable = json.dumps(
        [{"claim_index": i, "verdict": "unverifiable", "justification": "x"} for i in range(3)]
    )
    score = score_faithfulness(
        claim_set,
        full_context,
        role=PATIENT_ROLE,
        reference_mode=ReferenceMode.FULL_REFERENCE,
        provider=MockProvider([unverifiable]),
    )

    assert score.status is EvaluationStatus.INCOMPLETE
    assert score.provenance.incomplete_reason == "all_claims_unverifiable"
    assert score.detail["unverifiable"] == 3


def test_unverifiable_claims_leave_the_denominator(turns, full_context):
    claim_set = extract_claims(turns, provider=MockProvider([EXTRACTED]))
    mixed = json.dumps(
        [
            {"claim_index": 0, "verdict": "supported", "justification": "x"},
            {"claim_index": 1, "verdict": "unsupported", "justification": "x"},
            {"claim_index": 2, "verdict": "unverifiable", "justification": "x"},
        ]
    )
    score = score_faithfulness(
        claim_set,
        full_context,
        role=PATIENT_ROLE,
        reference_mode=ReferenceMode.FULL_REFERENCE,
        provider=MockProvider([mixed]),
    )

    assert score.value == 0.5
    assert score.detail["unverifiable"] == 1


def test_claim_extraction_failure_yields_incomplete_for_both_roles(turns, full_context):
    provider = MockProvider(["nonsense", "still nonsense"])
    scores = score_dialogue_faithfulness(
        turns, full_context, provider=provider, reference_mode=ReferenceMode.FULL_REFERENCE
    )

    assert set(scores) == {"patient_factuality", "doctor_factuality"}
    for score in scores.values():
        assert score.status is EvaluationStatus.INCOMPLETE
        assert score.provenance.incomplete_reason.startswith("claim_extraction_failed")


# --------------------------------------------------------------------------
# EVAL-2 / E0 confound 1 — the reference is an explicit, recorded choice
# --------------------------------------------------------------------------


def test_both_reference_modes_recorded_distinctly(turns, masked_context):
    claim_set = extract_claims(turns, provider=MockProvider([EXTRACTED]))

    against_policy = score_faithfulness(
        claim_set,
        masked_context,
        role=PATIENT_ROLE,
        reference_mode=ReferenceMode.POLICY_CONTEXT,
        provider=MockProvider([PATIENT_VERDICTS]),
    )
    against_full = score_faithfulness(
        claim_set,
        masked_context,
        role=PATIENT_ROLE,
        reference_mode=ReferenceMode.FULL_REFERENCE,
        provider=MockProvider([PATIENT_VERDICTS]),
    )

    assert against_policy.provenance.reference_mode is ReferenceMode.POLICY_CONTEXT
    assert against_full.provenance.reference_mode is ReferenceMode.FULL_REFERENCE


def test_policy_context_reference_is_smaller_than_the_full_reference(masked_context):
    """The confound E0 exists to measure: restricting disclosure shrinks the yardstick."""
    policy_view = reference_payload(masked_context, ReferenceMode.POLICY_CONTEXT)
    full_view = reference_payload(masked_context, ReferenceMode.FULL_REFERENCE)

    assert "Congestive Heart Failure" in json.dumps(full_view)
    assert "Congestive Heart Failure" not in json.dumps(policy_view)
    assert len(json.dumps(policy_view)) < len(json.dumps(full_view))


def test_policy_context_reference_uses_the_scored_roles_permissions(masked_context):
    patient_view = reference_payload(
        masked_context,
        ReferenceMode.POLICY_CONTEXT,
        role=PATIENT_ROLE,
    )
    doctor_view = reference_payload(
        masked_context,
        ReferenceMode.POLICY_CONTEXT,
        role=DOCTOR_ROLE,
    )

    assert "Shortness of breath" in json.dumps(patient_view)
    assert "Shortness of breath" not in json.dumps(doctor_view)
    assert set(doctor_view) == {"context"}
    assert set(doctor_view["context"]) == {"demographics"}
    assert doctor_view["context"]["demographics"]["age"] == 64


def test_reference_payload_never_carries_evidence_spans(full_context):
    for mode in ReferenceMode:
        assert "evidence" not in json.dumps(reference_payload(full_context, mode))


# --------------------------------------------------------------------------
# D-08 — infrastructure failure is not a measurement outcome
# --------------------------------------------------------------------------


def test_provider_error_propagates_rather_than_becoming_a_score(turns, full_context):
    provider = MockProvider(failure=ProviderError("model server unreachable"))
    with pytest.raises(ProviderError):
        score_dialogue_faithfulness(
            turns, full_context, provider=provider, reference_mode=ReferenceMode.FULL_REFERENCE
        )
