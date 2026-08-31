"""Versioned, structured cohort exclusion criteria (COH-1).

The old cohort path searched discharge-note prose for reassuring or alarming
words.  That makes eligibility depend on wording rather than on the admission.
This module evaluates the ten criteria in Appendix C from structured MIMIC-III
fields.  E1--E9 are evaluated per admission here; E10 (the first qualifying
admission per subject) is applied by :mod:`meddial.cohort.select` because it
requires seeing the whole candidate pool.

The defaults are part of the instrument.  Their canonical hash is written to
the private cohort manifest so changing a threshold changes the cohort identity
even if the human-readable version was accidentally left unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any


class CandidateValidationError(ValueError):
    """A candidate lacks fields needed to apply the criteria safely."""


class CriterionCode(str, Enum):
    """Stable identifiers used in audit records and exclusion-flow tables."""

    ICU_STAY = "E1"
    IN_HOSPITAL_DEATH = "E2"
    MECHANICAL_VENTILATION = "E3"
    PAEDIATRIC_OR_NEWBORN = "E4"
    AGE_90_OR_OVER = "E5"
    HIGH_ACUITY_DIAGNOSIS = "E6"
    LENGTH_OF_STAY = "E7"
    CHARLSON_COMORBIDITY = "E8"
    INSUFFICIENT_NOTE = "E9"
    LATER_QUALIFYING_ADMISSION = "E10"


CRITERION_ORDER: tuple[CriterionCode, ...] = tuple(CriterionCode)

CRITERION_LABELS: Mapping[CriterionCode, str] = {
    CriterionCode.ICU_STAY: "any ICU stay for the admission",
    CriterionCode.IN_HOSPITAL_DEATH: "in-hospital death",
    CriterionCode.MECHANICAL_VENTILATION: "mechanical ventilation or intubation",
    CriterionCode.PAEDIATRIC_OR_NEWBORN: "newborn or age under 18",
    CriterionCode.AGE_90_OR_OVER: "age 90 or over",
    CriterionCode.HIGH_ACUITY_DIAGNOSIS: "high-acuity ICD-9 diagnosis",
    CriterionCode.LENGTH_OF_STAY: "length of stay above threshold",
    CriterionCode.CHARLSON_COMORBIDITY: "Charlson index above threshold",
    CriterionCode.INSUFFICIENT_NOTE: "insufficient note content",
    CriterionCode.LATER_QUALIFYING_ADMISSION: "not first qualifying admission",
}


MECHANICAL_VENTILATION_CODES = frozenset({"9670", "9671", "9672", "9604"})


@dataclass(frozen=True)
class CohortCriteria:
    """The complete versioned criteria set used for one selection run."""

    criteria_id: str = "mimiciii_structured_lower_acuity"
    version: str = "1.0"
    minimum_age_years: float = 18.0
    maximum_age_years_exclusive: float = 90.0
    maximum_length_of_stay_days: float = 7.0
    maximum_charlson_score: int = 2
    minimum_note_characters: int = 500
    allowed_note_categories: tuple[str, ...] = ("discharge summary",)
    mechanical_ventilation_codes: frozenset[str] = MECHANICAL_VENTILATION_CODES

    def validate(self) -> None:
        if not self.criteria_id or not self.version:
            raise ValueError("criteria_id and version must be non-empty")
        if self.minimum_age_years < 0:
            raise ValueError("minimum_age_years must be non-negative")
        if self.maximum_age_years_exclusive <= self.minimum_age_years:
            raise ValueError("maximum age must be greater than minimum age")
        if self.maximum_length_of_stay_days <= 0:
            raise ValueError("maximum_length_of_stay_days must be positive")
        if self.maximum_charlson_score < 0:
            raise ValueError("maximum_charlson_score must be non-negative")
        if self.minimum_note_characters <= 0:
            raise ValueError("minimum_note_characters must be positive")
        if not self.allowed_note_categories:
            raise ValueError("at least one note category must be allowed")

    @property
    def key(self) -> str:
        return f"{self.criteria_id}@{self.version}"

    @property
    def content_hash(self) -> str:
        """A stable SHA-256 over every selection-relevant setting."""
        body = asdict(self)
        body["mechanical_ventilation_codes"] = sorted(self.mechanical_ventilation_codes)
        body["allowed_note_categories"] = list(self.allowed_note_categories)
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


DEFAULT_CRITERIA = CohortCriteria()


@dataclass(frozen=True)
class AdmissionRecord:
    """One admission and only the structured fields needed for selection.

    ``note_text`` is consulted only for E9's deterministic adequacy threshold;
    it is never searched for diagnoses, severity, or inclusion keywords.
    """

    subject_id: int
    hadm_id: int
    admittime: datetime
    dischtime: datetime
    age_years: float
    admission_type: str
    has_icu_stay: bool
    hospital_expire_flag: bool
    procedure_icd9_codes: tuple[str, ...]
    diagnosis_icd9_codes: tuple[str, ...]
    note_text: str
    note_category: str
    deathtime: datetime | None = None
    row_id: int | None = None

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> AdmissionRecord:
        """Build a record from a database/CSV-style mapping.

        The explicit required-key check prevents a missing join column from
        looking like a reassuring ``False`` value.
        """
        required = {
            "subject_id",
            "hadm_id",
            "admittime",
            "dischtime",
            "age_years",
            "admission_type",
            "has_icu_stay",
            "hospital_expire_flag",
            "procedure_icd9_codes",
            "diagnosis_icd9_codes",
            "note_text",
            "note_category",
        }
        missing = required - set(row)
        if missing:
            raise CandidateValidationError(
                f"candidate is missing required fields: {', '.join(sorted(missing))}"
            )
        return cls(
            subject_id=int(row["subject_id"]),
            hadm_id=int(row["hadm_id"]),
            admittime=_as_datetime(row["admittime"], "admittime"),
            dischtime=_as_datetime(row["dischtime"], "dischtime"),
            age_years=float(row["age_years"]),
            admission_type=str(row["admission_type"] or ""),
            has_icu_stay=_as_bool(row["has_icu_stay"], "has_icu_stay"),
            hospital_expire_flag=_as_bool(row["hospital_expire_flag"], "hospital_expire_flag"),
            procedure_icd9_codes=_as_codes(row["procedure_icd9_codes"]),
            diagnosis_icd9_codes=_as_codes(row["diagnosis_icd9_codes"]),
            note_text=str(row["note_text"] or ""),
            note_category=str(row["note_category"] or ""),
            deathtime=(
                None
                if row.get("deathtime") in (None, "")
                else _as_datetime(row["deathtime"], "deathtime")
            ),
            row_id=None if row.get("row_id") is None else int(row["row_id"]),
        )

    @property
    def identity(self) -> tuple[int, int]:
        return self.subject_id, self.hadm_id

    @property
    def length_of_stay_days(self) -> float:
        return (self.dischtime - self.admittime).total_seconds() / 86_400.0


@dataclass(frozen=True)
class CharlsonResult:
    score: int
    conditions: tuple[str, ...]


@dataclass(frozen=True)
class CriteriaEvaluation:
    """Per-case evidence for every exclusion decision."""

    record: AdmissionRecord
    fired: tuple[CriterionCode, ...]
    length_of_stay_days: float
    charlson: CharlsonResult

    @property
    def eligible(self) -> bool:
        return not self.fired

    def add(self, criterion: CriterionCode) -> CriteriaEvaluation:
        if criterion in self.fired:
            return self
        ordered = tuple(code for code in CRITERION_ORDER if code in {*self.fired, criterion})
        return replace(self, fired=ordered)


def evaluate_admission(
    record: AdmissionRecord,
    criteria: CohortCriteria = DEFAULT_CRITERIA,
) -> CriteriaEvaluation:
    """Evaluate E1--E9 for one admission without lexical clinical matching."""
    criteria.validate()
    _validate_record(record)
    fired: list[CriterionCode] = []

    if record.has_icu_stay:
        fired.append(CriterionCode.ICU_STAY)
    if record.hospital_expire_flag or record.deathtime is not None:
        fired.append(CriterionCode.IN_HOSPITAL_DEATH)
    if any(
        normalise_icd9(code) in criteria.mechanical_ventilation_codes
        for code in record.procedure_icd9_codes
    ):
        fired.append(CriterionCode.MECHANICAL_VENTILATION)

    admission_type = record.admission_type.strip().casefold()
    if admission_type in {"newborn", "neonatal"} or record.age_years < criteria.minimum_age_years:
        fired.append(CriterionCode.PAEDIATRIC_OR_NEWBORN)
    if record.age_years >= criteria.maximum_age_years_exclusive:
        fired.append(CriterionCode.AGE_90_OR_OVER)
    if any(is_high_acuity_icd9(code) for code in record.diagnosis_icd9_codes):
        fired.append(CriterionCode.HIGH_ACUITY_DIAGNOSIS)

    length_of_stay = record.length_of_stay_days
    if length_of_stay > criteria.maximum_length_of_stay_days:
        fired.append(CriterionCode.LENGTH_OF_STAY)

    charlson = calculate_charlson(record.diagnosis_icd9_codes)
    if charlson.score > criteria.maximum_charlson_score:
        fired.append(CriterionCode.CHARLSON_COMORBIDITY)

    category = record.note_category.strip().casefold()
    allowed = {item.strip().casefold() for item in criteria.allowed_note_categories}
    if category not in allowed or len(record.note_text.strip()) < criteria.minimum_note_characters:
        fired.append(CriterionCode.INSUFFICIENT_NOTE)

    return CriteriaEvaluation(
        record=record,
        fired=tuple(fired),
        length_of_stay_days=length_of_stay,
        charlson=charlson,
    )


def normalise_icd9(code: str) -> str:
    """Canonical comparison form for an ICD-9 code."""
    return re.sub(r"[^A-Z0-9]", "", str(code).strip().upper())


def is_high_acuity_icd9(code: str) -> bool:
    """Appendix C.2 high-acuity diagnosis sets."""
    value = normalise_icd9(code)
    if not value:
        return False
    if value.startswith(("038", "410", "7855")):
        return True
    if value in {"99591", "99592", "78552", "4275", "51881", "51884"}:
        return True
    category = _numeric_category(value)
    return category is not None and (140 <= category <= 239 or 430 <= category <= 438)


# Quan et al. ICD-9 Charlson categories.  Scores are deduplicated by condition,
# and the severe form supersedes its corresponding mild category.
CHARLSON_WEIGHTS: Mapping[str, int] = {
    "myocardial_infarction": 1,
    "congestive_heart_failure": 1,
    "peripheral_vascular_disease": 1,
    "cerebrovascular_disease": 1,
    "dementia": 1,
    "chronic_pulmonary_disease": 1,
    "rheumatic_disease": 1,
    "peptic_ulcer_disease": 1,
    "mild_liver_disease": 1,
    "diabetes_without_complication": 1,
    "diabetes_with_complication": 2,
    "hemiplegia_or_paraplegia": 2,
    "renal_disease": 2,
    "malignancy": 2,
    "moderate_or_severe_liver_disease": 3,
    "metastatic_solid_tumour": 6,
    "aids_hiv": 6,
}


def calculate_charlson(codes: Iterable[str]) -> CharlsonResult:
    """Compute the diagnosis-code component of the Charlson index.

    This intentionally does not add age points: age is an independent cohort
    criterion (E4/E5), and Appendix C asks for Charlson *from ICD-9*.
    """
    conditions: set[str] = set()
    for raw in codes:
        code = normalise_icd9(raw)
        if code:
            conditions.update(_charlson_conditions(code))

    if "diabetes_with_complication" in conditions:
        conditions.discard("diabetes_without_complication")
    if "moderate_or_severe_liver_disease" in conditions:
        conditions.discard("mild_liver_disease")
    if "metastatic_solid_tumour" in conditions:
        conditions.discard("malignancy")

    ordered = tuple(sorted(conditions))
    return CharlsonResult(
        score=sum(CHARLSON_WEIGHTS[name] for name in ordered),
        conditions=ordered,
    )


def _charlson_conditions(code: str) -> set[str]:
    found: set[str] = set()
    category = _numeric_category(code)

    if _starts_any(code, "410", "412"):
        found.add("myocardial_infarction")
    if _starts_any(
        code,
        "39891",
        "40201",
        "40211",
        "40291",
        "40401",
        "40403",
        "40411",
        "40413",
        "40491",
        "40493",
        "428",
    ) or _four_digit_between(code, 4254, 4259):
        found.add("congestive_heart_failure")
    if _starts_any(
        code,
        "0930",
        "4373",
        "440",
        "441",
        "4431",
        "4432",
        "4438",
        "4439",
        "4471",
        "5571",
        "5579",
        "V434",
    ):
        found.add("peripheral_vascular_disease")
    if code.startswith("36234") or (category is not None and 430 <= category <= 438):
        found.add("cerebrovascular_disease")
    if _starts_any(code, "290", "2941", "3312"):
        found.add("dementia")
    if (
        _starts_any(code, "4168", "4169", "5064", "5081", "5088")
        or category is not None
        and 490 <= category <= 505
    ):
        found.add("chronic_pulmonary_disease")
    if _starts_any(code, "4465", "7140", "7141", "7142", "7148", "725") or _four_digit_between(
        code, 7100, 7104
    ):
        found.add("rheumatic_disease")
    if category is not None and 531 <= category <= 534:
        found.add("peptic_ulcer_disease")
    if _starts_any(
        code,
        "07022",
        "07023",
        "07032",
        "07033",
        "07044",
        "07054",
        "0706",
        "0709",
        "570",
        "571",
        "5733",
        "5734",
        "5738",
        "5739",
        "V427",
    ):
        found.add("mild_liver_disease")
    if code.startswith("250") and len(code) >= 4 and code[3].isdigit():
        manifestation = int(code[3])
        if manifestation in {4, 5, 6, 7}:
            found.add("diabetes_with_complication")
        elif manifestation in {0, 1, 2, 3, 8, 9}:
            found.add("diabetes_without_complication")
    if _starts_any(
        code, "3341", "342", "343", "3440", "3441", "3442", "3443", "3444", "3445", "3446", "3449"
    ):
        found.add("hemiplegia_or_paraplegia")
    if _starts_any(
        code,
        "40301",
        "40311",
        "40391",
        "40402",
        "40403",
        "40412",
        "40413",
        "40492",
        "40493",
        "582",
        "585",
        "586",
        "5880",
        "V420",
        "V451",
        "V56",
    ) or _four_digit_between(code, 5830, 5837):
        found.add("renal_disease")
    if (
        category is not None
        and (140 <= category <= 172 or 174 <= category <= 195 or 200 <= category <= 208)
    ) or code.startswith("2386"):
        found.add("malignancy")
    if _starts_any(code, "4560", "4561", "4562") or (_four_digit_between(code, 5722, 5728)):
        found.add("moderate_or_severe_liver_disease")
    if category is not None and 196 <= category <= 199:
        found.add("metastatic_solid_tumour")
    if category is not None and 42 <= category <= 44 and code.startswith("0"):
        found.add("aids_hiv")
    return found


def _validate_record(record: AdmissionRecord) -> None:
    if record.subject_id <= 0 or record.hadm_id <= 0:
        raise CandidateValidationError("subject_id and hadm_id must be positive")
    if record.age_years < 0:
        raise CandidateValidationError("age_years must be non-negative")
    if record.dischtime < record.admittime:
        raise CandidateValidationError("dischtime precedes admittime")


def _as_datetime(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise CandidateValidationError(
            f"{field_name} is not an ISO-8601 datetime: {value!r}"
        ) from exc


def _as_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"true", "t", "1", "yes"}:
        return True
    if text in {"false", "f", "0", "no"}:
        return False
    raise CandidateValidationError(f"{field_name} is not boolean: {value!r}")


def _as_codes(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip().strip("{}")
        return tuple(item.strip().strip('"') for item in stripped.split(",") if item.strip())
    return tuple(str(item) for item in value if item is not None)


def _numeric_category(code: str) -> int | None:
    if not code or not code[0].isdigit():
        return None
    try:
        return int(code[:3])
    except ValueError:
        return None


def _starts_any(code: str, *prefixes: str) -> bool:
    return any(code.startswith(prefix) for prefix in prefixes)


def _four_digit_between(code: str, start: int, end: int) -> bool:
    if len(code) < 4 or not code[:4].isdigit():
        return False
    return start <= int(code[:4]) <= end
