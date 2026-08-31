"""Public, synthetic-only validation benchmarks for MedDial instruments."""

from meddial.benchmarks.detector_eval import (
    DetectorMetrics,
    DetectorObservation,
    DetectorReport,
    evaluate_detector,
)
from meddial.benchmarks.discriminate import (
    DialoguePolicyRecord,
    DiscriminationError,
    DiscriminationReport,
    PolicySplit,
    case_split,
    evaluate_policy_discrimination,
)
from meddial.benchmarks.injection import (
    CorruptionType,
    InjectedError,
    InjectionError,
    InjectionResult,
    inject_fault,
    inject_suite,
    recover_injected_error,
)
from meddial.benchmarks.retention import (
    RetainedFacts,
    RetentionCase,
    RetentionError,
    RetentionReport,
    evaluate_retention,
    extract_retained_facts,
    render_retention_prompt,
)

__all__ = [
    "CorruptionType",
    "DetectorMetrics",
    "DetectorObservation",
    "DetectorReport",
    "DialoguePolicyRecord",
    "DiscriminationError",
    "DiscriminationReport",
    "InjectedError",
    "InjectionError",
    "InjectionResult",
    "PolicySplit",
    "RetainedFacts",
    "RetentionCase",
    "RetentionError",
    "RetentionReport",
    "case_split",
    "evaluate_detector",
    "evaluate_policy_discrimination",
    "evaluate_retention",
    "extract_retained_facts",
    "inject_fault",
    "inject_suite",
    "recover_injected_error",
    "render_retention_prompt",
]
