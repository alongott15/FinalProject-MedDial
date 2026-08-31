"""EXP-5 at the composition layer: five architectures actually execute.

test_experiment_harness.py covers the harness -- config hashing, resume,
immutable attempts, pure aggregation -- against a fake backend, so it cannot
show that anything generates a dialogue. These tests drive the real
:class:`MedDialBackend` through the real registry and assert the properties
that distinguish five architectures from five labels over one path.

Every case here is synthetic and every provider is a MockProvider.
"""

from __future__ import annotations

import pytest

from meddial.evaluation import ReferenceMode
from meddial.experiments import (
    ExperimentRunner,
    ModelSpec,
    RunConfig,
    VariantName,
    default_variant_registry,
)
from meddial.experiments.backend import (
    BackendCase,
    CaseInputError,
    DialogueFormatError,
    MedDialBackend,
    PolicyStageError,
    RecordingProvider,
    _apply_repair,
)
from meddial.experiments.repair import build_repair_plan
from meddial.experiments.variants import VariantRequest
from meddial.knowledge import (
    Core,
    Demographics,
    Diagnosis,
    Medication,
    StructuredClinicalReference,
    Symptom,
)
from meddial.llm import DataClassification, MockProvider

DIALOGUE_JSON = (
    '{"dialogue": [{"role": "Doctor", "text": "What brings you in today?"}, '
    '{"role": "Patient", "text": "I have been short of breath for three days."}]}'
)

_POLICY_STAGE = {"knowledge_controlled", "full_meddial"}


def _reference() -> StructuredClinicalReference:
    """A synthetic case. Deliberately not derived from any real record."""
    return StructuredClinicalReference(
        subject_id=1,
        hadm_id=1,
        core=Core(
            symptoms=[Symptom(description="Shortness of breath", duration="three days")],
            diagnoses=[Diagnosis(primary="Congestive Heart Failure")],
        ),
        context={
            "Patient_Demographics": Demographics(age="64", sex="F"),
            "Current_Medications": [Medication(name="Metoprolol", purpose="rate control")],
        },
    )


def _models() -> dict[str, ModelSpec]:
    digest = "sha256:" + "a" * 64
    return {
        "patient": ModelSpec("patient-model", digest, "mistral", "Q4_K_M"),
        "doctor": ModelSpec("doctor-model", digest, "gemma", "Q4_K_M"),
        "judge": ModelSpec(
            "judge-model",
            digest,
            "qwen",
            "Q4_K_M",
            prompt_cost_per_million=1.0,
            completion_cost_per_million=2.0,
        ),
    }


def _config(**changes) -> RunConfig:
    values = {
        "name": "backend-test",
        "variant": "full_meddial",
        "patient_policy_id": "NO_DIAGNOSIS",
        "patient_policy_version": "2.0",
        "doctor_guidance_id": "NO_DIAGNOSIS",
        "reference_mode": ReferenceMode.FULL_REFERENCE,
        "seed": 7,
        "max_turns": 2,
        "max_attempts": 1,
        "thresholds": {"naturalness": 0.6, "patient_factuality": 0.8},
        "models": _models(),
        "prompt_versions": {"patient": "sha256:patient", "doctor": "sha256:doctor"},
    }
    values.update(changes)
    return RunConfig(**values)


def _backend(generator: MockProvider | None = None, judge: MockProvider | None = None):
    return MedDialBackend(
        generator=generator or MockProvider([DIALOGUE_JSON] * 8),
        judge=judge or MockProvider(),
    )


def _request(config: RunConfig, **changes) -> VariantRequest:
    values = {
        "case_id": "case-1",
        "case": {
            "case_id": "case-1",
            "note_text": "Patient reports dyspnoea and ankle oedema.",
            "reference": _reference().model_dump(mode="json"),
        },
        "config": config,
        "attempt_index": 1,
        "seed": 11,
    }
    values.update(changes)
    return VariantRequest(**values)


# -- the runner's contract ---------------------------------------------------


@pytest.mark.parametrize("variant", list(VariantName))
def test_every_variant_executes_and_returns_the_runner_contract(variant) -> None:
    """All five dispatch to real code and return what AttemptRecord needs."""
    policy = "NO_DIAGNOSIS" if variant.value in _POLICY_STAGE else "FULL"
    config = _config(variant=variant.value, patient_policy_id=policy)
    implementation = default_variant_registry().resolve(variant)

    output = implementation.execute(_backend(), _request(config))

    assert set(output) >= {"dialogue", "evaluation", "accepted", "model_calls"}
    assert output["dialogue"], "every architecture must produce turns"
    assert {"scores", "acceptance"} <= set(output["evaluation"])
    assert output["model_calls"], "every architecture must record what it spent"


def test_the_five_architectures_do_not_share_one_execution_path() -> None:
    """A single-completion variant issues one generator call; agents issue many."""
    direct_generator = MockProvider([DIALOGUE_JSON])
    agent_generator = MockProvider([DIALOGUE_JSON] * 8)
    registry = default_variant_registry()

    registry.resolve(VariantName.DIRECT_LLM).execute(
        _backend(generator=direct_generator),
        _request(_config(variant="direct_llm", patient_policy_id="FULL")),
    )
    registry.resolve(VariantName.BASIC_MULTI_AGENT).execute(
        _backend(generator=agent_generator),
        _request(_config(variant="basic_multi_agent", patient_policy_id="FULL")),
    )

    assert len(direct_generator.calls) == 1
    assert len(agent_generator.calls) == 2  # one doctor turn, one patient turn


# -- architectural boundaries ------------------------------------------------


def test_direct_llm_is_given_the_note_and_never_the_structured_reference() -> None:
    """The lower bound is only a lower bound if it lacks the reference."""
    generator = MockProvider([DIALOGUE_JSON])
    config = _config(variant="direct_llm", patient_policy_id="FULL")

    default_variant_registry().resolve(VariantName.DIRECT_LLM).execute(
        _backend(generator=generator), _request(config)
    )

    prompt = "\n".join(m.content for m in generator.calls[0].messages)
    assert "dyspnoea" in prompt, "direct_llm reads the unstructured note"
    assert "Congestive Heart Failure" not in prompt
    assert "Metoprolol" not in prompt


def test_a_variant_without_a_policy_stage_refuses_a_restrictive_policy() -> None:
    """Recording a treatment the architecture never applied would be a lie."""
    config = _config(variant="basic_multi_agent", patient_policy_id="NO_DIAGNOSIS")

    with pytest.raises(PolicyStageError, match="no knowledge_policy stage"):
        default_variant_registry().resolve(VariantName.BASIC_MULTI_AGENT).execute(
            _backend(), _request(config)
        )


def test_structured_variants_refuse_a_case_with_no_reference() -> None:
    """References are extracted upstream; a missing one is not improvised."""
    config = _config(variant="structured_single_agent", patient_policy_id="FULL")
    request = _request(config, case={"case_id": "case-1", "note_text": "text only"})

    with pytest.raises(CaseInputError, match="no 'reference'"):
        default_variant_registry().resolve(
            VariantName.STRUCTURED_SINGLE_AGENT
        ).execute(_backend(), request)


def test_direct_llm_refuses_a_case_with_no_note() -> None:
    config = _config(variant="direct_llm", patient_policy_id="FULL")
    request = _request(
        config,
        case={"case_id": "case-1", "reference": _reference().model_dump(mode="json")},
    )

    with pytest.raises(CaseInputError, match="no 'note_text'"):
        default_variant_registry().resolve(VariantName.DIRECT_LLM).execute(
            _backend(), request
        )


def test_an_unparseable_completion_raises_instead_of_inventing_a_dialogue() -> None:
    """D-08 at the composition layer: no stand-in transcript."""
    config = _config(variant="direct_llm", patient_policy_id="FULL")
    generator = MockProvider(["I'm sorry, I can't help with that."])

    with pytest.raises(DialogueFormatError):
        default_variant_registry().resolve(VariantName.DIRECT_LLM).execute(
            _backend(generator=generator), _request(config)
        )


# -- cost accounting ---------------------------------------------------------


def test_model_calls_record_tokens_latency_and_priced_cost() -> None:
    sink: list[dict] = []
    spec = _models()["judge"]
    provider = RecordingProvider(
        MockProvider(["ok"]), role="judge", sink=sink, spec=spec
    )

    provider.complete(
        [],
        classification=DataClassification.SYNTHETIC,
        temperature=0.0,
        max_tokens=16,
    )

    assert len(sink) == 1
    call = sink[0]
    assert call["role"] == "judge"
    assert call["latency_ms"] >= 0
    assert call["estimated_cost"] == spec.estimated_cost(
        call["prompt_tokens"], call["completion_tokens"]
    )


def test_recording_does_not_widen_what_a_provider_accepts() -> None:
    """The wrapper delegates the gate; it must not become a way around it."""
    inner = MockProvider(["ok"], approved=frozenset({DataClassification.SYNTHETIC}))
    wrapped = RecordingProvider(inner, role="generator", sink=[])

    assert wrapped.approved_classifications == inner.approved_classifications
    assert wrapped.model_family == inner.model_family


# -- targeted repair ---------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.notes: list[str] = []

    def update_prompt(self, text: str) -> None:
        self.notes.append(text)


def test_repair_reaches_only_the_agent_its_targets_name() -> None:
    """EXP-3: one failed dimension must not rewrite every prompt."""
    doctor, patient = _Recorder(), _Recorder()
    plan = build_repair_plan(("patient_factuality",))

    _apply_repair(
        _request(_config(), repair=plan.as_record()), doctor=doctor, patient=patient
    )

    assert patient.notes, "patient_factuality targets the patient prompt"
    assert doctor.notes == [], "the doctor prompt is not a target of this dimension"


def test_structural_repair_is_not_routed_to_any_agent_prompt() -> None:
    """``orchestration`` is the loop's own concern, not a prompt edit."""
    doctor, patient = _Recorder(), _Recorder()
    plan = build_repair_plan(("structural_validity",))

    _apply_repair(
        _request(_config(), repair=plan.as_record()), doctor=doctor, patient=patient
    )

    assert doctor.notes == []
    assert patient.notes == []


# -- integration with the harness -------------------------------------------


def test_the_backend_drives_a_real_run_end_to_end(tmp_path) -> None:
    """The harness and the composition layer fit together without a fake."""
    config = _config(variant="direct_llm", patient_policy_id="FULL")
    runner = ExperimentRunner(tmp_path, git_commit="abc123")

    result = runner.run(
        config,
        [
            {
                "case_id": "case-1",
                "note_text": "Patient reports dyspnoea.",
                "reference": _reference().model_dump(mode="json"),
            }
        ],
        _backend(),
    )

    assert len(result.attempts) == 1
    record = result.attempts[0].as_record()
    assert record["case_id"] == "case-1"
    assert record["dialogue"], "the run persisted a real generated dialogue"
    assert record["cost"]["calls"] >= 1


def test_backend_case_reads_a_reference_given_as_a_model_or_a_mapping() -> None:
    reference = _reference()
    as_model = BackendCase.from_request(
        _request(_config(), case={"case_id": "c", "reference": reference})
    )
    as_mapping = BackendCase.from_request(
        _request(
            _config(),
            case={"case_id": "c", "reference": reference.model_dump(mode="json")},
        )
    )

    assert as_model.reference == as_mapping.reference
