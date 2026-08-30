"""Role-separated, reference-selectable, batched faithfulness scoring.

Implements Implementation Plan W3 items 2 and 8 (EVAL-1, EVAL-2, EVAL-5,
EVAL-10). Four properties the thesis implementation did not have:

* **Patient and doctor are scored separately** (EVAL-1). A single blended
  number cannot distinguish a patient who over-discloses from a doctor who
  fabricates.
* **The reference is an explicit input** (EVAL-2). Scoring against the policy
  context makes the reference shrink as disclosure is restricted, which alone
  can produce a rising faithfulness trend. Both modes are runnable and each
  is recorded on the score, which is what E0 needs to tell the two apart.
* **An unmeasurable dimension reports ``INCOMPLETE``** (EVAL-5), never a
  default. A dialogue with no factual claims has no faithfulness; recording
  ``0.0`` or ``1.0`` would move a published mean.
* **Verification is one call for all claims** (EVAL-10), validated by count
  and index. The per-claim path is kept only as the baseline that criterion
  is measured against.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from meddial.knowledge import EvaluatorContext, ParticipantRole, strip_evidence
from meddial.llm import CallMetadata, DataClassification, LLMProvider

from .claims import (
    DOCTOR_ROLE,
    PATIENT_ROLE,
    Claim,
    ClaimExtractionError,
    ClaimSet,
    Turn,
    extract_claims,
)
from .parsing import ResponseFormatError, parse_json_objects, require_keys
from .prompts import load_prompt
from .provenance import ReferenceMode, Score, ScoreProvenance, TurnScope

VERIFICATION_PROMPT = "claim_verification"
SCORER_ID = "meddial.evaluation.faithfulness"
DEFAULT_MAX_TOKENS = 2048
DEFAULT_ATTEMPTS = 2

_REQUIRED_VERDICT_KEYS = ("claim_index", "verdict")
_TURN_SCOPE_FOR_ROLE = {PATIENT_ROLE: TurnScope.PATIENT, DOCTOR_ROLE: TurnScope.DOCTOR}
_DIMENSION_FOR_ROLE = {PATIENT_ROLE: "patient_factuality", DOCTOR_ROLE: "doctor_factuality"}


class Verdict(str, Enum):
    """Whether the reference bears a claim out."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNVERIFIABLE = "unverifiable"


class VerificationError(Exception):
    """The judge's verdicts could not be aligned with the claims sent to it."""


@dataclass(frozen=True)
class ClaimVerdict:
    """One verdict, bound to the claim index it was asked about."""

    claim_index: int
    verdict: Verdict
    justification: str = ""


@dataclass(frozen=True)
class VerificationResult:
    """Verdicts for one claim list, in claim order, plus call provenance."""

    verdicts: tuple[ClaimVerdict, ...]
    metadata: CallMetadata
    prompt_version: str
    calls: int


def reference_payload(context: EvaluatorContext, mode: ReferenceMode) -> dict[str, Any]:
    """The evidence a claim is checked against, chosen explicitly (EVAL-2).

    ``POLICY_CONTEXT`` returns exactly what the patient was permitted to see
    under the run's knowledge policy. ``FULL_REFERENCE`` returns the whole
    clinical reference regardless of policy. Evidence spans are stripped from
    both: they cite the source note, which the judge must not see.
    """
    if mode is ReferenceMode.FULL_REFERENCE:
        return strip_evidence(context.reference.model_dump(mode="json"))
    return dict(context.policy.mask(context.reference, ParticipantRole.PATIENT))


def render_reference(payload: Mapping[str, Any]) -> str:
    """Serialise the reference deterministically, so the prompt is stable."""
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


def render_claims(claims: Sequence[Claim]) -> str:
    """Number the claims. These indices are what the verdicts must cite back."""
    return "\n".join(f"[{index}] ({claim.role}) {claim.text}" for index, claim in enumerate(claims))


def verify_claims(
    claims: Sequence[Claim],
    reference: Mapping[str, Any],
    *,
    provider: LLMProvider,
    batched: bool = True,
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    seed: int | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
) -> VerificationResult:
    """Check every claim against the reference.

    With ``batched=True`` (EVAL-10) this is one call returning a verdict array
    for all claims; a response whose length differs from the claim count, or
    whose indices are not each present exactly once, is retried once and then
    raises. ``batched=False`` issues one call per claim and exists as the
    baseline the batching speed-up is measured against.
    """
    if not claims:
        raise VerificationError("cannot verify an empty claim list")

    template = load_prompt(VERIFICATION_PROMPT)
    rendered_reference = render_reference(reference)

    if not batched:
        return _verify_per_claim(
            claims,
            rendered_reference,
            template=template,
            provider=provider,
            temperature=temperature,
            max_tokens=max_tokens,
            seed=seed,
            attempts=attempts,
        )

    messages = template.render(
        reference=rendered_reference,
        claims=render_claims(claims),
        claim_count=str(len(claims)),
        last_index=str(len(claims) - 1),
    )

    calls = 0
    last_error: Exception | None = None
    for _ in range(max(1, attempts)):
        result = provider.complete(
            messages,
            classification=DataClassification.RESTRICTED_CLINICAL,
            temperature=temperature,
            max_tokens=max_tokens,
            seed=seed,
        )
        calls += 1
        try:
            verdicts = _parse_verdicts(result.text, expected=len(claims))
        except (ResponseFormatError, VerificationError) as exc:
            last_error = exc
            continue
        return VerificationResult(
            verdicts=tuple(verdicts),
            metadata=result.metadata,
            prompt_version=template.version,
            calls=calls,
        )

    raise VerificationError(f"verdicts unaligned after {attempts} attempt(s): {last_error}")


def score_faithfulness(
    claims: ClaimSet | Sequence[Claim],
    context: EvaluatorContext,
    *,
    role: str,
    reference_mode: ReferenceMode,
    provider: LLMProvider,
    threshold: float | None = None,
    batched: bool = True,
    temperature: float = 0.0,
    seed: int | None = None,
) -> Score:
    """Score one speaker's factual claims against the selected reference.

    EVAL-1/2/10. Returns ``INCOMPLETE`` — never a default number — when the
    speaker made no factual claims (EVAL-5), when every verdict was
    ``unverifiable``, or when the judge's verdicts could not be aligned.
    """
    template = load_prompt(VERIFICATION_PROMPT)
    turn_scope = _TURN_SCOPE_FOR_ROLE[role]
    all_claims = list(claims)
    factual = [claim for claim in all_claims if claim.role == role and claim.is_factual]

    def unmeasured(reason: str, detail: Mapping[str, Any] | None = None) -> Score:
        return Score.incomplete(
            ScoreProvenance.unmeasured(
                scorer_id=SCORER_ID,
                reference_mode=reference_mode,
                turn_scope=turn_scope,
                prompt_version=template.version,
                reason=reason,
            ),
            detail=detail,
        )

    base_detail: dict[str, Any] = {
        "claims_total": len(all_claims),
        "claims_for_role": sum(1 for claim in all_claims if claim.role == role),
        "factual_claims": len(factual),
    }

    if not factual:
        return unmeasured("no_factual_claims", base_detail)

    try:
        verification = verify_claims(
            factual,
            reference_payload(context, reference_mode),
            provider=provider,
            batched=batched,
            temperature=temperature,
            seed=seed,
        )
    except (VerificationError, ResponseFormatError) as exc:
        return unmeasured(f"verification_failed: {exc}", base_detail)

    counts = {verdict: 0 for verdict in Verdict}
    for claim_verdict in verification.verdicts:
        counts[claim_verdict.verdict] += 1

    detail = {
        **base_detail,
        "supported": counts[Verdict.SUPPORTED],
        "unsupported": counts[Verdict.UNSUPPORTED],
        "unverifiable": counts[Verdict.UNVERIFIABLE],
        "calls": verification.calls,
        "batched": batched,
        "verdicts": [
            {
                "claim_index": claim_verdict.claim_index,
                "turn_index": factual[claim_verdict.claim_index].turn_index,
                "type": factual[claim_verdict.claim_index].type.value,
                "text": factual[claim_verdict.claim_index].text,
                "verdict": claim_verdict.verdict.value,
                "justification": claim_verdict.justification,
            }
            for claim_verdict in verification.verdicts
        ],
    }

    decidable = counts[Verdict.SUPPORTED] + counts[Verdict.UNSUPPORTED]
    if decidable == 0:
        return unmeasured("all_claims_unverifiable", detail)

    return Score.measured(
        counts[Verdict.SUPPORTED] / decidable,
        ScoreProvenance.from_call(
            verification.metadata,
            scorer_id=SCORER_ID,
            reference_mode=reference_mode,
            turn_scope=turn_scope,
            prompt_version=verification.prompt_version,
        ),
        threshold=threshold,
        detail=detail,
    )


def score_dialogue_faithfulness(
    turns: Sequence[Turn],
    context: EvaluatorContext,
    *,
    provider: LLMProvider,
    reference_mode: ReferenceMode,
    threshold: float | None = None,
    batched: bool = True,
    temperature: float = 0.0,
    seed: int | None = None,
) -> dict[str, Score]:
    """Extract once, then score both speakers separately (EVAL-1).

    Extraction is shared: the same claim set feeds both scores, so a
    difference between them is a difference in the speakers, not in two
    independent extraction passes.
    """
    template = load_prompt(VERIFICATION_PROMPT)
    try:
        claim_set = extract_claims(turns, provider=provider, temperature=temperature, seed=seed)
    except ClaimExtractionError as exc:
        return {
            dimension: Score.incomplete(
                ScoreProvenance.unmeasured(
                    scorer_id=SCORER_ID,
                    reference_mode=reference_mode,
                    turn_scope=_TURN_SCOPE_FOR_ROLE[role],
                    prompt_version=template.version,
                    reason=f"claim_extraction_failed: {exc}",
                )
            )
            for role, dimension in _DIMENSION_FOR_ROLE.items()
        }

    return {
        dimension: score_faithfulness(
            claim_set,
            context,
            role=role,
            reference_mode=reference_mode,
            provider=provider,
            threshold=threshold,
            batched=batched,
            temperature=temperature,
            seed=seed,
        )
        for role, dimension in _DIMENSION_FOR_ROLE.items()
    }


def _verify_per_claim(
    claims: Sequence[Claim],
    rendered_reference: str,
    *,
    template: Any,
    provider: LLMProvider,
    temperature: float,
    max_tokens: int,
    seed: int | None,
    attempts: int,
) -> VerificationResult:
    """One call per claim. The baseline, not the production path."""
    verdicts: list[ClaimVerdict] = []
    metadata: CallMetadata | None = None
    calls = 0

    for index, claim in enumerate(claims):
        messages = template.render(
            reference=rendered_reference,
            claims=f"[0] ({claim.role}) {claim.text}",
            claim_count="1",
            last_index="0",
        )
        last_error: Exception | None = None
        for _ in range(max(1, attempts)):
            result = provider.complete(
                messages,
                classification=DataClassification.RESTRICTED_CLINICAL,
                temperature=temperature,
                max_tokens=max_tokens,
                seed=seed,
            )
            calls += 1
            metadata = result.metadata
            try:
                parsed = _parse_verdicts(result.text, expected=1)
            except (ResponseFormatError, VerificationError) as exc:
                last_error = exc
                continue
            verdicts.append(ClaimVerdict(index, parsed[0].verdict, parsed[0].justification))
            break
        else:
            raise VerificationError(f"claim {index} unresolved after {attempts}: {last_error}")

    if metadata is None:  # unreachable: claims is non-empty
        raise VerificationError("no verification call was made")
    return VerificationResult(
        verdicts=tuple(verdicts),
        metadata=metadata,
        prompt_version=template.version,
        calls=calls,
    )


def _parse_verdicts(text: str, *, expected: int) -> list[ClaimVerdict]:
    """Parse and align verdicts, or raise. Alignment is the whole point (EVAL-10)."""
    items = parse_json_objects(text)
    if len(items) != expected:
        raise VerificationError(f"expected {expected} verdict(s), got {len(items)}")

    by_index: dict[int, ClaimVerdict] = {}
    for position, item in enumerate(items):
        require_keys(item, _REQUIRED_VERDICT_KEYS, position=position)

        raw_index = item["claim_index"]
        if isinstance(raw_index, bool) or not isinstance(raw_index, (int, str)):
            raise VerificationError(f"verdict {position} has non-integer claim_index {raw_index!r}")
        try:
            claim_index = int(raw_index)
        except ValueError as exc:
            raise VerificationError(
                f"verdict {position} has non-integer claim_index {raw_index!r}"
            ) from exc

        if not 0 <= claim_index < expected:
            raise VerificationError(
                f"verdict {position} cites claim {claim_index}, outside 0..{expected - 1}"
            )
        if claim_index in by_index:
            raise VerificationError(f"claim {claim_index} received more than one verdict")

        try:
            verdict = Verdict(str(item["verdict"]).strip().lower())
        except ValueError as exc:
            raise VerificationError(
                f"verdict {position} has value {item['verdict']!r}, which is not a Verdict"
            ) from exc

        by_index[claim_index] = ClaimVerdict(
            claim_index=claim_index,
            verdict=verdict,
            justification=str(item.get("justification", "")).strip(),
        )

    return [by_index[index] for index in range(expected)]
