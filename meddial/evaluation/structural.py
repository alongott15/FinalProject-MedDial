"""Deterministic structural validation. No model call, ever.

Implements W3 item 6 (EVAL-9, PRD §9.4). The conjunction is fixed: role
alternation valid, no empty or whitespace-only turns, turn count within
bounds, no exact-duplicate consecutive turns, repetition below threshold, and
no provider-error sentinel in any turn.

The sentinel check is the one that matters most for the paper. Before W1 a
provider failure returned a string like ``"[ERROR: connection refused]"``
which entered the transcript and was scored as content (D-08). W1 stopped
providers producing those strings; this check is the standing guard that a
dialogue carrying one can never be accepted, including dialogues generated
before the fix and replayed later.

Being deterministic is a requirement, not an optimisation: identical input
must yield identical output with no model call, so structural validity can
never be the reason two runs of the same cohort disagree.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from Utils.repetition_filter import detect_symptom_repetition

from .claims import DOCTOR_ROLE, PATIENT_ROLE, Turn
from .provenance import Score, ScoreProvenance, TurnScope

SCORER_ID = "meddial.evaluation.structural"

ERROR_SENTINEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[\s*ERROR\b", re.IGNORECASE),
    re.compile(r"\berror generating\b", re.IGNORECASE),
    re.compile(r"\bunable to generate\b", re.IGNORECASE),
    re.compile(r"\bfailed to generate\b", re.IGNORECASE),
    re.compile(r"\bno response (?:was )?(?:generated|received)\b", re.IGNORECASE),
    re.compile(r"\b(?:api|provider|model) (?:error|failure|unavailable)\b", re.IGNORECASE),
)
"""Text that is a failure report rather than dialogue. Any match fails the dialogue."""

_VALID_ROLES = frozenset({PATIENT_ROLE, DOCTOR_ROLE})


@dataclass(frozen=True)
class StructuralConfig:
    """Bounds for the deterministic checks. Recorded in the score's detail."""

    min_turns: int = 4
    max_turns: int = 40
    max_repeated_symptoms: int = 0

    def as_record(self) -> dict[str, Any]:
        return {
            "min_turns": self.min_turns,
            "max_turns": self.max_turns,
            "max_repeated_symptoms": self.max_repeated_symptoms,
        }


@dataclass(frozen=True)
class StructuralViolation:
    """One failed check, located where possible."""

    check: str
    detail: str
    turn_index: int | None = None

    def as_record(self) -> dict[str, Any]:
        return {"check": self.check, "detail": self.detail, "turn_index": self.turn_index}


@dataclass(frozen=True)
class StructuralReport:
    """The full outcome of the deterministic pass."""

    violations: tuple[StructuralViolation, ...] = field(default_factory=tuple)

    @property
    def is_valid(self) -> bool:
        return not self.violations

    @property
    def failed_checks(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for violation in self.violations:
            seen.setdefault(violation.check, None)
        return tuple(seen)


def check_structure(
    turns: Sequence[Turn], *, config: StructuralConfig | None = None
) -> StructuralReport:
    """Run every deterministic check and collect the violations."""
    settings = config or StructuralConfig()
    violations: list[StructuralViolation] = []

    violations.extend(_check_turn_count(turns, settings))
    violations.extend(_check_roles_and_alternation(turns))
    violations.extend(_check_non_empty(turns))
    violations.extend(_check_consecutive_duplicates(turns))
    violations.extend(_check_error_sentinels(turns))
    violations.extend(_check_repetition(turns, settings))

    return StructuralReport(violations=tuple(violations))


def score_structural_validity(
    turns: Sequence[Turn], *, config: StructuralConfig | None = None
) -> tuple[Score, StructuralReport]:
    """Score structural validity without touching a model (EVAL-9)."""
    settings = config or StructuralConfig()
    report = check_structure(turns, config=settings)

    score = Score.measured(
        1.0 if report.is_valid else 0.0,
        ScoreProvenance.deterministic(scorer_id=SCORER_ID, turn_scope=TurnScope.BOTH),
        threshold=1.0,
        detail={
            "turns": len(turns),
            "config": settings.as_record(),
            "failed_checks": list(report.failed_checks),
            "violations": [violation.as_record() for violation in report.violations],
        },
    )
    return score, report


def _check_turn_count(
    turns: Sequence[Turn], config: StructuralConfig
) -> list[StructuralViolation]:
    if len(turns) < config.min_turns:
        return [
            StructuralViolation(
                "turn_bounds", f"{len(turns)} turns, minimum is {config.min_turns}"
            )
        ]
    if len(turns) > config.max_turns:
        return [
            StructuralViolation(
                "turn_bounds", f"{len(turns)} turns, maximum is {config.max_turns}"
            )
        ]
    return []


def _check_roles_and_alternation(turns: Sequence[Turn]) -> list[StructuralViolation]:
    violations: list[StructuralViolation] = []
    for position, turn in enumerate(turns):
        if turn.role not in _VALID_ROLES:
            violations.append(
                StructuralViolation("role_validity", f"unknown role {turn.role!r}", turn.index)
            )
            continue
        if position and turn.role == turns[position - 1].role:
            violations.append(
                StructuralViolation(
                    "alternation", f"{turn.role} speaks twice in a row", turn.index
                )
            )
    return violations


def _check_non_empty(turns: Sequence[Turn]) -> list[StructuralViolation]:
    return [
        StructuralViolation("empty_turn", "turn is empty or whitespace only", turn.index)
        for turn in turns
        if not turn.text.strip()
    ]


def _check_consecutive_duplicates(turns: Sequence[Turn]) -> list[StructuralViolation]:
    violations: list[StructuralViolation] = []
    for position in range(1, len(turns)):
        current = turns[position].text.strip()
        previous = turns[position - 1].text.strip()
        if current and current == previous:
            violations.append(
                StructuralViolation(
                    "duplicate_turn",
                    "turn is identical to the one before it",
                    turns[position].index,
                )
            )
    return violations


def _check_error_sentinels(turns: Sequence[Turn]) -> list[StructuralViolation]:
    violations: list[StructuralViolation] = []
    for turn in turns:
        for pattern in ERROR_SENTINEL_PATTERNS:
            match = pattern.search(turn.text)
            if match:
                violations.append(
                    StructuralViolation(
                        "error_sentinel",
                        f"turn contains a provider-error sentinel: {match.group(0)!r}",
                        turn.index,
                    )
                )
                break
    return violations


def _check_repetition(
    turns: Sequence[Turn], config: StructuralConfig
) -> list[StructuralViolation]:
    history = [{"role": turn.role, "content": turn.text} for turn in turns]
    overmentioned = detect_symptom_repetition(history)
    if len(overmentioned) > config.max_repeated_symptoms:
        return [
            StructuralViolation(
                "repetition", f"symptoms repeated excessively: {sorted(overmentioned)}"
            )
        ]
    return []
