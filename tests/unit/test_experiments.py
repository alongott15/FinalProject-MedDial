from __future__ import annotations

import pytest

from dialogue_generation_framework import DialogueGenerationPipeline
from meddial.experiments.aggregation import aggregate_attempt_records
from meddial.experiments.config import AblationVariant, ExperimentConfig
from meddial.experiments.records import (
    AttemptRecord,
    AttemptStore,
    ResumeConfigurationMismatch,
    RunManager,
)


def test_ablation_variants_have_explicit_features():
    full = ExperimentConfig(variant=AblationVariant.FULL_MEDDIAL)
    direct = ExperimentConfig(variant=AblationVariant.DIRECT_LLM)
    assert full.features["knowledge_control"]
    assert full.features["role_aware_evaluation"]
    assert not direct.features["structured_reference"]
    assert full.config_hash != direct.config_hash


def test_run_manager_rejects_config_hash_mismatch(tmp_path):
    manager = RunManager(tmp_path)
    first = manager.resolve(ExperimentConfig(seed=1), requested_run_id="fixed")
    assert first.run_dir.exists()
    with pytest.raises(ResumeConfigurationMismatch):
        manager.resolve(ExperimentConfig(seed=2), requested_run_id="fixed")


def test_attempt_records_are_immutable(tmp_path):
    config = ExperimentConfig()
    context = RunManager(tmp_path).resolve(config, requested_run_id="run")
    store = AttemptStore(context.run_dir, context.run_id, context.config_hash)
    record = AttemptRecord(
        run_id=context.run_id,
        config_hash=context.config_hash,
        profile_id="1_2",
        profile_type="FULL",
        attempt=1,
        status="FAIL",
        accepted=False,
        started_at="now",
        duration_seconds=0.1,
    )
    store.append(record)
    with pytest.raises(FileExistsError):
        store.append(record)


def test_aggregation_includes_failures_and_static_binding_is_fixed():
    attempts = [
        {
            "profile_id": "1_2",
            "profile_type": "FULL",
            "attempt": 1,
            "accepted": False,
            "duration_seconds": 1,
            "evaluation": {
                "composite_score": 0.5,
                "evaluation_status": "FAIL",
                "metrics": {},
            },
        }
    ]
    outcomes = aggregate_attempt_records(attempts, ["FULL"])
    stats = DialogueGenerationPipeline._build_global_stats(outcomes, 1, ["FULL"])
    instance = object.__new__(DialogueGenerationPipeline)
    instance_stats = instance._build_global_stats(outcomes, 1, ["FULL"])
    assert stats == instance_stats
    assert stats["failed_dialogues"] == 1
    assert stats["completed_records"] == 1
