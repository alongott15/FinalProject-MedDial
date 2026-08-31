"""Statistics for case-clustered designs.

What E0 needs in order to report anything at all: intervals that respect the
fact that the three policy arms of a case are the same source case seen three
times. Appendix E.1-E.3 of the implementation plan. W8 extends this with
multiplicity control and the power derivation.
"""

from .paired import (
    Interval,
    PairedResult,
    StatsError,
    case_clustered_bootstrap,
    mean,
    paired_difference,
    wilson_interval,
)

__all__ = [
    "Interval",
    "PairedResult",
    "StatsError",
    "case_clustered_bootstrap",
    "mean",
    "paired_difference",
    "wilson_interval",
]
