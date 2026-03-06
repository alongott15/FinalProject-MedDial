from pydantic import BaseModel # For Pydantic V2, no change needed here. For V1, it's also just BaseModel
from typing import List, Optional # Optional can be useful

# Core Fields
class Symptom(BaseModel):
    description: str
    onset: str = "not provided" # Added default
    duration: str = "not provided" # Added default
    severity: str = "not provided" # Added default

class Diagnosis(BaseModel):
    primary: str
    notes: str = "not provided" # Added default

class Medication(BaseModel):
    name: str
    purpose: str = "not provided" # Added default
    dosage: str = "not provided" # Added default
    frequency: str = "not provided" # Added default

class TreatmentOption(BaseModel):
    procedure: str
    details: str = "not provided" # Added default
    treatment: str = "not provided" # Added default
    medications: List[Medication] = []

class CoreFields(BaseModel):
    Symptoms: List[Symptom] = []
    Diagnoses: List[Diagnosis] = []
    Treatment_Options: List[TreatmentOption] = []

class PatientDemographics(BaseModel):
    Date_of_Birth: str = "not provided" # Added default
    Age: int = 0 # Added default
    Sex: str = "not provided" # Added default
    Religion: str = "not provided" # Added default
    Marital_Status: str = "not provided" # Added default
    Ethnicity: str = "not provided" # Added default
    Insurance: str = "not provided" # Added default
    Admission_Type: str = "not provided" # Added default
    Admission_Date: str = "not provided" # Added default
    Discharge_Date: str = "not provided" # Added default

class MedicalHistory(BaseModel):
    Past_Medical_History: str = "not provided" # Added default

class ContextFields(BaseModel):
    Patient_Demographics: PatientDemographics # Will default if not provided due to PatientDemographics defaults
    Medical_History: MedicalHistory # Will default
    Allergies: List[str] = []
    Current_Medications: List[Medication] = []
    Discharge_Medications: List[Medication] = []

class AdditionalContext(BaseModel):
    Chief_Complaint: str = "not provided" # Added default

# The complete GTMF model
class GTMF(BaseModel):
    row_id: int = 0
    subject_id: int = 0
    hadm_id: int = 0
    Core_Fields: CoreFields
    Context_Fields: ContextFields
    Additional_Context: AdditionalContext