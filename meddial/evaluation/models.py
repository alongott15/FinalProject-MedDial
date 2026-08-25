from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EvaluationStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    UNSCORABLE = "UNSCORABLE"


@dataclass(frozen=True)
class MetricResult:
    name: str
    status: EvaluationStatus
    score: float | None
    reason: str
    details: Mapping[str, Any] = field(default_factory=dict)
    evaluator: str | None = None

    def __post_init__(self) -> None:
        if self.score is not None and not 0.0 <= self.score <= 1.0:
            raise ValueError(f"Metric score must be in [0, 1], got {self.score}")
        if self.status in {EvaluationStatus.ERROR, EvaluationStatus.UNSCORABLE} and self.score is not None:
            raise ValueError(f"{self.status.value} metric results must not have a score")

    @property
    def complete(self) -> bool:
        return self.status in {EvaluationStatus.PASS, EvaluationStatus.FAIL}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data
