"""Knowledge-boundary leakage as located events, not a bare rate.

Implements W3 item 5 (EVAL-6, PRD §9.2). A leakage event is a claim by role
*r* that references a reference field outside *r*'s permissible-knowledge set
under the active policy. Each event carries the turn it happened in, the
field path it revealed and a verbatim excerpt, so a reviewer can check any
reported rate against the text that produced it.

Two properties are enforced rather than trusted:

* **Field paths must exist in the reference schema** (Appendix D). A detector
  free to invent paths would produce events nothing can be checked against.
* **A field the speaker was permitted to see is not leakage.** Events citing
  a permitted path are dropped, so the detector cannot inflate a rate by
  flagging legitimate disclosure.

Under ``FULL`` the patient's permissible set is the whole reference, so
patient leakage is zero by construction. PRD §9.2 requires that be stated
wherever the policy comparison is reported, or ``FULL`` looks better than it
is; ``detail["permissible_is_total"]`` records it on every score.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from meddial.knowledge import KnowledgePolicy, ParticipantRole, addressable_paths
from meddial.llm import CallMetadata, DataClassification, LLMProvider

from .claims import DOCTOR_ROLE, PATIENT_ROLE, Turn, render_transcript
from .parsing import ResponseFormatError, parse_json_objects, require_keys
from .prompts import load_prompt
from .provenance import ReferenceMode, Score, ScoreProvenance, TurnScope

BOUNDARY_PROMPT = "boundary_check"
SCORER_ID = "meddial.evaluation.boundary"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_ATTEMPTS = 2

_REQUIRED_EVENT_KEYS = ("turn_index", "field_path", "excerpt")
_PARTICIPANT_FOR_ROLE = {
    PATIENT_ROLE: ParticipantRole.PATIENT,
    DOCTOR_ROLE: ParticipantRole.DOCTOR,
}
_TURN_SCOPE_FOR_ROLE = {PATIENT_ROLE: TurnScope.PATIENT, DOCTOR_ROLE: TurnScope.DOCTOR}


class BoundaryError(Exception):
    """The detector returned events that cannot be bound to the schema or transcript."""


@dataclass(frozen=True)
class LeakageEvent:
    """One located disclosure of a field the speaker should not have known."""

    turn_index: int
    role: str
    field_path: str
    policy: str
    excerpt: str

    def as_record(self) -> dict[str, Any]:
        """The shape PRD §6.3 stores in ``leakage_events[]``."""
        return {
            "turn_index": self.turn_index,
            "role": self.role,
            "field_path": self.field_path,
            "policy": self.policy,
            "excerpt": self.excerpt,
        }


def permissible_paths(policy: KnowledgePolicy, role: str) -> frozenset[str]:
    """The reference fields ``role`` is allowed to know under ``policy``."""
    participant = _PARTICIPANT_FOR_ROLE[role]
    if participant is ParticipantRole.PATIENT:
        return frozenset(policy.patient_visible)
    return frozenset(policy.doctor_visible)


def is_permitted(path: str, permitted: frozenset[str]) -> bool:
    """True when ``path`` is a permitted field or lives inside one."""
    return any(
        path == allowed or path.startswith((f"{allowed}.", f"{allowed}["))
        for allowed in permitted
    )


def _is_container_of(path: str, permitted: frozenset[str]) -> bool:
    """True when ``path`` is only an ancestor of permitted fields.

    A container is not itself leakable: revealing ``core`` means revealing one
    of its fields, and that field carries the verdict.
    """
    return any(allowed.startswith((f"{path}.", f"{path}[")) for allowed in permitted)


def leakable_paths(policy: KnowledgePolicy, role: str) -> frozenset[str]:
    """Reference fields ``role`` could leak — everything not already permitted."""
    permitted = permissible_paths(policy, role)
    return frozenset(
        path
        for path in addressable_paths()
        if not is_permitted(path, permitted) and not _is_container_of(path, permitted)
    )


def detect_leakage(
    turns: Sequence[Turn],
    policy: KnowledgePolicy,
    *,
    role: str = PATIENT_ROLE,
    provider: LLMProvider,
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    seed: int | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
) -> tuple[list[LeakageEvent], CallMetadata]:
    """Find every leakage event by ``role``. An empty list is a real result."""
    if not turns:
        raise BoundaryError("cannot check boundaries on an empty transcript")

    template = load_prompt(BOUNDARY_PROMPT)
    permitted = permissible_paths(policy, role)
    schema = addressable_paths()

    messages = template.render(
        role=role,
        permitted=json.dumps(sorted(permitted), indent=2),
        field_paths="\n".join(f"- {path}" for path in sorted(schema)),
        transcript=render_transcript(turns),
    )
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
            events = _parse_events(
                result.text,
                role=role,
                policy=policy,
                permitted=permitted,
                schema=schema,
                by_index=by_index,
            )
        except (ResponseFormatError, BoundaryError) as exc:
            last_error = exc
            continue
        return events, result.metadata

    raise BoundaryError(f"boundary check failed after {attempts} attempt(s): {last_error}")


def score_knowledge_boundary(
    turns: Sequence[Turn],
    policy: KnowledgePolicy,
    *,
    role: str = PATIENT_ROLE,
    provider: LLMProvider,
    temperature: float = 0.0,
    seed: int | None = None,
) -> tuple[Score, list[LeakageEvent]]:
    """Per-dialogue boundary compliance, plus the events behind it.

    The value is binary — 1.0 when the speaker leaked nothing, 0.0 otherwise —
    so the run-level mean is exactly the zero-leakage rate PRD §9.2 defines.
    The event list is returned separately because it belongs in the attempt
    record's ``leakage_events[]``, not only in a score's detail.
    """
    template = load_prompt(BOUNDARY_PROMPT)
    turn_scope = _TURN_SCOPE_FOR_ROLE[role]
    # PRD §9.2: when nothing is left to leak, zero leakage is definitional
    # rather than an achievement of the generator. True under FULL.
    permissible_is_total = not leakable_paths(policy, role)

    try:
        events, metadata = detect_leakage(
            turns, policy, role=role, provider=provider, temperature=temperature, seed=seed
        )
    except BoundaryError as exc:
        return (
            Score.incomplete(
                ScoreProvenance.unmeasured(
                    scorer_id=SCORER_ID,
                    reference_mode=ReferenceMode.POLICY_CONTEXT,
                    turn_scope=turn_scope,
                    prompt_version=template.version,
                    reason=f"boundary_check_failed: {exc}",
                ),
                detail={"policy": policy.key, "permissible_is_total": permissible_is_total},
            ),
            [],
        )

    score = Score.measured(
        0.0 if events else 1.0,
        ScoreProvenance.from_call(
            metadata,
            scorer_id=SCORER_ID,
            reference_mode=ReferenceMode.POLICY_CONTEXT,
            turn_scope=turn_scope,
            prompt_version=template.version,
        ),
        threshold=1.0,
        detail={
            "policy": policy.key,
            "event_count": len(events),
            "permissible_is_total": permissible_is_total,
            "events": [event.as_record() for event in events],
        },
    )
    return score, events


def _parse_events(
    text: str,
    *,
    role: str,
    policy: KnowledgePolicy,
    permitted: frozenset[str],
    schema: frozenset[str],
    by_index: dict[int, Turn],
) -> list[LeakageEvent]:
    events: list[LeakageEvent] = []
    for position, item in enumerate(parse_json_objects(text)):
        require_keys(item, _REQUIRED_EVENT_KEYS, position=position)

        raw_index = item["turn_index"]
        if isinstance(raw_index, bool) or not isinstance(raw_index, (int, str)):
            raise BoundaryError(f"event {position} has non-integer turn_index {raw_index!r}")
        try:
            turn_index = int(raw_index)
        except ValueError as exc:
            raise BoundaryError(f"event {position} has non-integer turn_index {raw_index!r}") from exc

        turn = by_index.get(turn_index)
        if turn is None:
            raise BoundaryError(
                f"event {position} cites turn {turn_index}, which is not in the transcript"
            )
        if turn.role != role:
            raise BoundaryError(
                f"event {position} cites turn {turn_index}, spoken by {turn.role}, not {role}"
            )

        field_path = str(item["field_path"]).strip()
        if field_path not in schema:
            raise BoundaryError(
                f"event {position} cites field path {field_path!r}, "
                "which is not in the reference schema"
            )

        # A field the speaker was permitted to know is disclosure, not leakage.
        if is_permitted(field_path, permitted):
            continue

        events.append(
            LeakageEvent(
                turn_index=turn_index,
                role=role,
                field_path=field_path,
                policy=policy.key,
                excerpt=str(item["excerpt"]).strip(),
            )
        )
    return events
