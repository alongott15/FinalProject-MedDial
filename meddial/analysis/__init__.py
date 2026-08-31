"""Reproducible analysis, power derivation, and manuscript outputs."""

from meddial.analysis.power import (
    PowerDerivation,
    PowerError,
    calculate_paired_power,
    write_power_record,
)
from meddial.analysis.stats import (
    AdjustmentMethod,
    ComparisonFamily,
    PairedTestResult,
    adjust_pvalues,
    paired_randomisation_test,
)
from meddial.analysis.tables import (
    AnalysisError,
    AnalysisOutputs,
    read_attempt_records,
    regenerate_tables,
)
from meddial.stats import (
    Interval,
    PairedResult,
    StatsError,
    case_clustered_bootstrap,
    paired_difference,
    wilson_interval,
)

__all__ = [
    "AdjustmentMethod",
    "AnalysisError",
    "AnalysisOutputs",
    "ComparisonFamily",
    "Interval",
    "PairedResult",
    "PairedTestResult",
    "PowerDerivation",
    "PowerError",
    "StatsError",
    "adjust_pvalues",
    "calculate_paired_power",
    "case_clustered_bootstrap",
    "paired_difference",
    "paired_randomisation_test",
    "read_attempt_records",
    "regenerate_tables",
    "wilson_interval",
    "write_power_record",
]
