"""Deterministic validation that does not require a model call."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from meddial.evaluation.models import EvaluationStatus, MetricResult


class DeterministicStructuralValidator:
    def __init__(self, min_turns: int = 4, max_turns: int = 30) -> None:
        self.min_turns = min_turns
        self.max_turns = max_turns

    def validate(self, dialogue: Sequence[Mapping[str, str]]) -> MetricResult:
        violations: list[str] = []
        if len(dialogue) < self.min_turns:
            violations.append(f"fewer than {self.min_turns} turns")
        if len(dialogue) > self.max_turns:
            violations.append(f"more than {self.max_turns} turns")
        expected_roles = {"doctor", "patient"}
        previous_role: str | None = None
        role_counts = {"doctor": 0, "patient": 0}
        for index, turn in enumerate(dialogue):
            role = str(turn.get("role", "")).strip().lower()
            content = str(turn.get("content", "")).strip()
            if role not in expected_roles:
                violations.append(f"turn {index} has invalid role {role!r}")
            else:
                role_counts[role] += 1
            if not content:
                violations.append(f"turn {index} has empty content")
            if "[error:" in content.lower():
                violations.append(f"turn {index} contains an error sentinel")
            if previous_role == role and role in expected_roles:
                violations.append(f"turn {index} repeats role {role}")
            previous_role = role
        if dialogue and str(dialogue[0].get("role", "")).lower() != "doctor":
            violations.append("dialogue does not start with the doctor")
        if not all(role_counts.values()):
            violations.append("both doctor and patient roles are required")
        score = max(0.0, 1.0 - (len(violations) / max(1, len(dialogue))))
        return MetricResult(
            name="structural_validity",
            status=EvaluationStatus.FAIL if violations else EvaluationStatus.PASS,
            score=score,
            reason="; ".join(violations) if violations else "All structural checks passed",
            details={"violations": violations, "role_counts": role_counts},
        )
