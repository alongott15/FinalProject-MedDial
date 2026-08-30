"""Naturalness scoring — a GEval-style rubric with no fallback.

Implements W3 item 4 (EVAL-4, PRD §9.3), and closes defect D-06.

The scorer this replaces caught every exception, logged a warning, and
switched to an ad-hoc direct-LLM scorer whose identity was never recorded —
so a number in the results table could have come from either scorer and
nothing said which. There is no fallback here. A naturalness score that
cannot be computed is reported as ``INCOMPLETE`` with the reason attached,
and the dialogue is excluded from the aggregate rather than defaulted to
0.5.
"""

from __future__ import annotations

from collections.abc import Sequence

from meddial.llm import CallMetadata, DataClassification, LLMProvider

from .claims import Turn, render_transcript
from .parsing import ResponseFormatError, parse_json_object
from .prompts import load_prompt
from .provenance import ReferenceMode, Score, ScoreProvenance, TurnScope

NATURALNESS_PROMPT = "naturalness"
SCORER_ID = "meddial.evaluation.naturalness"
DEFAULT_MAX_TOKENS = 512
DEFAULT_ATTEMPTS = 2
DEFAULT_TEMPERATURE = 0.1
MAX_TEMPERATURE = 0.1
"""PRD §9.3 requires deterministic settings: temperature <= 0.1, fixed seed."""


class NaturalnessError(Exception):
    """The rater did not return a usable ``{score, rationale}`` object."""


def rate_naturalness(
    turns: Sequence[Turn],
    *,
    provider: LLMProvider,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    seed: int | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
) -> tuple[float, str, CallMetadata]:
    """Return ``(score, rationale, call_metadata)`` or raise.

    Raises rather than returning a stand-in, which is the whole point of
    EVAL-4. :func:`score_naturalness` is the caller that turns a raise into
    an explicit ``INCOMPLETE``.
    """
    if not turns:
        raise NaturalnessError("cannot rate an empty transcript")
    if temperature > MAX_TEMPERATURE:
        raise NaturalnessError(
            f"temperature {temperature} exceeds the {MAX_TEMPERATURE} ceiling PRD §9.3 sets"
        )

    template = load_prompt(NATURALNESS_PROMPT)
    messages = template.render(transcript=render_transcript(turns))

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
            payload = parse_json_object(result.text)
            score = _coerce_score(payload)
        except (ResponseFormatError, NaturalnessError) as exc:
            last_error = exc
            continue
        rationale = str(payload.get("rationale", "")).strip()
        return score, rationale, result.metadata

    raise NaturalnessError(f"naturalness rating failed after {attempts} attempt(s): {last_error}")


def score_naturalness(
    turns: Sequence[Turn],
    *,
    provider: LLMProvider,
    threshold: float | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    seed: int | None = None,
) -> Score:
    """Score naturalness, or report ``INCOMPLETE``. Never a default number."""
    template = load_prompt(NATURALNESS_PROMPT)
    try:
        value, rationale, metadata = rate_naturalness(
            turns, provider=provider, temperature=temperature, seed=seed
        )
    except NaturalnessError as exc:
        return Score.incomplete(
            ScoreProvenance.unmeasured(
                scorer_id=SCORER_ID,
                reference_mode=ReferenceMode.FULL_REFERENCE,
                turn_scope=TurnScope.BOTH,
                prompt_version=template.version,
                reason=f"naturalness_failed: {exc}",
            ),
            detail={"turns": len(turns)},
        )

    return Score.measured(
        value,
        ScoreProvenance.from_call(
            metadata,
            scorer_id=SCORER_ID,
            reference_mode=ReferenceMode.FULL_REFERENCE,
            turn_scope=TurnScope.BOTH,
            prompt_version=template.version,
        ),
        threshold=threshold,
        detail={"turns": len(turns), "rationale": rationale},
    )


def _coerce_score(payload: dict[str, object]) -> float:
    if "score" not in payload:
        raise NaturalnessError("response has no 'score' key")
    raw = payload["score"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise NaturalnessError(f"score {raw!r} is not a number")
    try:
        value = float(raw)
    except ValueError as exc:
        raise NaturalnessError(f"score {raw!r} is not a number") from exc
    if not 0.0 <= value <= 1.0:
        raise NaturalnessError(f"score {value} outside [0, 1]")
    return value
