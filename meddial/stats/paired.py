"""Case-clustered bootstrap and paired policy comparison (Appendix E.1-E.3).

The unit of resampling is the **case**, never the dialogue. Each case
contributes one dialogue per policy arm, and those are not independent
observations — they share a source record, a patient profile and a seed.
Resampling dialogues would treat three correlated observations as three
independent ones and understate every interval, which is defect D-11.

Two consequences run through this module:

* :func:`case_clustered_bootstrap` samples case identifiers with replacement
  and takes *all* of a sampled case's observations, so a case drawn twice
  contributes its whole cluster twice.
* :func:`paired_difference` forms the within-case difference first and
  bootstraps over those, so a comparison never crosses cases. Unpaired group
  means across policies are not offered here, because under this design they
  are the wrong statistic.

``INCOMPLETE`` scores arrive as ``None`` and are dropped, and the number
dropped is returned rather than absorbed — E.6 requires the exclusion rate be
reported, since a dialogue that fails to score may differ systematically from
one that does.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import sqrt
from statistics import fmean
from typing import Any

DEFAULT_RESAMPLES = 2000
"""Appendix E.1 sets B >= 2000."""

DEFAULT_CONFIDENCE = 0.95


class StatsError(Exception):
    """The sample cannot support the statistic asked of it."""


@dataclass(frozen=True)
class Interval:
    """A point estimate and the interval around it. No bare means (E.6)."""

    estimate: float
    low: float
    high: float
    method: str
    confidence: float = DEFAULT_CONFIDENCE

    def as_record(self) -> dict[str, Any]:
        return {
            "estimate": self.estimate,
            "low": self.low,
            "high": self.high,
            "method": self.method,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class PairedResult:
    """A within-case comparison of two arms."""

    difference: Interval
    n_cases: int
    n_dropped: int
    arm_a: str
    arm_b: str

    @property
    def excludes_zero(self) -> bool:
        """True when the interval lies wholly on one side of zero."""
        return self.difference.low > 0.0 or self.difference.high < 0.0

    def as_record(self) -> dict[str, Any]:
        return {
            "arm_a": self.arm_a,
            "arm_b": self.arm_b,
            "difference": self.difference.as_record(),
            "n_cases": self.n_cases,
            "n_dropped": self.n_dropped,
            "excludes_zero": self.excludes_zero,
        }


def mean(values: Sequence[float]) -> float:
    """The arithmetic mean, refusing an empty sample rather than returning 0."""
    if not values:
        raise StatsError("cannot take the mean of an empty sample")
    return fmean(values)


def case_clustered_bootstrap(
    by_case: Mapping[str, Sequence[float | None]],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = 0,
) -> Interval:
    """Percentile interval for the grand mean, resampling cases (E.1).

    ``by_case`` maps a case identifier to that case's observations. ``None``
    entries are ``INCOMPLETE`` scores and are dropped; a case left with no
    observation drops out entirely.
    """
    clusters = _clean_clusters(by_case)
    if not clusters:
        raise StatsError("no case has a measured observation")

    pooled = [value for cluster in clusters.values() for value in cluster]
    estimate = mean(pooled)

    rng = random.Random(seed)
    keys = list(clusters)
    replicates: list[float] = []
    for _ in range(resamples):
        drawn: list[float] = []
        for _ in keys:
            # Take the whole cluster: a case drawn twice counts twice, which
            # is what keeps the interval honest about the correlation.
            drawn.extend(clusters[rng.choice(keys)])
        replicates.append(fmean(drawn))

    low, high = _percentiles(replicates, confidence)
    return Interval(
        estimate=estimate,
        low=low,
        high=high,
        method=f"case-clustered percentile bootstrap (B={resamples})",
        confidence=confidence,
    )


def paired_difference(
    arm_a: Mapping[str, float | None],
    arm_b: Mapping[str, float | None],
    *,
    label_a: str = "A",
    label_b: str = "B",
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = 0,
) -> PairedResult:
    """Mean within-case difference ``metric(A) - metric(B)``, with an interval (E.2).

    Only cases measured in both arms contribute. Cases dropped because either
    arm is ``INCOMPLETE`` are counted and returned, never absorbed.
    """
    shared = sorted(set(arm_a) & set(arm_b))
    differences: dict[str, float] = {}
    for case in shared:
        value_a, value_b = arm_a[case], arm_b[case]
        if value_a is not None and value_b is not None:
            differences[case] = value_a - value_b

    dropped = len(set(arm_a) | set(arm_b)) - len(differences)
    if not differences:
        raise StatsError("no case is measured in both arms")

    interval = case_clustered_bootstrap(
        {case: [value] for case, value in differences.items()},
        resamples=resamples,
        confidence=confidence,
        seed=seed,
    )
    return PairedResult(
        difference=Interval(
            estimate=interval.estimate,
            low=interval.low,
            high=interval.high,
            method=f"paired within case, {interval.method}",
            confidence=confidence,
        ),
        n_cases=len(differences),
        n_dropped=dropped,
        arm_a=label_a,
        arm_b=label_b,
    )


def wilson_interval(
    successes: int, trials: int, *, confidence: float = DEFAULT_CONFIDENCE
) -> Interval:
    """Wilson score interval for a proportion (E.3).

    The default for rates near the ceiling — a 98% pass rate breaks the normal
    approximation, which can put the upper bound above 1.
    """
    if trials <= 0:
        raise StatsError("cannot form a proportion over zero trials")
    if not 0 <= successes <= trials:
        raise StatsError(f"{successes} successes in {trials} trials is not a proportion")

    z = _z_for(confidence)
    proportion = successes / trials
    denominator = 1 + z**2 / trials
    centre = (proportion + z**2 / (2 * trials)) / denominator
    spread = (
        z * sqrt(proportion * (1 - proportion) / trials + z**2 / (4 * trials**2)) / denominator
    )
    return Interval(
        estimate=proportion,
        low=max(0.0, centre - spread),
        high=min(1.0, centre + spread),
        method="Wilson score",
        confidence=confidence,
    )


def _clean_clusters(by_case: Mapping[str, Sequence[float | None]]) -> dict[str, list[float]]:
    clusters: dict[str, list[float]] = {}
    for case, values in by_case.items():
        measured = [float(value) for value in values if value is not None]
        if measured:
            clusters[case] = measured
    return clusters


def _percentiles(values: Sequence[float], confidence: float) -> tuple[float, float]:
    ordered = sorted(values)
    tail = (1.0 - confidence) / 2.0
    return _quantile(ordered, tail), _quantile(ordered, 1.0 - tail)


def _quantile(ordered: Sequence[float], q: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


# Normal quantiles for the confidences actually reported. Spelling them out
# avoids depending on scipy for three numbers.
_Z_SCORES = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}


def _z_for(confidence: float) -> float:
    try:
        return _Z_SCORES[round(confidence, 2)]
    except KeyError:
        raise StatsError(
            f"no z-score tabulated for confidence {confidence}; use one of {sorted(_Z_SCORES)}"
        ) from None
