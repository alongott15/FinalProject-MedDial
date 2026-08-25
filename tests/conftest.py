from __future__ import annotations

import pytest


@pytest.fixture
def clinical_reference() -> dict:
    return {
        "row_id": 7,
        "subject_id": 101,
        "hadm_id": 202,
        "Core_Fields": {
            "Symptoms": [
                {
                    "description": "dry cough",
                    "onset": "three days ago",
                    "duration": "three days",
                    "severity": "mild",
                }
            ],
            "Diagnoses": [{"primary": "viral upper respiratory infection"}],
            "Treatment_Options": [
                {
                    "procedure": "supportive care",
                    "treatment": "rest and hydration",
                    "medications": [{"name": "acetaminophen"}],
                }
            ],
        },
        "Context_Fields": {
            "Patient_Demographics": {"Age": 40, "Sex": "F"},
            "Medical_History": {"Past_Medical_History": "hypertension"},
            "Allergies": ["penicillin"],
            "Current_Medications": [{"name": "lisinopril"}],
            "Discharge_Medications": [{"name": "acetaminophen"}],
        },
        "Additional_Context": {"Chief_Complaint": "cough"},
    }
