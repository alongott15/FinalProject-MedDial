"""Prospective sample-size derivation from paired pilot effects (STAT-5)."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist, stdev
from typing import Any


class PowerError(ValueError):
    """Pilot inputs or the planned confirmatory size are invalid."""


@dataclass(frozen=True)
class PowerDerivation:
    pilot_n: int
    pilot_hash: str
    paired_sd: float
    smallest_difference: float
    alpha: float
    incomplete_rate: float
    required_complete_80: int
    required_total_80: int
    required_complete_90: int
    required_total_90: int
    method: str = "normal approximation for a two-sided paired mean test"

    def achieved_power(self, planned_total: int) -> float:
        if planned_total < 2:
            raise PowerError("planned_total must be at least two")
        complete = max(2, math.floor(planned_total * (1.0 - self.incomplete_rate)))
        if self.paired_sd == 0.0:
            return 1.0
        noncentrality = self.smallest_difference * math.sqrt(complete) / self.paired_sd
        critical = NormalDist().inv_cdf(1.0 - self.alpha / 2.0)
        # Power under a positive alternative for a two-sided normal test.
        return 1.0 - NormalDist().cdf(critical - noncentrality) + NormalDist().cdf(
            -critical - noncentrality
        )

    def assert_powered(self, planned_total: int, *, target: float = 0.8) -> None:
        achieved = self.achieved_power(planned_total)
        if achieved < target:
            raise PowerError(
                f"planned cohort {planned_total} has estimated power {achieved:.3f}, "
                f"below target {target:.3f}"
            )

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


def calculate_paired_power(
    pilot_differences: Sequence[float],
    *,
    smallest_difference: float,
    alpha: float = 0.05,
    incomplete_rate: float = 0.0,
) -> PowerDerivation:
    """Derive 80% and 90% powered sample sizes from a development pilot."""

    values = tuple(float(value) for value in pilot_differences)
    if len(values) < 2:
        raise PowerError("at least two paired pilot differences are required")
    if not math.isfinite(smallest_difference) or smallest_difference <= 0.0:
        raise PowerError("smallest_difference must be positive and finite")
    if not 0.0 < alpha < 1.0:
        raise PowerError("alpha must be between zero and one")
    if not 0.0 <= incomplete_rate < 1.0:
        raise PowerError("incomplete_rate must be in [0, 1)")
    if not all(math.isfinite(value) for value in values):
        raise PowerError("pilot differences must all be finite")

    paired_sd = stdev(values)
    payload = json.dumps(values, separators=(",", ":"))
    pilot_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def required(power: float) -> tuple[int, int]:
        if paired_sd == 0.0:
            complete = 2
        else:
            z_alpha = NormalDist().inv_cdf(1.0 - alpha / 2.0)
            z_power = NormalDist().inv_cdf(power)
            complete = max(
                2,
                math.ceil(((z_alpha + z_power) * paired_sd / smallest_difference) ** 2),
            )
        total = math.ceil(complete / (1.0 - incomplete_rate))
        return complete, total

    complete_80, total_80 = required(0.8)
    complete_90, total_90 = required(0.9)
    return PowerDerivation(
        pilot_n=len(values),
        pilot_hash=pilot_hash,
        paired_sd=paired_sd,
        smallest_difference=smallest_difference,
        alpha=alpha,
        incomplete_rate=incomplete_rate,
        required_complete_80=complete_80,
        required_total_80=total_80,
        required_complete_90=complete_90,
        required_total_90=total_90,
    )


def write_power_record(
    path: Path | str,
    derivation: PowerDerivation,
    *,
    planned_total: int,
    frozen_at: datetime,
) -> Path:
    """Write the prospective derivation before a confirmatory run starts."""

    if frozen_at.tzinfo is None:
        raise PowerError("frozen_at must include a timezone")
    frozen = frozen_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    achieved = derivation.achieved_power(planned_total)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Confirmatory cohort power derivation",
        "",
        f"- Frozen before the run: `{frozen}`",
        f"- Pilot pairs: {derivation.pilot_n}",
        f"- Pilot input SHA-256: `{derivation.pilot_hash}`",
        f"- Within-case paired SD: {derivation.paired_sd:.8g}",
        f"- Smallest difference worth detecting: {derivation.smallest_difference:.8g}",
        f"- Two-sided alpha: {derivation.alpha:.6g}",
        f"- Pilot `INCOMPLETE` allowance: {derivation.incomplete_rate:.3%}",
        f"- Required total for 80% power: {derivation.required_total_80}",
        f"- Required total for 90% power: {derivation.required_total_90}",
        f"- Planned total: {planned_total}",
        f"- Estimated power at planned total: {achieved:.3%}",
        f"- Method: {derivation.method}",
        "",
        (
            "This record is prospective. Changing the pilot, target effect, alpha, "
            "incomplete allowance, or planned cohort requires a new run configuration."
        ),
        "",
    ]
    destination.write_text("\n".join(lines))
    return destination


__all__ = [
    "PowerDerivation",
    "PowerError",
    "calculate_paired_power",
    "write_power_record",
]
