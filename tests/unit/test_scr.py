from __future__ import annotations

import pytest

from gtmf_creation import (
    ClinicalReferenceExtractionError,
    extract_scr_chunked,
    merge_scr_extractions,
    safe_json_parse_object,
)
from Utils.markdown_gtmf import gtmf_to_markdown, markdown_to_gtmf_dict


def test_extraction_parse_failure_is_explicit():
    with pytest.raises(ClinicalReferenceExtractionError):
        safe_json_parse_object("", "chunk_1")
    with pytest.raises(ClinicalReferenceExtractionError):
        safe_json_parse_object("not json", "chunk_2")


def test_all_chunk_extraction_failures_do_not_create_empty_scr():
    class InvalidClient:
        model_name = "mock"

        def chat_completion(self, *args, **kwargs):
            return "no structured content"

    with pytest.raises(ClinicalReferenceExtractionError, match="No valid chunk extractions"):
        extract_scr_chunked("Clinical note without valid extractor output", InvalidClient())


def test_chunk_merge_combines_all_relevant_fields():
    first = {
        "Core_Fields": {
            "Symptoms": [{"description": "cough", "evidence": [{"chunk_index": 0}]}],
            "Diagnoses": [],
            "Treatment_Options": [],
        },
        "Context_Fields": {
            "Patient_Demographics": {"Age": 40},
            "Medical_History": {"Past_Medical_History": "not provided"},
            "Allergies": ["penicillin"],
            "Current_Medications": [{"name": "drug-a", "dosage": "not provided"}],
            "Discharge_Medications": [],
        },
        "Additional_Context": {"Chief_Complaint": "cough"},
        "structured_diagnoses": [{"icd9_code": "1", "description": "one"}],
    }
    second = {
        "Core_Fields": {
            "Symptoms": [
                {"description": "cough", "duration": "3 days", "evidence": [{"chunk_index": 1}]},
                {"description": "fever"},
            ],
            "Diagnoses": [{"primary": "viral illness"}],
            "Treatment_Options": [{"procedure": "supportive care"}],
        },
        "Context_Fields": {
            "Patient_Demographics": {"Sex": "F"},
            "Medical_History": {"Past_Medical_History": "hypertension"},
            "Allergies": ["Penicillin", "latex"],
            "Current_Medications": [{"name": "drug-a", "dosage": "5 mg"}],
            "Discharge_Medications": [{"name": "drug-b"}],
        },
        "Additional_Context": {"Chief_Complaint": "not provided"},
        "structured_procedures": [{"icd9_code": "2", "description": "two"}],
    }
    merged = merge_scr_extractions([first, second])
    assert len(merged["Core_Fields"]["Symptoms"]) == 2
    cough = merged["Core_Fields"]["Symptoms"][0]
    assert cough["duration"] == "3 days"
    assert len(cough["evidence"]) == 2
    assert merged["Context_Fields"]["Medical_History"]["Past_Medical_History"] == "hypertension"
    assert len(merged["Context_Fields"]["Allergies"]) == 2
    assert merged["Context_Fields"]["Current_Medications"][0]["dosage"] == "5 mg"
    assert merged["structured_diagnoses"] and merged["structured_procedures"]


def test_scr_markdown_roundtrip_preserves_evidence(clinical_reference):
    clinical_reference["Core_Fields"]["Symptoms"][0]["evidence"] = [
        {"source_note_id": "n1", "chunk_index": 0}
    ]
    markdown = gtmf_to_markdown(clinical_reference)
    loaded = markdown_to_gtmf_dict(markdown)
    assert markdown.startswith("# Structured Clinical Reference")
    assert loaded["Core_Fields"]["Symptoms"][0]["evidence"][0]["source_note_id"] == "n1"
