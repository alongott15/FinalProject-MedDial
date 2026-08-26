"""Controlled, labelled error-injection benchmark for evaluator validation."""

from __future__ import annotations

import copy
import random
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class CorruptionType(str, Enum):
    PATIENT_DIAGNOSIS_LEAKAGE = "patient_diagnosis_leakage"
    PATIENT_TREATMENT_LEAKAGE = "patient_treatment_leakage"
    FABRICATED_PATIENT_SYMPTOM = "fabricated_patient_symptom"
    DOCTOR_HIDDEN_FACT_LEAKAGE = "doctor_hidden_fact_leakage"
    UNSUPPORTED_DOCTOR_FACT = "unsupported_doctor_fact"
    ROLE_ORDER_VIOLATION = "role_order_violation"
    EMPTY_TURN = "empty_turn"


@dataclass(frozen=True)
class InjectedError:
    corruption_type: CorruptionType
    turn_index: int
    label: str
    expected_detector: str


@dataclass(frozen=True)
class CorruptedDialogue:
    dialogue: tuple[Mapping[str, str], ...]
    errors: tuple[InjectedError, ...]
    seed: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dialogue": list(self.dialogue),
            "errors": [
                {**asdict(error), "corruption_type": error.corruption_type.value}
                for error in self.errors
            ],
            "seed": self.seed,
        }


class InjectedErrorBenchmark:
    def __init__(self, seed: int = 42) -> None:
        self.seed = seed

    def inject(
        self,
        dialogue: Sequence[Mapping[str, str]],
        corruption_type: CorruptionType,
        reference: Mapping[str, Any],
    ) -> CorruptedDialogue:
        if not dialogue:
            raise ValueError("Cannot corrupt an empty dialogue")
        rng = random.Random(self.seed)
        corrupted: list[dict[str, str]] = [copy.deepcopy(dict(turn)) for turn in dialogue]
        patient_indices = [
            index
            for index, turn in enumerate(corrupted)
            if str(turn.get("role", "")).lower() == "patient"
        ]
        doctor_indices = [
            index
            for index, turn in enumerate(corrupted)
            if str(turn.get("role", "")).lower() == "doctor"
        ]
        index = rng.choice(patient_indices or list(range(len(corrupted))))
        detector = "role_aware_clinical_faithfulness"
        label = corruption_type.value
        core = reference.get("Core_Fields", {})

        if corruption_type is CorruptionType.PATIENT_DIAGNOSIS_LEAKAGE:
            diagnoses = core.get("Diagnoses", [])
            value = (
                diagnoses[0].get("primary", "the hidden diagnosis")
                if diagnoses
                else "the hidden diagnosis"
            )
            corrupted[index]["content"] = f"I already know I have {value}."
            detector = "knowledge_boundary"
        elif corruption_type is CorruptionType.PATIENT_TREATMENT_LEAKAGE:
            treatments = core.get("Treatment_Options", [])
            value = (
                treatments[0].get("treatment", "the hidden treatment")
                if treatments
                else "the hidden treatment"
            )
            corrupted[index]["content"] = f"My established treatment is {value}."
            detector = "knowledge_boundary"
        elif corruption_type is CorruptionType.FABRICATED_PATIENT_SYMPTOM:
            corrupted[index]["content"] = "I have sudden purple vision every morning."
        elif corruption_type is CorruptionType.DOCTOR_HIDDEN_FACT_LEAKAGE:
            index = rng.choice(doctor_indices or [0])
            symptoms = core.get("Symptoms", [])
            value = (
                symptoms[0].get("description", "your hidden symptom")
                if symptoms
                else "your hidden symptom"
            )
            corrupted[index]["content"] = f"Your record tells me you have {value}."
            detector = "knowledge_boundary"
        elif corruption_type is CorruptionType.UNSUPPORTED_DOCTOR_FACT:
            index = rng.choice(doctor_indices or [0])
            corrupted[index]["content"] = "Your MRI definitively showed a brain tumour."
        elif corruption_type is CorruptionType.ROLE_ORDER_VIOLATION:
            index = min(1, len(corrupted) - 1)
            corrupted[index]["role"] = corrupted[index - 1].get("role", "Doctor")
            detector = "structural_validity"
        elif corruption_type is CorruptionType.EMPTY_TURN:
            corrupted[index]["content"] = ""
            detector = "structural_validity"
        else:  # pragma: no cover - Enum protects public calls
            raise ValueError(f"Unsupported corruption type: {corruption_type}")

        error = InjectedError(corruption_type, index, label, detector)
        return CorruptedDialogue(tuple(corrupted), (error,), self.seed)


def benchmark_metrics(
    expected_labels: Sequence[str], detected_labels: Sequence[str]
) -> dict[str, float | int]:
    expected = set(expected_labels)
    detected = set(detected_labels)
    true_positive = len(expected & detected)
    false_positive = len(detected - expected)
    false_negative = len(expected - detected)
    precision = true_positive / (true_positive + false_positive) if detected else 0.0
    recall = true_positive / (true_positive + false_negative) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }
