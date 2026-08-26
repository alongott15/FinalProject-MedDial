"""Pydantic v2 models for the Structured Clinical Reference (SCR).

``GTMF`` remains an alias so old imports and saved artifacts continue to load.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MedDialModel(BaseModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)


class EvidenceProvenance(MedDialModel):
    source_note_id: str = "not provided"
    chunk_index: int | None = None
    character_start: int | None = None
    character_end: int | None = None
    excerpt: str | None = None
    extractor: str | None = None
    model: str | None = None
    extraction_metadata: dict[str, Any] = Field(default_factory=dict)


class Symptom(MedDialModel):
    description: str
    onset: str = "not provided"
    duration: str = "not provided"
    severity: str = "not provided"
    evidence: list[EvidenceProvenance] = Field(default_factory=list)


class Diagnosis(MedDialModel):
    primary: str
    notes: str = "not provided"
    evidence: list[EvidenceProvenance] = Field(default_factory=list)


class Medication(MedDialModel):
    name: str
    purpose: str = "not provided"
    dosage: str = "not provided"
    frequency: str = "not provided"
    evidence: list[EvidenceProvenance] = Field(default_factory=list)


class TreatmentOption(MedDialModel):
    procedure: str = "not provided"
    details: str = "not provided"
    treatment: str = "not provided"
    medications: list[Medication] = Field(default_factory=list)
    evidence: list[EvidenceProvenance] = Field(default_factory=list)


class CoreFields(MedDialModel):
    Symptoms: list[Symptom] = Field(default_factory=list)
    Diagnoses: list[Diagnosis] = Field(default_factory=list)
    Treatment_Options: list[TreatmentOption] = Field(default_factory=list)


class PatientDemographics(MedDialModel):
    Date_of_Birth: str = "not provided"
    Age: int = 0
    Sex: str = "not provided"
    Religion: str = "not provided"
    Marital_Status: str = "not provided"
    Ethnicity: str = "not provided"
    Insurance: str = "not provided"
    Admission_Type: str = "not provided"
    Admission_Date: str = "not provided"
    Discharge_Date: str = "not provided"


class MedicalHistory(MedDialModel):
    Past_Medical_History: str = "not provided"
    evidence: list[EvidenceProvenance] = Field(default_factory=list)


class ContextFields(MedDialModel):
    Patient_Demographics: PatientDemographics = Field(default_factory=PatientDemographics)
    Medical_History: MedicalHistory = Field(default_factory=MedicalHistory)
    Allergies: list[str] = Field(default_factory=list)
    Current_Medications: list[Medication] = Field(default_factory=list)
    Discharge_Medications: list[Medication] = Field(default_factory=list)


class AdditionalContext(MedDialModel):
    Chief_Complaint: str = "not provided"
    evidence: list[EvidenceProvenance] = Field(default_factory=list)


class StructuredClinicalReference(MedDialModel):
    """Validated clinical reference extracted from an EHR note."""

    schema_name: str = "Structured Clinical Reference"
    schema_version: str = "1.0"
    extraction_status: str = "VALID"
    row_id: int = 0
    subject_id: int = 0
    hadm_id: int = 0
    Core_Fields: CoreFields = Field(default_factory=CoreFields)
    Context_Fields: ContextFields = Field(default_factory=ContextFields)
    Additional_Context: AdditionalContext = Field(default_factory=AdditionalContext)
    reference_evidence: list[EvidenceProvenance] = Field(default_factory=list)


SCR = StructuredClinicalReference
GTMF = StructuredClinicalReference

__all__ = [
    "AdditionalContext",
    "ContextFields",
    "CoreFields",
    "Diagnosis",
    "EvidenceProvenance",
    "GTMF",
    "MedicalHistory",
    "Medication",
    "PatientDemographics",
    "SCR",
    "StructuredClinicalReference",
    "Symptom",
    "TreatmentOption",
]
