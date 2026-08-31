"""D-05 at the agent layer: the doctor's briefing is not the patient's policy.

``meddial.knowledge`` has always produced two ids — ``patient.policy_id`` and
``doctor.guidance_id`` — and the run record has always written both. But
``DoctorAgent`` read its briefing off ``patient_profile["profile_type"]``, so
the two were equal by construction whatever the caller asked for. Disclosure
and briefing were then a single treatment, and no analysis of the resulting
corpus could separate them. That is confound 4 of experiment E0.

These tests fail if the coupling is reintroduced.
"""

from __future__ import annotations

from Agents.DoctorAgent import DoctorAgent
from dialogue_generation_framework import DialogueGenerationPipeline
from meddial.knowledge import DoctorContext
from meddial.llm import MockProvider
from Utils.bias_aware_prompts import DOCTOR_GUIDANCE

PROFILE = {
    "profile_type": "NO_DIAGNOSIS_NO_TREATMENT",
    "Core_Fields": {"Symptoms": [{"description": "sore throat for four days"}]},
    "Context_Fields": {"Patient_Demographics": {"Age": 41, "Sex": "F"}},
}


def _doctor(guidance_id: str | None) -> DoctorAgent:
    return DoctorAgent(MockProvider(["ok"]), patient_profile=PROFILE, guidance_id=guidance_id)


def test_guidance_defaults_to_the_patient_policy() -> None:
    """The uncrossed default: an ordinary run is unchanged."""
    agent = _doctor(None)

    assert agent.guidance_id == "NO_DIAGNOSIS_NO_TREATMENT"
    assert DOCTOR_GUIDANCE["NO_DIAGNOSIS_NO_TREATMENT"] in agent.system_message["content"]


def test_guidance_follows_guidance_id_not_the_patient_profile() -> None:
    """The crossed cell E0 needs: an ignorant patient, a doctor briefed as FULL."""
    agent = _doctor("FULL")

    # The patient's policy is untouched; only the briefing moved.
    assert agent.profile_type == "NO_DIAGNOSIS_NO_TREATMENT"
    assert agent.guidance_id == "FULL"

    briefing = agent.system_message["content"]
    assert DOCTOR_GUIDANCE["FULL"] in briefing
    assert DOCTOR_GUIDANCE["NO_DIAGNOSIS_NO_TREATMENT"] not in briefing


def test_crossing_actually_changes_the_prompt() -> None:
    """Guards against a guidance id that is recorded but never reaches the model."""
    assert _doctor("FULL").system_message["content"] != _doctor(None).system_message["content"]


def test_an_unknown_guidance_id_falls_closed() -> None:
    """An unrecognised id briefs the doctor for the least-informed patient."""
    agent = _doctor("NO_SUCH_ARM")

    assert DOCTOR_GUIDANCE["NO_DIAGNOSIS_NO_TREATMENT"] in agent.system_message["content"]


def test_the_doctor_is_never_handed_the_diagnosis() -> None:
    """The briefing names what data exists, not what it says."""
    profile = dict(PROFILE, Core_Fields=dict(
        PROFILE["Core_Fields"], Diagnoses=[{"primary": "acute pharyngitis"}]
    ))
    agent = DoctorAgent(MockProvider(["ok"]), patient_profile=profile)

    assert "pharyngitis" not in agent.system_message["content"].lower()


def test_doctor_context_is_the_agents_only_structured_view() -> None:
    visible = {"context": {"demographics": {"age": 41, "sex": "F"}}}
    context = DoctorContext(guidance_id="FULL", visible=visible)

    agent = DoctorAgent(MockProvider(["ok"]), doctor_context=context)

    assert agent.patient_profile == visible
    assert agent.guidance_id == "FULL"
    assert "Age: 41" in agent.system_message["content"]


def test_hidden_expected_symptoms_are_not_injected_into_doctor_calls() -> None:
    # The sentinel must not be a phrase that DOCTOR_GUIDANCE itself uses as an
    # illustration -- "sore throat" appears verbatim in the FULL briefing's
    # "this is a light, common complaint" example, so asserting on it reports a
    # leak that never happened.  An implausible symptom can only reach the
    # rendered call by being injected from the profile.
    profile = dict(
        PROFILE,
        Core_Fields=dict(
            PROFILE["Core_Fields"],
            Symptoms=[{"description": "zzqx paraesthesia for four days"}],
        ),
    )
    provider = MockProvider(["What brings you in today?"])
    agent = DoctorAgent(provider, patient_profile=profile, guidance_id="FULL")

    agent.respond([])

    rendered_call = "\n".join(message.content for message in provider.calls[0].messages)
    assert "zzqx" not in rendered_call.lower()


def test_pipeline_builds_doctor_from_doctor_context_not_patient_profile(tmp_path) -> None:
    generator = MockProvider(["ok"], model_family="generator")
    judge = MockProvider(["ok"], model_family="judge")
    pipeline = DialogueGenerationPipeline(
        generator,
        judge,
        output_dir=tmp_path,
    )
    visible = {"context": {"demographics": {"age": 41, "sex": "F"}}}
    doctor_context = DoctorContext(guidance_id="FULL", visible=visible)

    doctor, patient = pipeline._build_dialogue_agents(PROFILE, doctor_context)

    assert doctor.patient_profile == visible
    assert patient.profile is PROFILE
