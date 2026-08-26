from __future__ import annotations

from Agents.DoctorAgent import DoctorAgent
from meddial.knowledge import ProfileType, build_conversation_contexts, mask_profile_for_patient
from meddial.llm import MockLLMProvider


def test_full_policy_exposes_documented_fields(clinical_reference):
    profile = mask_profile_for_patient(clinical_reference, ProfileType.FULL)
    assert profile["Core_Fields"]["Diagnoses"]
    assert profile["Core_Fields"]["Treatment_Options"]
    assert profile["Context_Fields"]["Current_Medications"]
    assert profile["Context_Fields"]["Discharge_Medications"]


def test_no_diagnosis_masks_only_diagnosis(clinical_reference):
    profile = mask_profile_for_patient(clinical_reference, ProfileType.NO_DIAGNOSIS)
    assert profile["Core_Fields"]["Diagnoses"] == []
    assert profile["Core_Fields"]["Treatment_Options"]
    assert profile["Context_Fields"]["Current_Medications"]
    assert profile["Context_Fields"]["Discharge_Medications"]


def test_symptom_only_policy_masks_treatment_and_medications(clinical_reference):
    profile = mask_profile_for_patient(clinical_reference, ProfileType.NO_DIAGNOSIS_NO_TREATMENT)
    assert profile["Core_Fields"]["Diagnoses"] == []
    assert profile["Core_Fields"]["Treatment_Options"] == []
    assert profile["Context_Fields"]["Current_Medications"] == []
    assert profile["Context_Fields"]["Discharge_Medications"] == []
    assert profile["Core_Fields"]["Symptoms"]


def test_masking_is_a_deep_copy(clinical_reference):
    profile = mask_profile_for_patient(clinical_reference, ProfileType.FULL)
    profile["Core_Fields"]["Symptoms"][0]["description"] = "changed"
    assert clinical_reference["Core_Fields"]["Symptoms"][0]["description"] == "dry cough"


def test_doctor_context_and_prompt_do_not_receive_hidden_clinical_facts(clinical_reference):
    contexts = build_conversation_contexts(
        clinical_reference, ProfileType.NO_DIAGNOSIS_NO_TREATMENT
    )
    doctor_dict = contexts.doctor.as_dict()
    assert "Core_Fields" not in doctor_dict
    assert "Medical_History" not in doctor_dict["Context_Fields"]
    doctor = DoctorAgent(doctor_context=contexts.doctor, llm=MockLLMProvider([]))
    prompt = doctor.system_message["content"].lower()
    for hidden_value in (
        "dry cough",
        "viral upper respiratory infection",
        "supportive care",
        "lisinopril",
        "penicillin",
    ):
        assert hidden_value not in prompt
