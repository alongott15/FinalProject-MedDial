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

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    """Base for every model here: an explicit ``null`` means "not provided".

    A model asked for an optional descriptive field it cannot fill answers
    ``"details": null`` at least as often as it omits the key. Pydantic treats
    those differently -- an absent key takes the default, ``None`` is a type
    error -- and the whole extraction was discarded over a field carrying no
    clinical content. In one 200-case run that lost 30 references, a third of
    every failure, several of them after 40-odd evidence spans had already been
    located.

    No field in this module is optional, so ``None`` is never meaningful here.
    Dropping it lets the default apply, which is what the model meant.
    """

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _null_means_absent(cls, data: object) -> object:
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value is not None}
        return data


class EvidenceSpan(_Model):
    """Where in a source note an extracted entity came from (KNOW-1)."""

    note_id: str
    char_start: int
    char_end: int
    text: str

    def validation_error(self, source_note: str) -> str | None:
        """Explain why this span does not resolve in ``source_note``.

        Offsets come from a model and therefore cannot be trusted merely
        because they are integers.  Validation is kept explicit rather than
        rejecting the whole SCR: an unresolved entity is a measured/flagged
        extraction defect, not a reason to silently drop the case.
        """
        if self.char_start < 0:
            return "char_start is negative"
        if self.char_end <= self.char_start:
            return "char_end must be greater than char_start"
        if self.char_end > len(source_note):
            return f"char_end {self.char_end} exceeds note length {len(source_note)}"
        resolved = source_note[self.char_start : self.char_end]
        if resolved != self.text:
            return f"offset text {resolved!r} does not equal recorded text {self.text!r}"
        return None

    def resolves_against(self, source_note: str) -> bool:
        """Whether offsets and quoted text agree with a source note exactly."""
        return self.validation_error(source_note) is None


class _Evidenced(_Model):
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


class Core(_Model):
    model_config = ConfigDict(populate_by_name=True)

    symptoms: list[Symptom] = Field(default_factory=list, alias="Symptoms")
    diagnoses: list[Diagnosis] = Field(default_factory=list, alias="Diagnoses")
    treatments: list[TreatmentOption] = Field(
        default_factory=list, alias="Treatment_Options"
    )


class Demographics(_Model):
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


class MedicalHistory(_Model):
    model_config = ConfigDict(populate_by_name=True)

    past_medical_history: str = Field("not provided", alias="Past_Medical_History")


class Context(_Model):
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


class Additional(_Model):
    model_config = ConfigDict(populate_by_name=True)

    chief_complaint: str = Field("not provided", alias="Chief_Complaint")


class StructuredClinicalReference(_Model):
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

    def _evidenced_entities(self) -> list[tuple[str, _Evidenced]]:
        entities: list[tuple[str, _Evidenced]] = []
        groups: list[tuple[str, list[_Evidenced]]] = [
            ("core.symptoms", list(self.core.symptoms)),
            ("core.diagnoses", list(self.core.diagnoses)),
            ("core.treatments", list(self.core.treatments)),
            ("context.current_medications", list(self.context.current_medications)),
            ("context.discharge_medications", list(self.context.discharge_medications)),
        ]
        for path, values in groups:
            entities.extend((f"{path}[{index}]", entity) for index, entity in enumerate(values))
        for treatment_index, treatment in enumerate(self.core.treatments):
            entities.extend(
                (
                    f"core.treatments[{treatment_index}].medications[{medication_index}]",
                    medication,
                )
                for medication_index, medication in enumerate(treatment.medications)
            )
        return entities

    def unevidenced_entities(
        self, source_notes: Mapping[str, str] | None = None
    ) -> list[str]:
        """Dotted paths of entities carrying no evidence span (KNOW-1).

        Reported rather than raised: extraction recall is itself a measured
        quantity (GRND-1/2), so an unevidenced entity is a finding, not a
        crash.
        """
        missing: list[str] = []
        for path, entity in self._evidenced_entities():
            if not entity.evidence:
                missing.append(path)
                continue
            if source_notes is not None and not any(
                span.note_id in source_notes
                and span.resolves_against(source_notes[span.note_id])
                for span in entity.evidence
            ):
                missing.append(path)
        return missing

    def evidence_issues(self, source_notes: Mapping[str, str]) -> dict[str, str]:
        """Return every unresolved span, keyed by its entity/span path."""
        issues: dict[str, str] = {}
        for path, entity in self._evidenced_entities():
            for index, span in enumerate(entity.evidence):
                source_note = source_notes.get(span.note_id)
                if source_note is None:
                    issues[f"{path}.evidence[{index}]"] = (
                        f"unknown note_id {span.note_id!r}"
                    )
                    continue
                error = span.validation_error(source_note)
                if error is not None:
                    issues[f"{path}.evidence[{index}]"] = error
        return issues


# Legacy name. Utils/markdown_gtmf.py and existing serialised references
# continue to work because every field keeps its PascalCase alias.
GTMF = StructuredClinicalReference
