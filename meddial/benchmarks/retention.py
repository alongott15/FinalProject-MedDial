"""Dialogue-only fact extraction and coded-ground-truth retention (BENCH-4)."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from meddial.evaluation import ResponseFormatError, Turn, parse_json_object, render_transcript
from meddial.grounding import (
    CodedCase,
    ExtractedCase,
    ExtractedEntity,
    GroundingReport,
    Matcher,
    MatcherErrorRate,
    evaluate_structured_matches,
)
from meddial.llm import (
    CallMetadata,
    ChatMessage,
    DataClassification,
    LLMProvider,
)

_TEMPLATE_PATH = Path(__file__).with_name("templates") / "retention_extraction.md"
_SYSTEM_MARKER = "---SYSTEM---"
_USER_MARKER = "---USER---"


class RetentionError(ValueError):
    """Retention extraction or comparison is invalid."""


@dataclass(frozen=True)
class RenderedRetentionPrompt:
    messages: tuple[ChatMessage, ...]
    prompt_version: str


@dataclass(frozen=True)
class RetainedFacts:
    diagnoses: tuple[str, ...]
    medications: tuple[str, ...]
    metadata: CallMetadata
    prompt_version: str


@dataclass(frozen=True)
class RetentionCase:
    case_id: str
    policy_id: str
    turns: tuple[Turn, ...]
    coded: CodedCase
    generator_families: frozenset[str]

    def __post_init__(self) -> None:
        if self.coded.case_id != self.case_id:
            raise RetentionError("retention case and coded ground truth IDs differ")


@dataclass(frozen=True)
class RetentionPolicyResult:
    policy_id: str
    grounding: GroundingReport
    calls: tuple[CallMetadata, ...]
    extractor_family: str

    def as_record(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "extractor_family": self.extractor_family,
            "grounding": self.grounding.as_record(),
            "calls": len(self.calls),
            "prompt_tokens": sum(call.prompt_tokens for call in self.calls),
            "completion_tokens": sum(call.completion_tokens for call in self.calls),
        }


@dataclass(frozen=True)
class RetentionReport:
    by_policy: Mapping[str, RetentionPolicyResult]

    def as_record(self) -> dict[str, Any]:
        return {
            policy: result.as_record()
            for policy, result in sorted(self.by_policy.items())
        }


def render_retention_prompt(turns: Sequence[Turn]) -> RenderedRetentionPrompt:
    """Render a prompt whose only clinical input is the dialogue.

    There is deliberately no ``reference`` parameter.  This API shape makes it
    impossible for a caller to interpolate the SCR or coded rows accidentally.
    """

    if not turns:
        raise RetentionError("cannot extract retention from an empty dialogue")
    raw = _TEMPLATE_PATH.read_text()
    if raw.count(_SYSTEM_MARKER) != 1 or raw.count(_USER_MARKER) != 1:
        raise RetentionError("retention prompt template markers are invalid")
    system_part, user_part = raw.split(_USER_MARKER)
    system = system_part.replace(_SYSTEM_MARKER, "", 1).strip()
    user = user_part.strip().format(transcript=render_transcript(turns))
    version = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return RenderedRetentionPrompt(
        messages=(ChatMessage("system", system), ChatMessage("user", user)),
        prompt_version=version,
    )


def extract_retained_facts(
    turns: Sequence[Turn],
    *,
    provider: LLMProvider,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    seed: int | None = None,
) -> RetainedFacts:
    rendered = render_retention_prompt(turns)
    result = provider.complete(
        rendered.messages,
        classification=DataClassification.RESTRICTED_CLINICAL,
        temperature=temperature,
        max_tokens=max_tokens,
        seed=seed,
    )
    try:
        payload = parse_json_object(result.text)
        diagnoses = _string_array(payload, "diagnoses")
        medications = _string_array(payload, "medications")
        unknown = set(payload) - {"diagnoses", "medications"}
        if unknown:
            raise RetentionError(
                f"retention extraction returned unknown fields: {sorted(unknown)}"
            )
    except ResponseFormatError as exc:
        raise RetentionError(f"retention extraction returned malformed JSON: {exc}") from exc
    return RetainedFacts(
        diagnoses=diagnoses,
        medications=medications,
        metadata=result.metadata,
        prompt_version=rendered.prompt_version,
    )


def evaluate_retention(
    cases: Sequence[RetentionCase],
    *,
    provider: LLMProvider,
    diagnosis_matcher: Matcher,
    medication_matcher: Matcher,
    diagnosis_matcher_error: MatcherErrorRate,
    medication_matcher_error: MatcherErrorRate,
    run_started_at: datetime,
    resamples: int = 2000,
    seed: int = 0,
) -> RetentionReport:
    """Extract each dialogue in isolation and report retention per policy."""

    if not cases:
        raise RetentionError("no retention cases were supplied")
    seen: set[tuple[str, str]] = set()
    extracted_by_policy: dict[str, list[ExtractedCase]] = defaultdict(list)
    coded_by_policy: dict[str, list[CodedCase]] = defaultdict(list)
    calls_by_policy: dict[str, list[CallMetadata]] = defaultdict(list)
    for offset, case in enumerate(cases):
        key = (case.policy_id, case.case_id)
        if key in seen:
            raise RetentionError(f"duplicate retention case {case.case_id}/{case.policy_id}")
        seen.add(key)
        if provider.model_family in case.generator_families:
            raise RetentionError(
                f"retention extractor family {provider.model_family!r} also generated "
                f"case {case.case_id}; BENCH-4 requires an independent family"
            )
        facts = extract_retained_facts(
            case.turns, provider=provider, seed=seed + offset
        )
        extracted_by_policy[case.policy_id].append(
            ExtractedCase(
                case_id=case.case_id,
                diagnoses=tuple(ExtractedEntity(value) for value in facts.diagnoses),
                medications=tuple(ExtractedEntity(value) for value in facts.medications),
            )
        )
        coded_by_policy[case.policy_id].append(case.coded)
        calls_by_policy[case.policy_id].append(facts.metadata)

    reports = {}
    for policy_offset, policy_id in enumerate(sorted(extracted_by_policy)):
        grounding = evaluate_structured_matches(
            extracted_by_policy[policy_id],
            coded_by_policy[policy_id],
            diagnosis_matcher=diagnosis_matcher,
            medication_matcher=medication_matcher,
            diagnosis_matcher_error=diagnosis_matcher_error,
            medication_matcher_error=medication_matcher_error,
            run_started_at=run_started_at,
            resamples=resamples,
            seed=seed + policy_offset,
        )
        reports[policy_id] = RetentionPolicyResult(
            policy_id=policy_id,
            grounding=grounding,
            calls=tuple(calls_by_policy[policy_id]),
            extractor_family=provider.model_family,
        )
    return RetentionReport(by_policy=reports)


def _string_array(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    if key not in payload or not isinstance(payload[key], list):
        raise RetentionError(f"retention extraction field {key!r} must be an array")
    values = []
    for position, value in enumerate(payload[key]):
        if not isinstance(value, str) or not value.strip():
            raise RetentionError(f"{key}[{position}] must be a non-empty string")
        values.append(value.strip())
    # Repetition is not extra retained information and must not inflate FP.
    return tuple(dict.fromkeys(values))


__all__ = [
    "RenderedRetentionPrompt",
    "RetainedFacts",
    "RetentionCase",
    "RetentionError",
    "RetentionPolicyResult",
    "RetentionReport",
    "evaluate_retention",
    "extract_retained_facts",
    "render_retention_prompt",
]
