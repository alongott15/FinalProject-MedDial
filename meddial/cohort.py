"""Auditable lexical cohort selection with deterministic manifests.

The filter identifies records containing selected lower-acuity terms; it does
not establish a primary-care setting or prove clinical severity.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

INCLUDE_PATTERNS: tuple[str, ...] = (
    r"\bcough\b",
    r"\bsore throat\b",
    r"\brunny nose\b",
    r"\bnasal congestion\b",
    r"\bcommon cold\b",
    r"\bseasonal allerg(?:y|ies)\b",
    r"\bsneez(?:e|ing)\b",
    r"\bsinus (?:pressure|pain)\b",
    r"\bearache\b",
    r"\btension headache\b",
    r"\blow[- ]grade fever\b",
    r"\bmild fever\b",
    r"\bmild nausea\b",
    r"\bupset stomach\b",
    r"\bindigestion\b",
    r"\bheartburn\b",
    r"\bconstipation\b",
    r"\bmild diarrhea\b",
    r"\bminor (?:sprain|strain|wound|bruise)\b",
    r"\bmild (?:rash|skin irritation|swelling)\b",
)

EXCLUDE_PATTERNS: tuple[str, ...] = (
    r"\bicu\b",
    r"\bintubat(?:ed|ion)\b",
    r"\bcardiac arrest\b",
    r"\bseptic shock\b",
    r"\bsepsis\b",
    r"\bmechanical ventilation\b",
    r"\bmulti[- ]?organ failure\b",
    r"\bmetastatic\b",
    r"\bmalignan(?:cy|t)\b",
    r"\bacute respiratory distress syndrome\b",
    r"\bards\b",
    r"\bmajor trauma\b",
    r"\bintracranial hemorrhage\b",
    r"\bacute stroke\b",
    r"\bstemi\b",
    r"\bnstemi\b",
    r"\bmyocardial infarction\b",
    r"\bacute coronary syndrome\b",
    r"\bcardiogenic shock\b",
    r"\bcomplete heart block\b",
    r"\bpulmonary embol(?:ism|us)\b",
    r"\baortic dissection\b",
    r"\bdiabetic ketoacidosis\b",
    r"\bstatus epilepticus\b",
    r"\bgastrointestinal bleed(?:ing)?\b",
)

STRUCTURED_EXCLUSION_FLAGS: tuple[str, ...] = (
    "has_icu_stay",
    "hospital_expire_flag",
    "mechanical_ventilation",
    "major_surgery",
    "major_trauma",
    "active_malignancy",
)


@dataclass(frozen=True)
class CohortFilterResult:
    passed: bool
    reason: str
    matched_inclusions: tuple[str, ...] = ()
    matched_exclusions: tuple[str, ...] = ()
    filter_version: str = "lexical-v3"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_lower_acuity_candidate(
    note_text: str,
    chief_complaint: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> CohortFilterResult:
    metadata = metadata or {}
    structured_exclusions = tuple(
        flag for flag in STRUCTURED_EXCLUSION_FLAGS if bool(metadata.get(flag))
    )
    age = metadata.get("age")
    try:
        is_minor = age is not None and float(age) < 18
    except (TypeError, ValueError):
        is_minor = False
    admission_type = str(metadata.get("admission_type", "")).strip().lower()
    if is_minor:
        structured_exclusions += ("age_under_18",)
    if admission_type in {"newborn", "neonatal"}:
        structured_exclusions += ("neonatal_admission",)
    if structured_exclusions:
        return CohortFilterResult(
            False,
            "Excluded by structured acuity/demographic criteria",
            matched_exclusions=structured_exclusions,
        )
    text = f"{chief_complaint}\n{note_text}".lower()
    exclusions = tuple(
        pattern for pattern in EXCLUDE_PATTERNS if re.search(pattern, text, re.IGNORECASE)
    )
    if exclusions:
        return CohortFilterResult(
            False,
            "Excluded by one or more high-acuity lexical indicators",
            matched_exclusions=exclusions,
        )
    inclusions = tuple(
        pattern for pattern in INCLUDE_PATTERNS if re.search(pattern, text, re.IGNORECASE)
    )
    if not inclusions:
        return CohortFilterResult(False, "No eligible lower-acuity lexical indicator found")
    return CohortFilterResult(
        True,
        "Eligible lower-acuity lexical candidate; clinical severity is not established",
        matched_inclusions=inclusions,
    )


def _selection_key(note: Mapping[str, Any], seed: int) -> str:
    identity = f"{note.get('subject_id')}:{note.get('hadm_id')}:{note.get('row_id')}"
    return hashlib.sha256(f"{seed}:{identity}".encode()).hexdigest()


def deterministic_sample(
    notes: Sequence[Mapping[str, Any]],
    limit: int,
    seed: int = 42,
    one_per_patient: bool = True,
) -> list[dict[str, Any]]:
    if limit < 0:
        raise ValueError("limit must be non-negative")
    ordered = sorted(notes, key=lambda note: _selection_key(note, seed))
    selected: list[dict[str, Any]] = []
    seen_patients: set[str] = set()
    for note in ordered:
        patient_id = str(note.get("subject_id"))
        if one_per_patient and patient_id in seen_patients:
            continue
        seen_patients.add(patient_id)
        selected.append(dict(note))
        if len(selected) == limit:
            break
    return selected


def create_cohort_manifest(
    selected: Sequence[Mapping[str, Any]],
    seed: int,
    source: str,
) -> dict[str, Any]:
    entries = [
        {
            "subject_id": note.get("subject_id"),
            "hadm_id": note.get("hadm_id"),
            "row_id": note.get("row_id"),
            "selection_key": _selection_key(note, seed),
            "filter": note.get("light_case_filter"),
            "clinical_review_status": note.get("clinical_review_status", "not_recorded"),
        }
        for note in selected
    ]
    payload = {
        "manifest_version": "2.0",
        "filter_version": "lexical-v3",
        "data_classification": "restricted_clinical",
        "publishable": False,
        "seed": seed,
        "source": source,
        "selected_count": len(entries),
        "selected": entries,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["manifest_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def create_release_manifest(
    private_manifest: Mapping[str, Any],
    *,
    publication_salt: str,
) -> dict[str, Any]:
    """Create a public manifest without MIMIC identifiers or reversible selection keys."""

    if len(publication_salt) < 16:
        raise ValueError("publication_salt must contain at least 16 characters")
    selected = []
    for position, entry in enumerate(private_manifest.get("selected", []), start=1):
        identity = f"{entry.get('subject_id')}:{entry.get('hadm_id')}:{entry.get('row_id')}"
        digest = hmac.new(
            publication_salt.encode("utf-8"), identity.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:20]
        selected.append(
            {
                "study_id": f"MEDDIAL-{digest}",
                "selection_order": position,
                "clinical_review_status": entry.get("clinical_review_status", "not_recorded"),
            }
        )
    payload = {
        "manifest_version": "2.0-release",
        "filter_version": private_manifest.get("filter_version", "lexical-v3"),
        "data_classification": "public_metadata_only",
        "publishable": True,
        "selected_count": len(selected),
        "selected": selected,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["manifest_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def save_cohort_manifest(path: str | Path, manifest: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)


def load_manifest_selection(
    path: str | Path, available_notes: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("publishable") is True:
        raise ValueError("Release manifests intentionally cannot resolve MIMIC source records")
    available = {
        (note.get("subject_id"), note.get("hadm_id"), note.get("row_id")): note
        for note in available_notes
    }
    selected: list[dict[str, Any]] = []
    missing: list[tuple[Any, Any, Any]] = []
    for entry in manifest.get("selected", []):
        key = (entry.get("subject_id"), entry.get("hadm_id"), entry.get("row_id"))
        if key not in available:
            missing.append(key)
        else:
            selected.append(dict(available[key]))
    if missing:
        raise ValueError(f"Manifest references {len(missing)} unavailable notes")
    return selected
