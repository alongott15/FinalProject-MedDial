"""Confirmatory statistical procedures beyond the E0 bootstrap primitives."""

from __future__ import annotations

import itertools
import random
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from statistics import fmean
from typing import Any

from meddial.stats import (
    Interval,
    PairedResult,
    StatsError,
    case_clustered_bootstrap,
    paired_difference,
    wilson_interval,
)


class AdjustmentMethod(str, Enum):
    HOLM = "holm-bonferroni"
    BENJAMINI_HOCHBERG = "benjamini-hochberg"


@dataclass(frozen=True)
class ComparisonFamily:
    """A pre-declared family whose p-values are adjusted together (STAT-3)."""

    family_id: str
    description: str
    method: AdjustmentMethod


DECLARED_FAMILIES: tuple[ComparisonFamily, ...] = (
    ComparisonFamily(
        "F1",
        "architecture: five variants pairwise on the primary outcome",
        AdjustmentMethod.HOLM,
    ),
    ComparisonFamily(
        "F2",
        "policy sensitivity: three policies pairwise per metric",
        AdjustmentMethod.HOLM,
    ),
    ComparisonFamily(
        "F3",
        "judge-family agreement across independent evaluators",
        AdjustmentMethod.HOLM,
    ),
    ComparisonFamily(
        "F4",
        "detector performance across seven injected fault classes",
        AdjustmentMethod.BENJAMINI_HOCHBERG,
    ),
)


@dataclass(frozen=True)
class PairedTestResult:
    arm_a: str
    arm_b: str
    mean_difference: float
    p_value: float
    n_cases: int
    n_dropped: int
    method: str

    def as_record(self) -> dict[str, Any]:
        return {
            "arm_a": self.arm_a,
            "arm_b": self.arm_b,
            "mean_difference": self.mean_difference,
            "p_value": self.p_value,
            "n_cases": self.n_cases,
            "n_dropped": self.n_dropped,
            "method": self.method,
        }


def paired_randomisation_test(
    arm_a: Mapping[str, float | None],
    arm_b: Mapping[str, float | None],
    *,
    label_a: str = "A",
    label_b: str = "B",
    monte_carlo_samples: int = 20_000,
    seed: int = 0,
) -> PairedTestResult:
    """Two-sided paired sign-flip test over cases.

    For at most twenty measured pairs every sign assignment is enumerated.
    Larger samples use a deterministic Monte Carlo approximation.  Forming
    differences before testing prevents policy arms from different cases being
    compared as though independent.
    """

    shared = sorted(set(arm_a) & set(arm_b))
    differences = [
        float(arm_a[case]) - float(arm_b[case])
        for case in shared
        if arm_a[case] is not None and arm_b[case] is not None
    ]
    dropped = len(set(arm_a) | set(arm_b)) - len(differences)
    if not differences:
        raise StatsError("no case is measured in both arms")

    observed = abs(fmean(differences))
    tolerance = 1e-15
    if len(differences) <= 20:
        total = 2 ** len(differences)
        extreme = 0
        for signs in itertools.product((-1.0, 1.0), repeat=len(differences)):
            statistic = abs(
                fmean(value * sign for value, sign in zip(differences, signs, strict=True))
            )
            if statistic + tolerance >= observed:
                extreme += 1
        p_value = extreme / total
        method = f"exact paired sign-flip ({total} assignments)"
    else:
        if monte_carlo_samples < 1:
            raise StatsError("monte_carlo_samples must be positive")
        rng = random.Random(seed)
        extreme = 0
        for _ in range(monte_carlo_samples):
            statistic = abs(
                fmean(value if rng.getrandbits(1) else -value for value in differences)
            )
            if statistic + tolerance >= observed:
                extreme += 1
        # Phipson-Smyth correction: a sampled randomisation p-value is never 0.
        p_value = (extreme + 1) / (monte_carlo_samples + 1)
        method = f"Monte Carlo paired sign-flip (B={monte_carlo_samples})"

    return PairedTestResult(
        arm_a=label_a,
        arm_b=label_b,
        mean_difference=fmean(differences),
        p_value=p_value,
        n_cases=len(differences),
        n_dropped=dropped,
        method=method,
    )


def adjust_pvalues(
    p_values: Mapping[str, float], method: AdjustmentMethod | str
) -> dict[str, float]:
    """Adjust one declared comparison family while preserving its labels."""

    try:
        selected = AdjustmentMethod(method)
    except ValueError as exc:
        raise StatsError(f"unknown multiplicity method {method!r}") from exc
    for label, value in p_values.items():
        if not 0.0 <= value <= 1.0:
            raise StatsError(f"p-value {label}={value} is outside [0, 1]")
    if not p_values:
        return {}

    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    adjusted: dict[str, float] = {}
    if selected is AdjustmentMethod.HOLM:
        running = 0.0
        for rank, (label, value) in enumerate(ordered):
            running = max(running, (count - rank) * value)
            adjusted[label] = min(1.0, running)
    else:
        # Work backwards so adjusted BH values are monotone in raw p.
        running = 1.0
        for reverse_rank in range(count - 1, -1, -1):
            label, value = ordered[reverse_rank]
            rank = reverse_rank + 1
            running = min(running, value * count / rank)
            adjusted[label] = min(1.0, running)
    return {label: adjusted[label] for label in p_values}


__all__ = [
    "DECLARED_FAMILIES",
    "AdjustmentMethod",
    "ComparisonFamily",
    "Interval",
    "PairedResult",
    "PairedTestResult",
    "StatsError",
    "adjust_pvalues",
    "case_clustered_bootstrap",
    "paired_difference",
    "paired_randomisation_test",
    "wilson_interval",
]
