"""Measurement layer: claims, verdicts, scores and the provenance of each.

Implements Implementation Plan §3.3. What is here so far is what the E0 gate
needs — claim extraction over all turns, role-separated and
reference-selectable faithfulness with batched verification, and a score type
that cannot exist without provenance. Naturalness, boundary, structural,
acceptance and the evaluator ensemble follow in the rest of W3.
"""

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
    SCORER_ID,
    ClaimVerdict,
    Verdict,
    VerificationError,
    VerificationResult,
    reference_payload,
    score_dialogue_faithfulness,
    score_faithfulness,
    verify_claims,
)
from .parsing import ResponseFormatError, parse_json_objects
from .prompts import PromptError, PromptTemplate, load_prompt
from .provenance import (
    EvaluationStatus,
    ReferenceMode,
    Score,
    ScoreProvenance,
    TurnScope,
)

__all__ = [
    "DOCTOR_ROLE",
    "FACTUAL",
    "PATIENT_ROLE",
    "SCORER_ID",
    "Claim",
    "ClaimExtractionError",
    "ClaimSet",
    "ClaimType",
    "ClaimVerdict",
    "EvaluationStatus",
    "PromptError",
    "PromptTemplate",
    "ReferenceMode",
    "ResponseFormatError",
    "Score",
    "ScoreProvenance",
    "Turn",
    "TurnScope",
    "Verdict",
    "VerificationError",
    "VerificationResult",
    "build_turns",
    "extract_claims",
    "load_prompt",
    "normalise_role",
    "parse_json_objects",
    "reference_payload",
    "render_transcript",
    "score_dialogue_faithfulness",
    "score_faithfulness",
    "verify_claims",
]
