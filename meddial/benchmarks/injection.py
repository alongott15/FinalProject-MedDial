"""Seeded, labelled dialogue corruptions for detector validation (BENCH-1)."""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from enum import Enum

from meddial.evaluation import DOCTOR_ROLE, PATIENT_ROLE, Turn
from meddial.knowledge import StructuredClinicalReference


class CorruptionType(str, Enum):
    PATIENT_DIAGNOSIS_LEAKAGE = "patient_diagnosis_leakage"
    PATIENT_TREATMENT_LEAKAGE = "patient_treatment_leakage"
    FABRICATED_PATIENT_SYMPTOM = "fabricated_patient_symptom"
    DOCTOR_HIDDEN_FACT_LEAKAGE = "doctor_hidden_fact_leakage"
    UNSUPPORTED_DOCTOR_FACT = "unsupported_doctor_fact"
    ROLE_ORDER_VIOLATION = "role_order_violation"
    EMPTY_TURN = "empty_turn"


EXPECTED_DETECTOR = {
    CorruptionType.PATIENT_DIAGNOSIS_LEAKAGE: "knowledge_boundary",
    CorruptionType.PATIENT_TREATMENT_LEAKAGE: "knowledge_boundary",
    CorruptionType.FABRICATED_PATIENT_SYMPTOM: "patient_factuality",
    CorruptionType.DOCTOR_HIDDEN_FACT_LEAKAGE: "knowledge_boundary",
    CorruptionType.UNSUPPORTED_DOCTOR_FACT: "doctor_factuality",
    CorruptionType.ROLE_ORDER_VIOLATION: "structural_validity",
    CorruptionType.EMPTY_TURN: "structural_validity",
}


class InjectionError(ValueError):
    """A clean dialogue/reference cannot support the requested corruption."""


@dataclass(frozen=True)
class InjectedError:
    corruption_type: CorruptionType
    turn_index: int
    label: str
    expected_detector: str
    field_path: str | None = None

    def as_record(self) -> dict[str, str | int | None]:
        return {
            "corruption_type": self.corruption_type.value,
            "turn_index": self.turn_index,
            "label": self.label,
            "expected_detector": self.expected_detector,
            "field_path": self.field_path,
        }


@dataclass(frozen=True)
class InjectionResult:
    clean: tuple[Turn, ...]
    corrupted: tuple[Turn, ...]
    error: InjectedError
    seed: int


def inject_fault(
    turns: tuple[Turn, ...] | list[Turn],
    reference: StructuredClinicalReference,
    corruption_type: CorruptionType | str,
    *,
    seed: int,
) -> InjectionResult:
    """Return a corrupted copy and the exact ground-truth label behind it."""

    clean = tuple(turns)
    if not clean:
        raise InjectionError("cannot corrupt an empty dialogue")
    try:
        kind = CorruptionType(corruption_type)
    except ValueError as exc:
        raise InjectionError(f"unknown corruption type {corruption_type!r}") from exc
    rng = random.Random(seed)
    corrupted = list(clean)

    if kind is CorruptionType.ROLE_ORDER_VIOLATION:
        if len(clean) < 2:
            raise InjectionError("role-order corruption requires at least two turns")
        position = rng.randrange(1, len(clean))
        corrupted[position] = replace(clean[position], role=clean[position - 1].role)
        return _result(
            clean,
            corrupted,
            kind,
            position,
            f"two consecutive {clean[position - 1].role} turns",
            seed,
        )

    if kind is CorruptionType.EMPTY_TURN:
        position = rng.randrange(len(clean))
        corrupted[position] = replace(clean[position], text="")
        return _result(clean, corrupted, kind, position, "empty turn", seed)

    role = (
        PATIENT_ROLE
        if kind
        in {
            CorruptionType.PATIENT_DIAGNOSIS_LEAKAGE,
            CorruptionType.PATIENT_TREATMENT_LEAKAGE,
            CorruptionType.FABRICATED_PATIENT_SYMPTOM,
        }
        else DOCTOR_ROLE
    )
    positions = [position for position, turn in enumerate(clean) if turn.role == role]
    if not positions:
        raise InjectionError(f"{kind.value} requires a {role} turn")
    position = rng.choice(positions)

    field_path: str | None = None
    if kind is CorruptionType.PATIENT_DIAGNOSIS_LEAKAGE:
        label = _diagnosis(reference, rng)
        sentence = f"I already know that my diagnosis is {label}."
        field_path = "core.diagnoses"
    elif kind is CorruptionType.PATIENT_TREATMENT_LEAKAGE:
        label = _treatment(reference, rng)
        sentence = f"I already know the prescribed treatment is {label}."
        field_path = "core.treatments"
    elif kind is CorruptionType.FABRICATED_PATIENT_SYMPTOM:
        label = _absent_phrase(
            reference,
            clean,
            (
                "flashing purple spots in both eyes",
                "a metallic taste whenever I breathe",
                "numbness limited to my left little finger",
            ),
        )
        sentence = f"I also have {label}."
    elif kind is CorruptionType.DOCTOR_HIDDEN_FACT_LEAKAGE:
        label, field_path = _hidden_fact(reference, rng)
        sentence = f"Your hidden record states {label}."
    else:
        label = _absent_phrase(
            reference,
            clean,
            (
                "a potassium concentration of 9.9 mmol/L",
                "a positive test for lunar fever",
                "a blood pressure of 310 over 190 recorded today",
            ),
        )
        sentence = f"The record confirms {label}."

    original = clean[position]
    separator = " " if original.text and not original.text.endswith((" ", "\n")) else ""
    corrupted[position] = replace(original, text=f"{original.text}{separator}{sentence}")
    return _result(
        clean, corrupted, kind, position, label, seed, field_path=field_path
    )


def inject_suite(
    turns: tuple[Turn, ...] | list[Turn],
    reference: StructuredClinicalReference,
    *,
    seed: int,
) -> tuple[InjectionResult, ...]:
    """Generate all seven independently corrupted copies with derived seeds."""

    return tuple(
        inject_fault(turns, reference, kind, seed=seed + offset)
        for offset, kind in enumerate(CorruptionType)
    )


def recover_injected_error(result: InjectionResult) -> bool:
    """Property-test oracle: every label can be recovered from its corruption."""

    positions = [
        position
        for position, turn in enumerate(result.corrupted)
        if turn.index == result.error.turn_index
    ]
    if len(positions) != 1:
        return False
    position = positions[0]
    turn = result.corrupted[position]
    if result.error.corruption_type is CorruptionType.EMPTY_TURN:
        return not turn.text.strip()
    if result.error.corruption_type is CorruptionType.ROLE_ORDER_VIOLATION:
        return position > 0 and turn.role == result.corrupted[position - 1].role
    return result.error.label in turn.text


def _result(
    clean: tuple[Turn, ...],
    corrupted: list[Turn],
    kind: CorruptionType,
    position: int,
    label: str,
    seed: int,
    *,
    field_path: str | None = None,
) -> InjectionResult:
    return InjectionResult(
        clean=clean,
        corrupted=tuple(corrupted),
        error=InjectedError(
            corruption_type=kind,
            turn_index=corrupted[position].index,
            label=label,
            expected_detector=EXPECTED_DETECTOR[kind],
            field_path=field_path,
        ),
        seed=seed,
    )


def _diagnosis(reference: StructuredClinicalReference, rng: random.Random) -> str:
    values = [item.primary for item in reference.core.diagnoses if item.primary.strip()]
    if not values:
        raise InjectionError("diagnosis leakage requires a reference diagnosis")
    return rng.choice(values)


def _treatment(reference: StructuredClinicalReference, rng: random.Random) -> str:
    values = []
    for item in reference.core.treatments:
        values.extend(
            value
            for value in (item.procedure, item.treatment)
            if value.strip() and value.lower() != "not provided"
        )
        values.extend(medication.name for medication in item.medications if medication.name.strip())
    if not values:
        raise InjectionError("treatment leakage requires a reference treatment")
    return rng.choice(values)


def _hidden_fact(
    reference: StructuredClinicalReference, rng: random.Random
) -> tuple[str, str]:
    candidates = []
    history = reference.context.medical_history.past_medical_history
    if history.strip() and history.lower() != "not provided":
        candidates.append((history, "context.medical_history.past_medical_history"))
    candidates.extend(
        (diagnosis.primary, "core.diagnoses")
        for diagnosis in reference.core.diagnoses
        if diagnosis.primary.strip()
    )
    candidates.extend(
        (medication.name, "context.current_medications")
        for medication in reference.context.current_medications
        if medication.name.strip()
    )
    if not candidates:
        raise InjectionError("doctor hidden-fact leakage requires a hidden reference fact")
    return rng.choice(candidates)


def _absent_phrase(
    reference: StructuredClinicalReference,
    turns: tuple[Turn, ...],
    candidates: tuple[str, ...],
) -> str:
    haystack = (
        reference.model_dump_json() + " " + " ".join(turn.text for turn in turns)
    ).lower()
    for candidate in candidates:
        if candidate.lower() not in haystack:
            return candidate
    raise InjectionError("no synthetic phrase is absent from the fixture")


__all__ = [
    "EXPECTED_DETECTOR",
    "CorruptionType",
    "InjectedError",
    "InjectionError",
    "InjectionResult",
    "inject_fault",
    "inject_suite",
    "recover_injected_error",
]
