"""W7: versioned runs, distinct variants, immutable attempts, and pure aggregation.

Every case and completion in this file is synthetic.  The tests deliberately
exercise the failure modes from EXP-1--EXP-8: configuration drift on resume,
mutable attempt records, architecture aliases, broad prompt repair, and an
aggregation pass that accidentally calls a model.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from meddial.evaluation import ReferenceMode
from meddial.experiments import (
    AttemptLog,
    AttemptRecord,
    ExperimentRunner,
    ImmutableAttemptError,
    ModelSpec,
    ResumeHashMismatch,
    RunConfig,
    RunManager,
    UnimplementedVariantError,
    VariantAliasError,
    VariantName,
    VariantRegistry,
    aggregate_attempts,
    build_exp8_control_configs,
    build_repair_plan,
    default_variant_registry,
    derive_attempt_seed,
    load_run_config,
)
from meddial.knowledge import DeprecatedPolicyError, PolicyRegistry
from meddial.llm import MockProvider


PINNED = "sha256:" + "a" * 64


def _models(*, pinned: bool = True) -> dict[str, ModelSpec]:
    digest = PINNED if pinned else "UNPINNED:development"
    return {
        "patient": ModelSpec("patient-model", digest, "mistral", "Q4_K_M"),
        "doctor": ModelSpec("doctor-model", digest, "gemma", "Q4_K_M"),
        "judge": ModelSpec("judge-model", digest, "qwen", "Q4_K_M"),
    }


def _config(**changes) -> RunConfig:
    values = {
        "name": "w7-test",
        "variant": "full_meddial",
        "patient_policy_id": "NO_DIAGNOSIS",
        "patient_policy_version": "2.0",
        "doctor_guidance_id": "NEUTRAL",
        "reference_mode": ReferenceMode.FULL_REFERENCE,
        "seed": 20260914,
        "max_turns": 12,
        "max_attempts": 2,
        "thresholds": {"naturalness": 0.6, "patient_factuality": 0.8},
        "models": _models(),
        "prompt_versions": {"patient": "sha256:patient", "doctor": "sha256:doctor"},
        "frozen_at": "2026-09-12T17:40:00Z",
        "input_manifest_hash": "sha256:" + "b" * 64,
        "batch_config": {"max_batch_size": 8},
    }
    values.update(changes)
    return RunConfig(**values)


def test_config_hash_is_stable_across_mapping_order_and_json_roundtrip(tmp_path: Path) -> None:
    first = _config()
    second = _config(
        thresholds={"patient_factuality": 0.8, "naturalness": 0.6},
        prompt_versions={"doctor": "sha256:doctor", "patient": "sha256:patient"},
        models=dict(reversed(list(_models().items()))),
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(first.as_record(), indent=2), encoding="utf-8")

    assert first.config_hash() == second.config_hash()
    assert load_run_config(path).config_hash() == first.config_hash()
    assert first.prompt_set_hash() == second.prompt_set_hash()


def test_threshold_or_prompt_change_changes_the_run_identity(tmp_path: Path) -> None:
    baseline = _config()
    threshold_change = replace(
        baseline, thresholds={**baseline.thresholds, "naturalness": 0.61}
    )
    prompt_change = replace(
        baseline, prompt_versions={**baseline.prompt_versions, "doctor": "sha256:new"}
    )
    manager = RunManager(tmp_path, git_commit="abc123")

    base_run = manager.resolve(baseline)
    threshold_run = manager.resolve(threshold_change)
    prompt_run = manager.resolve(prompt_change)

    assert len({base_run.run_id, threshold_run.run_id, prompt_run.run_id}) == 3
    assert baseline.config_hash() != threshold_change.config_hash()
    assert baseline.prompt_set_hash() != prompt_change.prompt_set_hash()


def test_confirmatory_validation_requires_freeze_and_pinned_inputs() -> None:
    with pytest.raises(ValueError, match="frozen_at"):
        replace(_config(), frozen_at=None).validate(confirmatory=True)
    with pytest.raises(ValueError, match="model digest"):
        replace(_config(), models=_models(pinned=False)).validate(confirmatory=True)
    with pytest.raises(ValueError, match="input_manifest_hash"):
        replace(_config(), input_manifest_hash="UNPINNED:development").validate(
            confirmatory=True
        )


def test_deprecated_policy_is_allowed_for_e0_but_refused_for_confirmation() -> None:
    config = replace(
        _config(), patient_policy_id="NO_DIAGNOSIS", patient_policy_version="1.0"
    )
    registry = PolicyRegistry()

    assert config.validate(policy_registry=registry, confirmatory=False).deprecated
    with pytest.raises(DeprecatedPolicyError):
        config.validate(policy_registry=registry, confirmatory=True)


def test_exp8_helpers_pin_exactly_one_factor() -> None:
    controls = build_exp8_control_configs(
        _config(),
        patient_policies=("FULL@2.0", "NO_DIAGNOSIS@2.0"),
        doctor_guidance_ids=("NEUTRAL", "FULL"),
        pinned_doctor_guidance_id="NEUTRAL",
        pinned_patient_policy="NO_DIAGNOSIS@2.0",
    )

    assert {c.patient_policy_ref for c in controls.patient_policy_varied} == {
        "FULL@2.0",
        "NO_DIAGNOSIS@2.0",
    }
    assert {c.doctor_guidance_id for c in controls.patient_policy_varied} == {"NEUTRAL"}
    assert {c.patient_policy_ref for c in controls.doctor_guidance_varied} == {
        "NO_DIAGNOSIS@2.0"
    }
    assert {c.doctor_guidance_id for c in controls.doctor_guidance_varied} == {
        "NEUTRAL",
        "FULL",
    }


def test_default_registry_has_five_distinct_executable_implementations() -> None:
    registry = default_variant_registry()

    assert set(registry.names) == {variant.value for variant in VariantName}
    implementations = [registry.resolve(variant) for variant in VariantName]
    assert len({type(item) for item in implementations}) == 5
    assert len({item.implementation_id for item in implementations}) == 5
    assert len({item.fingerprint for item in implementations}) == 5


def test_registry_refuses_an_alias() -> None:
    registry = VariantRegistry()
    direct = default_variant_registry().resolve(VariantName.DIRECT_LLM)
    registry.register(direct)

    class Alias(type(direct)):
        variant = VariantName.STRUCTURED_SINGLE_AGENT
        implementation_id = direct.implementation_id

    with pytest.raises(VariantAliasError):
        registry.register(Alias())


def test_runner_refuses_unimplemented_variant_before_writing(tmp_path: Path) -> None:
    empty = VariantRegistry()
    runner = ExperimentRunner(tmp_path, variants=empty, git_commit="abc123")

    with pytest.raises(UnimplementedVariantError):
        runner.run(_config(), [{"case_id": "case-1", "value": "synthetic"}], object())
    assert not (tmp_path / "runs").exists()


def test_full_hash_resume_mismatch_raises(tmp_path: Path) -> None:
    manager = RunManager(tmp_path, git_commit="abc123")
    context = manager.resolve(_config(), requested_run_id="fixed")
    assert context.manifest_path.exists()

    changed = replace(_config(), input_manifest_hash="sha256:" + "c" * 64)
    with pytest.raises(ResumeHashMismatch, match="input_manifest_hash"):
        manager.resolve(changed, requested_run_id="fixed")


def test_attempt_log_is_append_only_and_detects_duplicate_records(tmp_path: Path) -> None:
    context = RunManager(tmp_path, git_commit="abc123").resolve(_config())
    log = AttemptLog(context)
    record = AttemptRecord.synthetic_for_test(context, case_id="case-1")
    log.append(record)
    original = context.attempts_path.read_bytes()

    with pytest.raises(ImmutableAttemptError):
        log.append(record)

    assert context.attempts_path.read_bytes() == original
    assert log.read()[0].record_hash == record.record_hash


def test_attempt_seeds_are_stable_and_separated_by_case_and_attempt() -> None:
    first = derive_attempt_seed(7, "case-1", "FULL@2.0", 1)
    assert first == derive_attempt_seed(7, "case-1", "FULL@2.0", 1)
    assert first != derive_attempt_seed(7, "case-2", "FULL@2.0", 1)
    assert first != derive_attempt_seed(7, "case-1", "FULL@2.0", 2)


class _Backend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def _result(self, request):
        self.calls.append((request.case_id, request.seed))
        return {
            "dialogue": [
                {"role": "Doctor", "text": "How are you feeling?"},
                {"role": "Patient", "text": "Short of breath."},
            ],
            "evaluation": {
                "scores": {
                    "naturalness": {"value": 0.9, "status": "pass"},
                    "patient_factuality": {"value": 0.9, "status": "pass"},
                },
                "acceptance": {"overall": "ACCEPT", "per_dimension": {}},
            },
            "accepted": True,
            "model_calls": [
                {
                    "model_id": "patient-model",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "latency_ms": 2,
                    "estimated_cost": 0.01,
                }
            ],
        }

    direct_llm = _result
    structured_single_agent = _result
    basic_multi_agent = _result
    knowledge_controlled = _result
    full_meddial = _result


def test_same_config_and_seed_produce_identical_attempt_records(tmp_path: Path) -> None:
    cases = [{"case_id": "case-2", "value": 2}, {"case_id": "case-1", "value": 1}]
    first_backend = _Backend()
    second_backend = _Backend()

    first = ExperimentRunner(
        tmp_path / "first", git_commit="abc123"
    ).run(_config(), cases, first_backend)
    second = ExperimentRunner(
        tmp_path / "second", git_commit="abc123"
    ).run(_config(), reversed(cases), second_backend)

    assert [record.as_record() for record in first.attempts] == [
        record.as_record() for record in second.attempts
    ]
    assert first_backend.calls == second_backend.calls


def test_resume_does_not_repeat_completed_provider_work(tmp_path: Path) -> None:
    backend = _Backend()
    runner = ExperimentRunner(tmp_path, git_commit="abc123")
    first = runner.run(_config(), [{"case_id": "case-1"}], backend)
    calls_after_first = list(backend.calls)
    second = runner.run(_config(), [{"case_id": "case-1"}], backend)

    assert backend.calls == calls_after_first
    assert [r.as_record() for r in first.attempts] == [r.as_record() for r in second.attempts]


def test_repair_is_dimension_keyed_and_never_rewrites_every_prompt() -> None:
    plan = build_repair_plan(
        ("patient_factuality", "structural_validity"),
        details={"patient_factuality": {"unsupported": ["invented fever"]}},
    )

    assert [action.dimension for action in plan.actions] == [
        "patient_factuality",
        "structural_validity",
    ]
    assert plan.actions[0].targets == ("patient_prompt",)
    assert plan.actions[1].targets == ("orchestration",)
    assert all("general" not in target for action in plan.actions for target in action.targets)
    with pytest.raises(ValueError, match="unknown failed dimension"):
        build_repair_plan(("composite",))


def test_aggregation_is_pure_and_excludes_incomplete_values(tmp_path: Path) -> None:
    provider = MockProvider(["must not be consumed"])
    context = RunManager(tmp_path, git_commit="abc123").resolve(_config())
    measured = AttemptRecord.synthetic_for_test(context, case_id="case-1")
    incomplete = replace(
        AttemptRecord.synthetic_for_test(context, case_id="case-2"),
        evaluation={
            "scores": {
                "naturalness": {"value": None, "status": "incomplete"},
            },
            "acceptance": {"overall": "INCOMPLETE"},
        },
    )

    report = aggregate_attempts((measured, incomplete))

    assert provider.calls == []
    assert report["attempts"] == 2
    assert report["dimensions"]["naturalness"]["measured"] == 1
    assert report["dimensions"]["naturalness"]["incomplete"] == 1


def test_all_sample_configs_load_and_cover_every_variant() -> None:
    config_dir = Path(__file__).resolve().parents[2] / "configs" / "experiments"
    configs = [load_run_config(path) for path in sorted(config_dir.glob("*.json"))]

    assert configs
    assert {config.variant for config in configs} >= {variant.value for variant in VariantName}
