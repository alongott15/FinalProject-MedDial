"""The run composition layer: five architectures wired to real models (EXP-5).

:mod:`meddial.experiments.variants` deliberately stops at the architecture
boundary -- it declares five implementations and dispatches to five named
methods, but constructs no provider, so the boundary stays testable offline.
This module is the other half: the object those methods are dispatched to.

Three properties are worth stating because they are easy to get wrong and
expensive to detect afterwards.

**The architectures differ structurally, not by flag.** ``direct_llm`` never
sees a structured reference; the single-agent variant writes both speakers in
one completion; the multi-agent variants alternate two agents holding separate
views of the case. A variant whose stage list omits ``knowledge_policy``
refuses to run under a restrictive policy rather than quietly applying or
quietly ignoring it -- see :class:`PolicyStageError`.

**Measurement is identical across architectures.** All five are scored on the
same five dimensions against the same reference mode. Only ``full_meddial``
declares ``targeted_repair`` as a stage, and it earns that by being the only
variant configured with ``max_attempts > 1``; the repair loop itself lives in
:class:`~meddial.experiments.runner.ExperimentRunner`, which re-dispatches with
a plan attached. Scoring the full system more thoroughly than its baselines
would manufacture the difference the study exists to measure.

**The structured reference is an input, not a per-attempt derivation.** Cases
carry an already-extracted reference (W2/W3 own that). Re-extracting inside an
attempt would make two attempts on one case incomparable and would bill the
extraction to every repair round.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from meddial.evaluation import (
    DOCTOR_FACTUALITY,
    DOCTOR_ROLE,
    KNOWLEDGE_BOUNDARY,
    NATURALNESS,
    PATIENT_FACTUALITY,
    PATIENT_ROLE,
    STRUCTURAL_VALIDITY,
    BoundaryError,
    ClaimExtractionError,
    ReferenceMode,
    Score,
    ScoreProvenance,
    StructuralConfig,
    TurnScope,
    VerificationError,
    build_turns,
    decide,
    extract_claims,
    parse_json_object,
    score_faithfulness,
    score_knowledge_boundary,
    score_naturalness,
    score_structural_validity,
)
from meddial.knowledge import (
    PolicyRegistry,
    StructuredClinicalReference,
    build_contexts,
    to_legacy_profile,
)
from meddial.llm import (
    ChatMessage,
    CompletionResult,
    DataClassification,
    LLMProvider,
)

from .config import ModelSpec, RunConfig
from .variants import VariantName, VariantRequest

FULL_DISCLOSURE_POLICY_ID = "FULL"
"""The only patient policy a variant without a ``knowledge_policy`` stage may use."""

_POLICY_STAGE_VARIANTS = frozenset(
    {VariantName.KNOWLEDGE_CONTROLLED, VariantName.FULL_MEDDIAL}
)

_PATIENT_REPAIR_TARGETS = frozenset(
    {"patient_prompt", "patient_context_guard", "patient_style"}
)
_DOCTOR_REPAIR_TARGETS = frozenset({"doctor_prompt", "doctor_style"})


class BackendError(RuntimeError):
    """The composition layer could not execute an architecture."""


class CaseInputError(BackendError):
    """A case does not carry the inputs its architecture needs."""


class PolicyStageError(BackendError):
    """A variant without a knowledge-policy stage was given a restrictive policy."""


class DialogueFormatError(BackendError):
    """A single-completion variant did not return a parseable dialogue."""


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------


class RecordingProvider:
    """Wrap a provider so every completion lands in the attempt's cost record.

    The agents return strings, so the only place a call's token counts and
    latency remain visible is the :class:`CompletionResult` they discard.
    Wrapping is what makes ``model_calls`` a measurement of the run rather than
    an estimate reconstructed afterwards.

    The wrapper delegates rather than reimplements: the inner provider still
    runs its own classification gate on itself, so wrapping cannot widen what a
    provider is approved to receive.
    """

    def __init__(
        self,
        inner: LLMProvider,
        *,
        role: str,
        sink: list[dict[str, Any]],
        spec: ModelSpec | None = None,
    ) -> None:
        self._inner = inner
        self._role = role
        self._sink = sink
        self._spec = spec

    @property
    def approved_classifications(self) -> frozenset[DataClassification]:
        return self._inner.approved_classifications

    @property
    def model_family(self) -> str:
        return self._inner.model_family

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        classification: DataClassification,
        temperature: float,
        max_tokens: int,
        seed: int | None = None,
    ) -> CompletionResult:
        started = time.perf_counter()
        result = self._inner.complete(
            messages,
            classification=classification,
            temperature=temperature,
            max_tokens=max_tokens,
            seed=seed,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        metadata = result.metadata
        prompt_tokens = int(getattr(metadata, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(metadata, "completion_tokens", 0) or 0)
        # Prefer the provider's own latency; fall back to the wall time just
        # measured so a provider that reports none still costs something.
        latency_ms = int(getattr(metadata, "latency_ms", 0) or 0) or elapsed_ms
        self._sink.append(
            {
                "role": self._role,
                "model_id": getattr(metadata, "model_id", "unknown"),
                "model_digest": getattr(metadata, "model_digest", "unknown"),
                "model_family": getattr(metadata, "model_family", "unknown"),
                "classification": getattr(
                    getattr(metadata, "classification", None), "value", None
                ),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "latency_ms": latency_ms,
                "estimated_cost": (
                    self._spec.estimated_cost(prompt_tokens, completion_tokens)
                    if self._spec is not None
                    else 0.0
                ),
            }
        )
        return result


# ---------------------------------------------------------------------------
# Case inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendCase:
    """The inputs one architecture needs, read out of the opaque case mapping.

    ``note_text`` is the raw clinical note and is required only by
    ``direct_llm``, which exists precisely to show what happens without a
    structured reference. Every other variant needs ``reference``.
    """

    case_id: str
    note_id: str
    note_text: str | None
    reference: StructuredClinicalReference | None

    @classmethod
    def from_request(cls, request: VariantRequest) -> BackendCase:
        case = request.case
        raw_reference = case.get("reference")
        reference: StructuredClinicalReference | None = None
        if isinstance(raw_reference, StructuredClinicalReference):
            reference = raw_reference
        elif isinstance(raw_reference, Mapping):
            reference = StructuredClinicalReference.model_validate(dict(raw_reference))
        note_text = case.get("note_text")
        return cls(
            case_id=request.case_id,
            note_id=str(case.get("note_id") or "source-note"),
            note_text=str(note_text) if note_text is not None else None,
            reference=reference,
        )

    def require_note(self, variant: VariantName) -> str:
        if not self.note_text:
            raise CaseInputError(
                f"{variant.value} reads the unstructured note, but case "
                f"{self.case_id!r} carries no 'note_text'"
            )
        return self.note_text

    def require_reference(self, variant: VariantName) -> StructuredClinicalReference:
        if self.reference is None:
            raise CaseInputError(
                f"{variant.value} needs a structured reference, but case "
                f"{self.case_id!r} carries no 'reference'. References are "
                "extracted upstream, not per attempt."
            )
        return self.reference


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    """Per-attempt state: the providers in use and the calls they have made."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    generator: Any = None
    judge: Any = None
    claim_extractor: Any = None


class MedDialBackend:
    """Execute the five architectures against injected providers.

    Providers are injected, never constructed here, so one run records one
    model configuration (GOV-4) and a test can drive every architecture with
    :class:`~meddial.llm.MockProvider`.
    """

    def __init__(
        self,
        *,
        generator: LLMProvider,
        judge: LLMProvider,
        claim_extractor: LLMProvider | None = None,
        policy_registry: PolicyRegistry | None = None,
        structural_config: StructuralConfig | None = None,
        generator_temperature: float = 0.6,
        dialogue_max_tokens: int = 2048,
    ) -> None:
        self._generator = generator
        self._judge = judge
        # Implementation Plan A.2 gives claim extraction its own model: it is
        # the highest-volume path in the evaluator and it wants JSON reliability
        # rather than world knowledge, which is a different thing to choose for
        # than judging. It defaults to the judge so an existing caller is
        # unaffected, but pointing it at a smaller, faster model is the intent.
        self._claim_extractor = claim_extractor or judge
        self._policies = policy_registry or PolicyRegistry()
        self._structural_config = structural_config
        self._generator_temperature = generator_temperature
        self._dialogue_max_tokens = dialogue_max_tokens

    # -- five architectures ------------------------------------------------

    def direct_llm(self, request: VariantRequest) -> Mapping[str, Any]:
        """One unstructured prompt, one completion, no reference and no policy."""
        case = BackendCase.from_request(request)
        session = self._session(request, VariantName.DIRECT_LLM)
        note = case.require_note(VariantName.DIRECT_LLM)
        dialogue = self._single_completion_dialogue(
            request,
            session,
            instruction=(
                "Write a realistic outpatient consultation between a Doctor and "
                "a Patient that is consistent with the clinical note below."
            ),
            body=note,
        )
        return self._finish(request, session, case, dialogue, policy_applied=False)

    def structured_single_agent(self, request: VariantRequest) -> Mapping[str, Any]:
        """One agent writes both speakers from the structured reference."""
        case = BackendCase.from_request(request)
        session = self._session(request, VariantName.STRUCTURED_SINGLE_AGENT)
        reference = case.require_reference(VariantName.STRUCTURED_SINGLE_AGENT)
        dialogue = self._single_completion_dialogue(
            request,
            session,
            instruction=(
                "Write a realistic outpatient consultation between a Doctor and "
                "a Patient that is consistent with the structured clinical "
                "reference below. Use only facts the reference contains."
            ),
            body=json.dumps(
                reference.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        )
        return self._finish(request, session, case, dialogue, policy_applied=False)

    def basic_multi_agent(self, request: VariantRequest) -> Mapping[str, Any]:
        """Two agents alternate, but the patient is not policy-restricted."""
        return self._agent_dialogue(request, VariantName.BASIC_MULTI_AGENT)

    def knowledge_controlled(self, request: VariantRequest) -> Mapping[str, Any]:
        """Two agents alternate over a policy-masked patient view."""
        return self._agent_dialogue(request, VariantName.KNOWLEDGE_CONTROLLED)

    def full_meddial(self, request: VariantRequest) -> Mapping[str, Any]:
        """Knowledge-controlled generation plus the runner's targeted repair."""
        return self._agent_dialogue(request, VariantName.FULL_MEDDIAL)

    # -- shared machinery --------------------------------------------------

    def _session(self, request: VariantRequest, variant: VariantName) -> _Session:
        config = _require_config(request)
        # The stage list is a claim about what the architecture does. A variant
        # that never applies a policy must not be handed one that withholds
        # anything, or its record would name a treatment it did not receive.
        if (
            variant not in _POLICY_STAGE_VARIANTS
            and config.patient_policy_id != FULL_DISCLOSURE_POLICY_ID
        ):
            raise PolicyStageError(
                f"{variant.value} declares no knowledge_policy stage, so it "
                f"cannot run under patient policy {config.patient_policy_ref!r}; "
                f"use {FULL_DISCLOSURE_POLICY_ID!r} or choose a variant that "
                "applies a policy"
            )
        session = _Session()
        session.calls = []
        session.generator = RecordingProvider(
            self._generator,
            role="generator",
            sink=session.calls,
            spec=_spec_for(config, "patient", "generator", "doctor"),
        )
        session.judge = RecordingProvider(
            self._judge,
            role="judge",
            sink=session.calls,
            spec=_spec_for(config, "judge"),
        )
        # Its own role, so the attempt record shows which model read the
        # transcript and which model judged it, even when they are the same one.
        session.claim_extractor = RecordingProvider(
            self._claim_extractor,
            role="claim_extractor",
            sink=session.calls,
            spec=_spec_for(config, "extractor", "judge"),
        )
        return session

    def _single_completion_dialogue(
        self,
        request: VariantRequest,
        session: _Session,
        *,
        instruction: str,
        body: str,
    ) -> list[dict[str, str]]:
        config = _require_config(request)
        prompt = (
            f"{instruction}\n\n"
            f"Produce at most {config.max_turns} turns, beginning with the Doctor "
            "and alternating.\n"
            'Respond with only a JSON object of the form {"dialogue": [{"role": '
            '"Doctor", "text": "..."}]}.\n\n'
            f"{body}"
        )
        repair_note = _repair_instruction(request)
        if repair_note:
            prompt = f"{prompt}\n\n{repair_note}"
        result = session.generator.complete(
            [ChatMessage(role="user", content=prompt)],
            # Note and reference are both MIMIC-derived, so this call carries
            # the strictest classification the provider gate knows.
            classification=DataClassification.RESTRICTED_CLINICAL,
            temperature=self._generator_temperature,
            max_tokens=self._dialogue_max_tokens,
            seed=request.seed,
        )
        return _parse_dialogue(result.text)

    def _agent_dialogue(
        self, request: VariantRequest, variant: VariantName
    ) -> Mapping[str, Any]:
        # Imported here: Agents/ is the legacy top-level package and importing
        # it at module scope would make meddial.experiments depend on it just
        # to read a config.
        from Agents.DoctorAgent import DoctorAgent
        from Agents.PatientAgent import PatientAgent

        config = _require_config(request)
        case = BackendCase.from_request(request)
        session = self._session(request, variant)
        reference = case.require_reference(variant)
        policy = self._policies.load(
            config.patient_policy_id, config.patient_policy_version
        )
        contexts = build_contexts(
            reference, policy, guidance_id=config.doctor_guidance_id
        )

        doctor = DoctorAgent(
            session.generator,
            doctor_context=contexts.doctor,
            guidance_id=config.doctor_guidance_id,
            seed=request.seed,
        )
        patient = PatientAgent(
            to_legacy_profile(contexts.patient),
            session.generator,
            seed=request.seed,
        )
        _apply_repair(request, doctor=doctor, patient=patient)

        history: list[dict[str, str]] = []
        for turn in range(config.max_turns):
            speaker = doctor if turn % 2 == 0 else patient
            role = DOCTOR_ROLE if turn % 2 == 0 else PATIENT_ROLE
            text = speaker.respond(list(history))
            if not str(text).strip():
                # An empty turn is a structural violation the evaluator must
                # see, not something to retry away here.
                break
            history.append({"role": role, "content": str(text)})

        dialogue = [{"role": t["role"], "text": t["content"]} for t in history]
        return self._finish(
            request,
            session,
            case,
            dialogue,
            policy_applied=variant in _POLICY_STAGE_VARIANTS,
        )

    def _finish(
        self,
        request: VariantRequest,
        session: _Session,
        case: BackendCase,
        dialogue: Sequence[Mapping[str, str]],
        *,
        policy_applied: bool,
    ) -> Mapping[str, Any]:
        evaluation = self._evaluate(request, session, case, dialogue)
        overall = evaluation["acceptance"].get("overall")
        return {
            "dialogue": list(dialogue),
            "evaluation": evaluation,
            "accepted": str(getattr(overall, "value", overall)).upper() == "ACCEPT",
            "model_calls": list(session.calls),
            "stages": {"policy_applied": policy_applied},
        }

    def _evaluate(
        self,
        request: VariantRequest,
        session: _Session,
        case: BackendCase,
        dialogue: Sequence[Mapping[str, str]],
    ) -> dict[str, Any]:
        """Score all five dimensions identically for every architecture."""
        config = _require_config(request)
        turns = build_turns(dialogue)
        scores: dict[str, Score] = {}
        leakage: list[Any] = []

        # score_structural_validity already carries the config, failed checks
        # and violations in the Score's detail, so the report is not repeated.
        structural, _ = score_structural_validity(turns, config=self._structural_config)
        scores[STRUCTURAL_VALIDITY] = structural

        scores[NATURALNESS] = score_naturalness(
            turns,
            provider=session.judge,
            threshold=config.thresholds.get(NATURALNESS),
            seed=request.seed,
        )

        # Faithfulness and boundary need the reference. A direct_llm case may
        # carry one for scoring even though the architecture never saw it;
        # without one those dimensions stay unmeasured rather than invented.
        if case.reference is not None:
            policy = self._policies.load(
                config.patient_policy_id, config.patient_policy_version
            )
            contexts = build_contexts(
                case.reference, policy, guidance_id=config.doctor_guidance_id
            )
            # EVAL-4: an evaluator that cannot parse its own judge output
            # yields INCOMPLETE for the dimensions it feeds, never a stand-in
            # number. ProviderError is deliberately not caught here -- a model
            # that failed to answer is a run failure, not an empty cell.
            try:
                claims = extract_claims(
                    turns, provider=session.claim_extractor, seed=request.seed
                )
            except ClaimExtractionError as exc:
                claims = None
                for dimension, scope in (
                    (PATIENT_FACTUALITY, TurnScope.PATIENT),
                    (DOCTOR_FACTUALITY, TurnScope.DOCTOR),
                ):
                    scores[dimension] = _incomplete(
                        scorer_id="faithfulness",
                        reference_mode=config.reference_mode,
                        turn_scope=scope,
                        reason=f"claim_extraction_failed: {exc}",
                    )

            if claims is not None:
                for role, dimension, scope in (
                    (PATIENT_ROLE, PATIENT_FACTUALITY, TurnScope.PATIENT),
                    (DOCTOR_ROLE, DOCTOR_FACTUALITY, TurnScope.DOCTOR),
                ):
                    try:
                        scores[dimension] = score_faithfulness(
                            claims.factual_for_role(role),
                            contexts.evaluator,
                            role=role,
                            reference_mode=config.reference_mode,
                            provider=session.judge,
                            threshold=config.thresholds.get(dimension),
                            seed=request.seed,
                        )
                    except VerificationError as exc:
                        scores[dimension] = _incomplete(
                            scorer_id="faithfulness",
                            reference_mode=config.reference_mode,
                            turn_scope=scope,
                            reason=f"verification_failed: {exc}",
                        )

            try:
                boundary, leakage = score_knowledge_boundary(
                    turns,
                    policy,
                    role=PATIENT_ROLE,
                    provider=session.judge,
                    seed=request.seed,
                )
            except BoundaryError as exc:
                boundary = _incomplete(
                    scorer_id="knowledge_boundary",
                    reference_mode=config.reference_mode,
                    turn_scope=TurnScope.PATIENT,
                    reason=f"boundary_check_failed: {exc}",
                )
            scores[KNOWLEDGE_BOUNDARY] = boundary

        acceptance = decide(scores, thresholds=config.thresholds)
        record: dict[str, Any] = {
            "scores": {name: score.as_record() for name, score in scores.items()},
            "acceptance": acceptance.as_record(),
            "reference_mode": config.reference_mode.value,
        }
        if leakage:
            record["leakage"] = [event.as_record() for event in leakage]
        return record


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _incomplete(
    *,
    scorer_id: str,
    reference_mode: ReferenceMode,
    turn_scope: TurnScope,
    reason: str,
) -> Score:
    """An explicitly empty cell that carries why it is empty (EVAL-4)."""
    return Score.incomplete(
        ScoreProvenance.unmeasured(
            scorer_id=scorer_id,
            reference_mode=reference_mode,
            turn_scope=turn_scope,
            prompt_version="none",
            reason=reason,
        )
    )


def _require_config(request: VariantRequest) -> RunConfig:
    config = request.config
    if not isinstance(config, RunConfig):
        raise BackendError(
            f"request.config must be a RunConfig, got {type(config).__name__}"
        )
    return config


def _spec_for(config: RunConfig, *roles: str) -> ModelSpec | None:
    """First declared spec among ``roles``; cost is 0.0 when none is declared."""
    for role in roles:
        spec = config.models.get(role)
        if spec is not None:
            return spec
    return None


def _parse_dialogue(text: str) -> list[dict[str, str]]:
    try:
        payload = parse_json_object(text)
    except Exception as exc:
        raise DialogueFormatError(
            f"single-completion variant returned no parseable JSON object: {exc}"
        ) from exc
    raw_turns = payload.get("dialogue")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise DialogueFormatError(
            "single-completion variant returned no 'dialogue' array"
        )
    dialogue: list[dict[str, str]] = []
    for index, raw in enumerate(raw_turns):
        if not isinstance(raw, Mapping):
            raise DialogueFormatError(f"dialogue turn {index} is not an object")
        role = str(raw.get("role", "")).strip()
        if not role:
            raise DialogueFormatError(f"dialogue turn {index} has no role")
        dialogue.append(
            {"role": role, "text": str(raw.get("text", raw.get("content", "")))}
        )
    return dialogue


def _repair_instruction(request: VariantRequest) -> str:
    """Render only the directives for the dimensions that actually failed."""
    if not request.repair:
        return ""
    directives = [
        f"- ({action.get('dimension')}) {action.get('directive')}"
        for action in request.repair.get("actions") or []
        if isinstance(action, Mapping)
    ]
    if not directives:
        return ""
    return "Revise the previous attempt as follows:\n" + "\n".join(directives)


def _apply_repair(request: VariantRequest, *, doctor: Any, patient: Any) -> None:
    """Route each repair action to the agent its targets name, and no other.

    ``orchestration`` actions are deliberately not routed to an agent: they
    describe turn order, empty turns and turn bounds, which the loop owns.
    """
    if not request.repair:
        return
    doctor_notes: list[str] = []
    patient_notes: list[str] = []
    for action in request.repair.get("actions") or []:
        if not isinstance(action, Mapping):
            continue
        directive = str(action.get("directive", "")).strip()
        if not directive:
            continue
        targets = {str(target) for target in action.get("targets", ())}
        if targets & _PATIENT_REPAIR_TARGETS:
            patient_notes.append(directive)
        if targets & _DOCTOR_REPAIR_TARGETS:
            doctor_notes.append(directive)
    if doctor_notes:
        doctor.update_prompt("\n".join(doctor_notes))
    if patient_notes:
        patient.update_prompt("\n".join(patient_notes))


__all__ = [
    "FULL_DISCLOSURE_POLICY_ID",
    "BackendCase",
    "BackendError",
    "CaseInputError",
    "DialogueFormatError",
    "MedDialBackend",
    "PolicyStageError",
    "RecordingProvider",
]
