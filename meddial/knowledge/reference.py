"""The Structured Clinical Reference (SCR) and its field-level provenance.

Replaces the flat ``GTMF`` model. Two changes matter:

* every extracted entity carries the ``EvidenceSpan`` it came from, so an
  entity with no evidence is *flagged* rather than silently trusted
  (KNOW-1);
* field names are the canonical dotted paths the policies address
  (``core.diagnoses``, ``context.current_medications``), while the legacy
  PascalCase names survive as pydantic aliases so ``Utils/markdown_gtmf.py``
  and every existing serialised reference keep loading unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EvidenceSpan(BaseModel):
    """Where in a source note an extracted entity came from (KNOW-1)."""

    note_id: str
    char_start: int
    char_end: int
    text: str


class _Evidenced(BaseModel):
    """Base for entities that must be traceable to a source note."""

    model_config = ConfigDict(populate_by_name=True)

    evidence: list[EvidenceSpan] = Field(default_factory=list)

    @property
    def is_evidenced(self) -> bool:
        return bool(self.evidence)


class Symptom(_Evidenced):
    description: str
    onset: str = "not provided"
    duration: str = "not provided"
    severity: str = "not provided"


class Diagnosis(_Evidenced):
    primary: str
    notes: str = "not provided"


class Medication(_Evidenced):
    name: str
    # Names the condition the drug treats, which is why medications are
    # dropped entirely under both no-diagnosis policies (KNOW-5).
    purpose: str = "not provided"
    dosage: str = "not provided"
    frequency: str = "not provided"


class TreatmentOption(_Evidenced):
    procedure: str
    details: str = "not provided"
    treatment: str = "not provided"
    # Nested medications are the last diagnosis route, closed by NDNT.
    medications: list[Medication] = Field(default_factory=list)


class Core(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    symptoms: list[Symptom] = Field(default_factory=list, alias="Symptoms")
    diagnoses: list[Diagnosis] = Field(default_factory=list, alias="Diagnoses")
    treatments: list[TreatmentOption] = Field(
        default_factory=list, alias="Treatment_Options"
    )


class Demographics(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    date_of_birth: str = Field("not provided", alias="Date_of_Birth")
    age: int = Field(0, alias="Age")
    sex: str = Field("not provided", alias="Sex")
    religion: str = Field("not provided", alias="Religion")
    marital_status: str = Field("not provided", alias="Marital_Status")
    ethnicity: str = Field("not provided", alias="Ethnicity")
    insurance: str = Field("not provided", alias="Insurance")
    admission_type: str = Field("not provided", alias="Admission_Type")
    admission_date: str = Field("not provided", alias="Admission_Date")
    discharge_date: str = Field("not provided", alias="Discharge_Date")


class MedicalHistory(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    past_medical_history: str = Field("not provided", alias="Past_Medical_History")


class Context(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    demographics: Demographics = Field(
        default_factory=Demographics, alias="Patient_Demographics"
    )
    medical_history: MedicalHistory = Field(
        default_factory=MedicalHistory, alias="Medical_History"
    )
    allergies: list[str] = Field(default_factory=list, alias="Allergies")
    current_medications: list[Medication] = Field(
        default_factory=list, alias="Current_Medications"
    )
    discharge_medications: list[Medication] = Field(
        default_factory=list, alias="Discharge_Medications"
    )


class Additional(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    chief_complaint: str = Field("not provided", alias="Chief_Complaint")


class StructuredClinicalReference(BaseModel):
    """The privileged, unmasked record for one admission."""

    model_config = ConfigDict(populate_by_name=True)

    row_id: int = 0
    subject_id: int = 0
    hadm_id: int = 0
    core: Core = Field(default_factory=Core, alias="Core_Fields")
    context: Context = Field(default_factory=Context, alias="Context_Fields")
    additional: Additional = Field(
        default_factory=Additional, alias="Additional_Context"
    )

    @property
    def case_id(self) -> str:
        """Stable per-case key. Analyses are clustered on this (STAT-1)."""
        return f"{self.subject_id}_{self.hadm_id}"

    def unevidenced_entities(self) -> list[str]:
        """Dotted paths of entities carrying no evidence span (KNOW-1).

        Reported rather than raised: extraction recall is itself a measured
        quantity (GRND-1/2), so an unevidenced entity is a finding, not a
        crash.
        """
        missing: list[str] = []
        groups: list[tuple[str, list[_Evidenced]]] = [
            ("core.symptoms", list(self.core.symptoms)),
            ("core.diagnoses", list(self.core.diagnoses)),
            ("core.treatments", list(self.core.treatments)),
            ("context.current_medications", list(self.context.current_medications)),
            ("context.discharge_medications", list(self.context.discharge_medications)),
        ]
        for path, entities in groups:
            for index, entity in enumerate(entities):
                if not entity.is_evidenced:
                    missing.append(f"{path}[{index}]")
        for t_index, treatment in enumerate(self.core.treatments):
            for m_index, med in enumerate(treatment.medications):
                if not med.is_evidenced:
                    missing.append(f"core.treatments[{t_index}].medications[{m_index}]")
        return missing


# Legacy name. Utils/markdown_gtmf.py and existing serialised references
# continue to work because every field keeps its PascalCase alias.
GTMF = StructuredClinicalReference
