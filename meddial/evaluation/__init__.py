"""Measurement layer: claims, verdicts, scores and the provenance of each.

Implements Implementation Plan §3.3. All five scored dimensions are here —
role-separated faithfulness with reference-mode selection, naturalness with
no fallback, located knowledge-boundary leakage, deterministic structural
validity — plus the acceptance gate that combines them and keeps the thesis
composite outside the decision. The evaluator ensemble (EVAL-8) follows in
W3b, after the E0 gate reports.

``SCORER_ID`` is deliberately not re-exported: each scorer defines its own,
and an unqualified one at package level would attribute a score to the wrong
module. Import it from the scorer you mean.
"""

from .acceptance import (
    COMPOSITE_NOTE,
    COMPOSITE_WEIGHTS,
    DEFAULT_THRESHOLDS,
    DOCTOR_FACTUALITY,
    KNOWLEDGE_BOUNDARY,
    MANDATORY_DIMENSIONS,
    NATURALNESS,
    PATIENT_FACTUALITY,
    STRUCTURAL_VALIDITY,
    Acceptance,
    AcceptanceResult,
    Composite,
    compute_composite,
    decide,
    gate,
)
from .boundary import (
    BoundaryError,
    LeakageEvent,
    detect_leakage,
    is_permitted,
    leakable_paths,
    permissible_paths,
    score_knowledge_boundary,
)
from .claims import (
    DOCTOR_ROLE,
    FACTUAL,
    PATIENT_ROLE,
    Claim,
    ClaimExtractionError,
    ClaimSet,
    ClaimType,
    Turn,
    build_turns,
    extract_claims,
    normalise_role,
    render_transcript,
)
from .faithfulness import (
    ClaimVerdict,
    Verdict,
    VerificationError,
    VerificationResult,
    reference_payload,
    score_dialogue_faithfulness,
    score_faithfulness,
    verify_claims,
)
from .naturalness import NaturalnessError, rate_naturalness, score_naturalness
from .parsing import ResponseFormatError, parse_json_object, parse_json_objects
from .prompts import PromptError, PromptTemplate, load_prompt
from .provenance import (
    DETERMINISTIC,
    EvaluationStatus,
    ReferenceMode,
    Score,
    ScoreProvenance,
    TurnScope,
)
from .structural import (
    ERROR_SENTINEL_PATTERNS,
    StructuralConfig,
    StructuralReport,
    StructuralViolation,
    check_structure,
    score_structural_validity,
)

__all__ = [
    "COMPOSITE_NOTE",
    "COMPOSITE_WEIGHTS",
    "DEFAULT_THRESHOLDS",
    "DETERMINISTIC",
    "DOCTOR_FACTUALITY",
    "DOCTOR_ROLE",
    "ERROR_SENTINEL_PATTERNS",
    "FACTUAL",
    "KNOWLEDGE_BOUNDARY",
    "MANDATORY_DIMENSIONS",
    "NATURALNESS",
    "PATIENT_FACTUALITY",
    "PATIENT_ROLE",
    "STRUCTURAL_VALIDITY",
    "Acceptance",
    "AcceptanceResult",
    "BoundaryError",
    "Claim",
    "ClaimExtractionError",
    "ClaimSet",
    "ClaimType",
    "ClaimVerdict",
    "Composite",
    "EvaluationStatus",
    "LeakageEvent",
    "NaturalnessError",
    "PromptError",
    "PromptTemplate",
    "ReferenceMode",
    "ResponseFormatError",
    "Score",
    "ScoreProvenance",
    "StructuralConfig",
    "StructuralReport",
    "StructuralViolation",
    "Turn",
    "TurnScope",
    "Verdict",
    "VerificationError",
    "VerificationResult",
    "build_turns",
    "check_structure",
    "compute_composite",
    "decide",
    "detect_leakage",
    "extract_claims",
    "gate",
    "is_permitted",
    "leakable_paths",
    "load_prompt",
    "normalise_role",
    "parse_json_object",
    "parse_json_objects",
    "permissible_paths",
    "rate_naturalness",
    "reference_payload",
    "render_transcript",
    "score_dialogue_faithfulness",
    "score_faithfulness",
    "score_knowledge_boundary",
    "score_naturalness",
    "score_structural_validity",
    "verify_claims",
]
