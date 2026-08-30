"""W3 items 4-7: naturalness, knowledge boundary, structural validity, acceptance.

Every fixture here is invented. No MIMIC-III content appears in this file or
may be added to it (constraint C2).

The properties under test are the ones a reviewer would challenge: that a
failed scorer reports ``INCOMPLETE`` rather than a default number, that a
leakage detector cannot inflate its own rate, that ``FULL`` records why its
zero-leakage result is definitional, that a provider-error sentinel can never
be accepted as dialogue, and that a strong composite cannot rescue a failed
dimension.
"""

from __future__ import annotations

import json

import pytest

from meddial.evaluation import (
    KNOWLEDGE_BOUNDARY,
    NATURALNESS,
    PATIENT_FACTUALITY,
    PATIENT_ROLE,
    STRUCTURAL_VALIDITY,
    Acceptance,
    EvaluationStatus,
    ReferenceMode,
    Score,
    ScoreProvenance,
    StructuralConfig,
    TurnScope,
    build_turns,
    check_structure,
    compute_composite,
    decide,
    is_permitted,
    leakable_paths,
    permissible_paths,
    rate_naturalness,
    score_knowledge_boundary,
    score_naturalness,
    score_structural_validity,
)
from meddial.evaluation.acceptance import DOCTOR_FACTUALITY
from meddial.evaluation.boundary import BoundaryError, detect_leakage
from meddial.evaluation.naturalness import NaturalnessError
from meddial.knowledge import PolicyRegistry
from meddial.llm import MockProvider

# --------------------------------------------------------------------------
# Fixtures — a synthetic dialogue, invented for this test
# --------------------------------------------------------------------------

DIALOGUE = [
    {"role": "Doctor", "content": "Good morning. What brings you in today?"},
    {"role": "Patient", "content": "I have been short of breath for a few days."},
    {"role": "Doctor", "content": "Does it get worse when you lie down?"},
    {"role": "Patient", "content": "Yes, I have to prop myself up with pillows."},
]

GOOD_RATING = json.dumps({"score": 0.72, "rationale": "Turns build on each other."})
NO_EVENTS = "[]"


@pytest.fixture
def turns():
    return build_turns(DIALOGUE)


@pytest.fixture
def full_policy():
    return PolicyRegistry().load("FULL")


@pytest.fixture
def masked_policy():
    return PolicyRegistry().load("NO_DIAGNOSIS")


def _measured(value: float, dimension: str = "test") -> Score:
    return Score.measured(
        value,
        ScoreProvenance(
            scorer_id=dimension,
            model_family="mock",
            model_id="mock-model",
            model_digest="deadbeef",
            quantisation="none",
            reference_mode=ReferenceMode.FULL_REFERENCE,
            turn_scope=TurnScope.BOTH,
            prompt_version="test@0",
            sampling={},
        ),
    )


def _unmeasured(dimension: str = "test") -> Score:
    return Score.incomplete(
        ScoreProvenance.unmeasured(
            scorer_id=dimension,
            reference_mode=ReferenceMode.FULL_REFERENCE,
            turn_scope=TurnScope.BOTH,
            prompt_version="test@0",
            reason="scorer_failed",
        )
    )


def _all_passing() -> dict[str, Score]:
    return {
        PATIENT_FACTUALITY: _measured(0.95),
        DOCTOR_FACTUALITY: _measured(0.90),
        KNOWLEDGE_BOUNDARY: _measured(1.0),
        NATURALNESS: _measured(0.80),
        STRUCTURAL_VALIDITY: _measured(1.0),
    }


# --------------------------------------------------------------------------
# W3 item 4 — naturalness has no fallback (EVAL-4, defect D-06)
# --------------------------------------------------------------------------


def test_naturalness_scores_a_well_formed_rating(turns):
    provider = MockProvider([GOOD_RATING])
    score = score_naturalness(turns, provider=provider)

    assert score.status is EvaluationStatus.PASS
    assert score.value == pytest.approx(0.72)
    assert score.provenance.prompt_version.startswith("naturalness@")
    assert score.provenance.fallback_used is False
    assert score.detail["rationale"] == "Turns build on each other."


def test_failed_naturalness_is_incomplete_not_a_default_number(turns):
    """The deleted fallback returned 0.5 here. A 0.5 is a claim; this is not."""
    provider = MockProvider(["I am not able to rate this.", "Still not JSON."])
    score = score_naturalness(turns, provider=provider)

    assert score.status is EvaluationStatus.INCOMPLETE
    assert score.value is None
    assert score.provenance.incomplete_reason.startswith("naturalness_failed:")
    assert score.provenance.fallback_used is False


def test_naturalness_retries_once_before_giving_up(turns):
    provider = MockProvider(["garbage", GOOD_RATING])
    value, _, _ = rate_naturalness(turns, provider=provider)

    assert value == pytest.approx(0.72)
    assert len(provider.calls) == 2


def test_naturalness_rejects_a_score_outside_the_unit_interval(turns):
    out_of_range = json.dumps({"score": 4.5, "rationale": "Out of ten."})
    provider = MockProvider([out_of_range, out_of_range])
    with pytest.raises(NaturalnessError, match="outside"):
        rate_naturalness(turns, provider=provider)


def test_naturalness_refuses_a_temperature_above_the_prd_ceiling(turns):
    provider = MockProvider([GOOD_RATING])
    with pytest.raises(NaturalnessError, match="ceiling"):
        rate_naturalness(turns, provider=provider, temperature=0.7)


# --------------------------------------------------------------------------
# W3 item 5 — knowledge boundary as located events (EVAL-6, PRD §9.2)
# --------------------------------------------------------------------------


def test_clean_dialogue_scores_one_with_no_events(turns, masked_policy):
    provider = MockProvider([NO_EVENTS])
    score, events = score_knowledge_boundary(turns, masked_policy, provider=provider)

    assert events == []
    assert score.value == 1.0
    assert score.status is EvaluationStatus.PASS
    assert score.detail["event_count"] == 0


def test_leakage_event_carries_the_turn_field_and_excerpt(turns, masked_policy):
    reported = json.dumps(
        [
            {
                "turn_index": 3,
                "field_path": "core.diagnoses",
                "excerpt": "prop myself up with pillows",
            }
        ]
    )
    provider = MockProvider([reported])
    score, events = score_knowledge_boundary(turns, masked_policy, provider=provider)

    assert score.value == 0.0
    assert score.status is EvaluationStatus.FAIL
    assert len(events) == 1
    assert events[0].as_record() == {
        "turn_index": 3,
        "role": PATIENT_ROLE,
        "field_path": "core.diagnoses",
        "policy": masked_policy.key,
        "excerpt": "prop myself up with pillows",
    }


def test_detector_cannot_count_a_permitted_field_as_leakage(turns, masked_policy):
    """core.symptoms is visible under NO_DIAGNOSIS, so naming it is disclosure."""
    reported = json.dumps(
        [{"turn_index": 1, "field_path": "core.symptoms", "excerpt": "short of breath"}]
    )
    provider = MockProvider([reported])
    events, _ = detect_leakage(turns, masked_policy, provider=provider)

    assert events == []


def test_detector_cannot_invent_a_field_path(turns, masked_policy):
    reported = json.dumps(
        [{"turn_index": 1, "field_path": "core.secret_score", "excerpt": "whatever"}]
    )
    provider = MockProvider([reported, reported])
    with pytest.raises(BoundaryError, match="not in the reference schema"):
        detect_leakage(turns, masked_policy, provider=provider)


def test_event_attributed_to_the_other_speaker_is_rejected(turns, masked_policy):
    reported = json.dumps(
        [{"turn_index": 2, "field_path": "core.diagnoses", "excerpt": "lie down"}]
    )
    provider = MockProvider([reported, reported])
    with pytest.raises(BoundaryError, match="spoken by Doctor"):
        detect_leakage(turns, masked_policy, provider=provider, role=PATIENT_ROLE)


def test_full_policy_records_that_zero_leakage_is_definitional(turns, full_policy):
    """PRD §9.2: under FULL nothing is left to leak, and the score must say so."""
    provider = MockProvider([NO_EVENTS])
    score, events = score_knowledge_boundary(turns, full_policy, provider=provider)

    assert events == []
    assert score.value == 1.0
    assert score.detail["permissible_is_total"] is True
    assert leakable_paths(full_policy, PATIENT_ROLE) == frozenset()


def test_restricted_policy_leaves_something_to_leak(turns, masked_policy):
    provider = MockProvider([NO_EVENTS])
    score, _ = score_knowledge_boundary(turns, masked_policy, provider=provider)

    assert score.detail["permissible_is_total"] is False
    assert "core.diagnoses" in leakable_paths(masked_policy, PATIENT_ROLE)


def test_permission_covers_fields_nested_under_a_permitted_path(masked_policy):
    permitted = permissible_paths(masked_policy, PATIENT_ROLE)

    assert is_permitted("core.symptoms[].description", permitted)
    assert not is_permitted("core.diagnoses[].primary", permitted)


def test_failed_boundary_check_is_incomplete_not_a_pass(turns, masked_policy):
    provider = MockProvider(["not json", "still not json"])
    score, events = score_knowledge_boundary(turns, masked_policy, provider=provider)

    assert score.status is EvaluationStatus.INCOMPLETE
    assert score.value is None
    assert events == []
    assert score.provenance.incomplete_reason.startswith("boundary_check_failed:")


# --------------------------------------------------------------------------
# W3 item 6 — structural validity is deterministic (EVAL-9, PRD §9.4)
# --------------------------------------------------------------------------


def test_structural_detects_error_sentinel(turns):
    """A provider-error string in a turn can never be accepted as dialogue (D-08)."""
    broken = list(DIALOGUE)
    broken[3] = {"role": "Patient", "content": "[ERROR: connection refused]"}
    score, report = score_structural_validity(build_turns(broken))

    assert score.value == 0.0
    assert score.status is EvaluationStatus.FAIL
    assert "error_sentinel" in report.failed_checks
    assert report.violations[0].turn_index == 3

    clean, clean_report = score_structural_validity(turns)
    assert clean.value == 1.0
    assert clean_report.is_valid


def test_structural_makes_no_model_call(turns):
    score, _ = score_structural_validity(turns)

    assert score.provenance.used_a_model is False
    assert score.provenance.reference_mode is None
    assert score.provenance.sampling == {}


def test_structural_is_reproducible(turns):
    first, _ = score_structural_validity(turns)
    second, _ = score_structural_validity(turns)

    assert first.value == second.value
    assert first.detail == second.detail


def test_structural_flags_a_broken_alternation():
    doubled = [
        {"role": "Doctor", "content": "What brings you in?"},
        {"role": "Doctor", "content": "Any pain?"},
        {"role": "Patient", "content": "Some, in my chest."},
        {"role": "Doctor", "content": "For how long?"},
    ]
    report = check_structure(build_turns(doubled))

    assert "alternation" in report.failed_checks


def test_structural_flags_an_empty_turn():
    blanked = list(DIALOGUE)
    blanked[3] = {"role": "Patient", "content": "   "}
    report = check_structure(build_turns(blanked))

    assert "empty_turn" in report.failed_checks


def test_structural_flags_a_duplicated_consecutive_turn():
    repeated = [
        {"role": "Doctor", "content": "Any pain?"},
        {"role": "Patient", "content": "Yes, in my chest."},
        {"role": "Doctor", "content": "Yes, in my chest."},
        {"role": "Patient", "content": "Yes, in my chest."},
    ]
    report = check_structure(build_turns(repeated))

    assert "duplicate_turn" in report.failed_checks


def test_structural_flags_a_dialogue_outside_the_turn_bounds(turns):
    too_short = check_structure(turns, config=StructuralConfig(min_turns=10))
    too_long = check_structure(turns, config=StructuralConfig(max_turns=2))

    assert "turn_bounds" in too_short.failed_checks
    assert "turn_bounds" in too_long.failed_checks


# --------------------------------------------------------------------------
# W3 item 7 — acceptance gates, with the composite outside them (EVAL-5/7)
# --------------------------------------------------------------------------


def test_all_dimensions_passing_is_accepted():
    result = decide(_all_passing())

    assert result.overall is Acceptance.ACCEPT
    assert result.missing == ()
    assert result.as_record()["overall"] == "ACCEPT"


def test_one_failed_dimension_rejects_despite_a_strong_composite():
    scores = _all_passing()
    scores[KNOWLEDGE_BOUNDARY] = _measured(0.0)
    result = decide(scores)

    assert result.overall is Acceptance.REJECT
    assert result.per_dimension[KNOWLEDGE_BOUNDARY] is EvaluationStatus.FAIL
    assert result.composite.value is not None


def test_unmeasured_dimension_makes_the_dialogue_incomplete_not_rejected():
    scores = _all_passing()
    scores[NATURALNESS] = _unmeasured(NATURALNESS)
    result = decide(scores)

    assert result.overall is Acceptance.INCOMPLETE
    assert result.per_dimension[NATURALNESS] is EvaluationStatus.INCOMPLETE


def test_a_definite_failure_outranks_an_unknown():
    scores = _all_passing()
    scores[NATURALNESS] = _unmeasured(NATURALNESS)
    scores[PATIENT_FACTUALITY] = _measured(0.10)
    result = decide(scores)

    assert result.overall is Acceptance.REJECT


def test_a_missing_dimension_is_not_a_pass():
    scores = _all_passing()
    del scores[DOCTOR_FACTUALITY]
    result = decide(scores)

    assert result.overall is Acceptance.INCOMPLETE
    assert result.missing == (DOCTOR_FACTUALITY,)


def test_composite_uses_the_thesis_weights_unchanged():
    scores = {
        NATURALNESS: _measured(1.0),
        KNOWLEDGE_BOUNDARY: _measured(0.0),
        PATIENT_FACTUALITY: _measured(0.0),
    }
    composite = compute_composite(scores)

    assert composite.value == pytest.approx(0.4)
    assert composite.note == "reporting only; not used for acceptance"


def test_composite_is_none_when_a_term_is_unmeasured():
    scores = _all_passing()
    scores[NATURALNESS] = _unmeasured(NATURALNESS)

    assert compute_composite(scores).value is None
    assert decide(scores).composite.value is None


def test_thresholds_used_are_recorded_on_the_result():
    result = decide(_all_passing(), thresholds={NATURALNESS: 0.9})

    assert result.overall is Acceptance.REJECT
    assert result.thresholds[NATURALNESS] == 0.9
    assert result.as_record()["thresholds"][NATURALNESS] == 0.9


def test_doctor_factuality_is_gated_not_merely_reported():
    """The thesis scored patient turns only; a lying doctor now rejects."""
    scores = _all_passing()
    scores[DOCTOR_FACTUALITY] = _measured(0.20)
    result = decide(scores)

    assert result.overall is Acceptance.REJECT
    assert result.per_dimension[DOCTOR_FACTUALITY] is EvaluationStatus.FAIL
