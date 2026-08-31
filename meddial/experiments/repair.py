"""Deterministic, dimension-keyed repair instructions (EXP-3).

The previous PromptImprovementAgent rewrote patient, doctor, and general
prompts together after any low composite score.  That changes several
experimental factors at once.  W7 repair is deliberately narrower: each
failed dimension maps to an audited set of targets and a fixed directive, and
the exact actions applied to an attempt are stored in its record.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from meddial.evaluation import (
    DOCTOR_FACTUALITY,
    KNOWLEDGE_BOUNDARY,
    NATURALNESS,
    PATIENT_FACTUALITY,
    STRUCTURAL_VALIDITY,
)


@dataclass(frozen=True)
class _RepairRule:
    targets: tuple[str, ...]
    directive: str


_RULES: Mapping[str, _RepairRule] = MappingProxyType(
    {
        PATIENT_FACTUALITY: _RepairRule(
            targets=("patient_prompt",),
            directive=(
                "Remove or explicitly qualify only the unsupported patient statements identified "
                "by the factuality evaluator; do not change doctor behaviour or disclosure policy."
            ),
        ),
        DOCTOR_FACTUALITY: _RepairRule(
            targets=("doctor_prompt",),
            directive=(
                "Remove or qualify only unsupported doctor assertions; retain questions, "
                "diagnostic hypotheses, and the configured patient knowledge boundary."
            ),
        ),
        KNOWLEDGE_BOUNDARY: _RepairRule(
            targets=("patient_prompt", "patient_context_guard"),
            directive=(
                "Prevent only the located prohibited disclosures and reinforce the configured "
                "policy boundary without adding facts from masked reference fields."
            ),
        ),
        NATURALNESS: _RepairRule(
            targets=("patient_style", "doctor_style"),
            directive=(
                "Revise only the evaluator-located conversational phrasing or flow; preserve all "
                "clinical content, policy masking, factual claims, and turn budget."
            ),
        ),
        STRUCTURAL_VALIDITY: _RepairRule(
            targets=("orchestration",),
            directive=(
                "Correct only the reported deterministic structure violations: role order, empty "
                "turns, repetition, provider sentinels, or turn bounds."
            ),
        ),
    }
)


@dataclass(frozen=True)
class RepairAction:
    dimension: str
    targets: tuple[str, ...]
    directive: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.dimension not in _RULES:
            raise ValueError(f"unknown failed dimension {self.dimension!r}")
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))

    def as_record(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "targets": list(self.targets),
            "directive": self.directive,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class RepairPlan:
    actions: tuple[RepairAction, ...]
    repair_version: str = "targeted-repair.v1"

    @property
    def failed_dimensions(self) -> tuple[str, ...]:
        return tuple(action.dimension for action in self.actions)

    def as_record(self) -> dict[str, Any]:
        return {
            "repair_version": self.repair_version,
            "failed_dimensions": list(self.failed_dimensions),
            "actions": [action.as_record() for action in self.actions],
        }


def build_repair_plan(
    failed_dimensions: Sequence[str],
    *,
    details: Mapping[str, Mapping[str, Any]] | None = None,
) -> RepairPlan:
    """Build one independent repair action per failed dimension.

    Unknown dimensions fail closed.  In particular, ``composite`` is not a
    repair target because it is reporting-only and cannot identify what needs
    to change.
    """
    seen: set[str] = set()
    actions: list[RepairAction] = []
    evidence = details or {}
    for raw_dimension in failed_dimensions:
        dimension = str(raw_dimension)
        if dimension not in _RULES:
            raise ValueError(f"unknown failed dimension {dimension!r}")
        if dimension in seen:
            continue
        seen.add(dimension)
        rule = _RULES[dimension]
        actions.append(
            RepairAction(
                dimension=dimension,
                targets=rule.targets,
                directive=rule.directive,
                evidence=evidence.get(dimension, {}),
            )
        )
    return RepairPlan(tuple(actions))


def failed_dimensions_from_evaluation(evaluation: Mapping[str, Any]) -> tuple[str, ...]:
    """Read failed dimensions from a serialized EvaluationResult-like mapping."""
    acceptance = evaluation.get("acceptance", {})
    per_dimension = acceptance.get("per_dimension", {}) if isinstance(acceptance, Mapping) else {}
    failed = [
        str(dimension)
        for dimension, status in per_dimension.items()
        if str(getattr(status, "value", status)).lower() == "fail"
    ]
    if failed:
        return tuple(failed)

    scores = evaluation.get("scores", {})
    if not isinstance(scores, Mapping):
        return ()
    return tuple(
        str(dimension)
        for dimension, score in scores.items()
        if isinstance(score, Mapping)
        and str(getattr(score.get("status"), "value", score.get("status"))).lower() == "fail"
    )


def repair_from_evaluation(evaluation: Mapping[str, Any]) -> RepairPlan:
    failed = failed_dimensions_from_evaluation(evaluation)
    scores = evaluation.get("scores", {})
    details = {
        dimension: dict(score.get("detail", {}))
        for dimension, score in scores.items()
        if isinstance(score, Mapping) and isinstance(score.get("detail", {}), Mapping)
    } if isinstance(scores, Mapping) else {}
    return build_repair_plan(failed, details=details)


__all__ = [
    "RepairAction",
    "RepairPlan",
    "build_repair_plan",
    "failed_dimensions_from_evaluation",
    "repair_from_evaluation",
]
