"""Atomic claim extraction over every turn of a dialogue.

Implements Implementation Plan §3.3 and W3 item 1. Two decisions here shape
every faithfulness number downstream:

* **Both speakers are extracted.** The thesis scored patient turns only, so a
  doctor who invented a lab value cost the dialogue nothing. Extracting from
  all turns is what makes ``doctor_factuality`` possible at all (EVAL-1,
  E0 confound 2).
* **Only :data:`FACTUAL` types are scored.** A doctor saying "this could be
  heart failure" is reasoning, not asserting. Scoring a hedged differential
  against the reference counts good clinical behaviour as a hallucination,
  which is a measurement error, not a finding.

Extraction fails loudly. A claim tagged with a turn that does not exist, or a
type outside the enum, means the judge lost alignment with the transcript;
the resulting claim set would be unattributable, so it is refused rather than
repaired.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from meddial.llm import CallMetadata, DataClassification, LLMProvider

from .parsing import ResponseFormatError, parse_json_objects, require_keys
from .prompts import load_prompt

PATIENT_ROLE = "Patient"
DOCTOR_ROLE = "Doctor"

EXTRACTION_PROMPT = "claim_extraction"
DEFAULT_MAX_TOKENS = 3072
DEFAULT_ATTEMPTS = 2

_ROLE_ALIASES = {
    "patient": PATIENT_ROLE,
    "user": PATIENT_ROLE,
    "doctor": DOCTOR_ROLE,
    "physician": DOCTOR_ROLE,
    "assistant": DOCTOR_ROLE,
}
_TEXT_KEYS = ("text", "content", "message", "utterance")
_ROLE_KEYS = ("role", "speaker")
_REQUIRED_CLAIM_KEYS = ("turn_index", "role", "type", "text")


class ClaimType(str, Enum):
    """What kind of thing a speaker said."""

    PATIENT_FACT = "patient_fact"
    DOCTOR_FACT = "doctor_fact"
    QUESTION = "question"
    DIAGNOSTIC_HYPOTHESIS = "diagnostic_hypothesis"
    RECOMMENDATION = "recommendation"
    ADVICE = "advice"
    NON_MEDICAL = "non_medical"


FACTUAL: frozenset[ClaimType] = frozenset({ClaimType.PATIENT_FACT, ClaimType.DOCTOR_FACT})
"""The only types checked against the reference. Everything else is reported but not scored."""

_ROLE_FOR_FACTUAL_TYPE = {
    ClaimType.PATIENT_FACT: PATIENT_ROLE,
    ClaimType.DOCTOR_FACT: DOCTOR_ROLE,
}


class ClaimExtractionError(Exception):
    """The judge returned a claim set that cannot be attributed to the transcript."""


def normalise_role(raw: object) -> str:
    """Map a speaker label onto ``Patient`` or ``Doctor``."""
    key = str(raw).strip().lower()
    if key not in _ROLE_ALIASES:
        raise ClaimExtractionError(f"unknown speaker role {raw!r}")
    return _ROLE_ALIASES[key]


@dataclass(frozen=True)
class Turn:
    """One utterance, with the index claims are attributed to."""

    index: int
    role: str
    text: str

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any], *, index: int) -> Turn:
        role = next((item[key] for key in _ROLE_KEYS if key in item), None)
        if role is None:
            raise ClaimExtractionError(f"turn {index} has no role")
        text = next((item[key] for key in _TEXT_KEYS if key in item), None)
        if text is None:
            raise ClaimExtractionError(f"turn {index} has no text")
        return cls(index=index, role=normalise_role(role), text=str(text))


def build_turns(raw: Iterable[Mapping[str, Any]]) -> list[Turn]:
    """Adapt the dialogue dicts the generator produces into indexed turns."""
    return [Turn.from_mapping(item, index=index) for index, item in enumerate(raw)]


@dataclass(frozen=True)
class Claim:
    """One atomic assertion, attributed to a speaker and a turn."""

    turn_index: int
    role: str
    type: ClaimType
    text: str

    @property
    def is_factual(self) -> bool:
        return self.type in FACTUAL


@dataclass(frozen=True)
class ClaimSet:
    """Every claim in one dialogue, plus the provenance of the call that found them."""

    claims: tuple[Claim, ...]
    metadata: CallMetadata
    prompt_version: str

    def __len__(self) -> int:
        return len(self.claims)

    def __iter__(self) -> Iterator[Claim]:
        return iter(self.claims)

    def for_role(self, role: str) -> list[Claim]:
        return [claim for claim in self.claims if claim.role == role]

    def factual_for_role(self, role: str) -> list[Claim]:
        """The claims a faithfulness score is computed over (EVAL-1)."""
        return [claim for claim in self.claims if claim.role == role and claim.is_factual]


def render_transcript(turns: Sequence[Turn]) -> str:
    """Format turns for the prompt, with the indices claims must cite back."""
    return "\n".join(f"[{turn.index}] {turn.role}: {turn.text}" for turn in turns)


def extract_claims(
    turns: Sequence[Turn],
    *,
    provider: LLMProvider,
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    seed: int | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
) -> ClaimSet:
    """Extract claims from every turn in one call.

    Retries once on a malformed or misaligned response, then raises. A
    :class:`~meddial.llm.errors.ProviderError` is never caught: a model that
    is unreachable is an infrastructure failure, not a measurement outcome.
    """
    if not turns:
        raise ClaimExtractionError("cannot extract claims from an empty transcript")

    template = load_prompt(EXTRACTION_PROMPT)
    messages = template.render(transcript=render_transcript(turns))
    by_index = {turn.index: turn for turn in turns}

    last_error: Exception | None = None
    for _ in range(max(1, attempts)):
        result = provider.complete(
            messages,
            classification=DataClassification.RESTRICTED_CLINICAL,
            temperature=temperature,
            max_tokens=max_tokens,
            seed=seed,
        )
        try:
            claims = _parse_claims(result.text, by_index)
        except (ResponseFormatError, ClaimExtractionError) as exc:
            last_error = exc
            continue
        return ClaimSet(
            claims=tuple(claims), metadata=result.metadata, prompt_version=template.version
        )

    raise ClaimExtractionError(f"claim extraction failed after {attempts} attempt(s): {last_error}")


def _parse_claims(text: str, by_index: Mapping[int, Turn]) -> list[Claim]:
    claims: list[Claim] = []
    for position, item in enumerate(parse_json_objects(text)):
        require_keys(item, _REQUIRED_CLAIM_KEYS, position=position)

        turn_index = _coerce_index(item["turn_index"], position=position)
        turn = by_index.get(turn_index)
        if turn is None:
            raise ClaimExtractionError(
                f"claim {position} cites turn {turn_index}, which is not in the transcript"
            )

        try:
            claim_type = ClaimType(str(item["type"]).strip().lower())
        except ValueError as exc:
            raise ClaimExtractionError(
                f"claim {position} has type {item['type']!r}, which is not a ClaimType"
            ) from exc

        role = normalise_role(item["role"])
        if role != turn.role:
            raise ClaimExtractionError(
                f"claim {position} is labelled {role} but turn {turn_index} is {turn.role}"
            )

        expected_role = _ROLE_FOR_FACTUAL_TYPE.get(claim_type)
        if expected_role is not None and expected_role != role:
            raise ClaimExtractionError(
                f"claim {position} is typed {claim_type.value} but was spoken by {role}"
            )

        claim_text = str(item["text"]).strip()
        if not claim_text:
            raise ClaimExtractionError(f"claim {position} has empty text")

        claims.append(Claim(turn_index=turn_index, role=role, type=claim_type, text=claim_text))
    return claims


def _coerce_index(raw: object, *, position: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ClaimExtractionError(f"claim {position} has non-integer turn_index {raw!r}")
    try:
        return int(raw)
    except ValueError as exc:
        raise ClaimExtractionError(f"claim {position} has non-integer turn_index {raw!r}") from exc
