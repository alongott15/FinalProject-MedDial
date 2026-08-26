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
from meddial.experiments.study import PublicationStudyDesign, PublicationStudyRunner


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
            "model_calls": [{"usage": {"input_tokens": 7, "output_tokens": 5}}],
            "evaluation": {
                "composite_score": 0.5,
                "evaluation_status": "FAIL",
                "metrics": {
                    "structural_validity": {"score": 1.0},
                    "knowledge_boundary": {
                        "score": 1.0,
                        "details": {"leakage_event_count": 0, "leakage_rate": 0.0},
                    },
                    "independent_ensemble": {
                        "details": {
                            "dimensions": {
                                "patient_factuality": 0.8,
                                "doctor_factuality": 0.7,
                                "clinical_plausibility": 0.9,
                            }
                        }
                    },
                },
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
    assert stats["first_attempt_success_rate"] == 0.0
    assert stats["zero_leakage_rate"] == 1.0
    assert stats["avg_doctor_factuality_score"] == 0.7
    assert stats["total_model_calls"] == 1
    assert stats["total_tokens"] == 12


def test_recommended_study_separates_architecture_policy_and_recovery():
    design = PublicationStudyDesign.recommended(
        "private/cohort_manifest.json",
        {"doctor": "generator", "patient": "generator"},
    )
    assert len(design.cells) == 13
    architecture = [cell for cell in design.cells if cell.phase.value == "architecture_ablation"]
    assert len(architecture) == 5
    assert all(cell.config.max_attempts == 1 for cell in architecture)
    recovery = [cell for cell in design.cells if cell.phase.value == "targeted_recovery"]
    assert [cell.config.max_attempts for cell in recovery] == [1, 3]
    assert design.primary_outcome == "all_mandatory_dimensions_pass_first_attempt"


def test_study_runner_fails_before_mislabelling_missing_variants(tmp_path):
    design = PublicationStudyDesign.recommended(
        "private/cohort_manifest.json",
        {"doctor": "generator", "patient": "generator"},
    )

    class IncompleteExecutor:
        supported_variants = frozenset({AblationVariant.FULL_MEDDIAL})

        def execute(self, config, profiles):  # pragma: no cover - must not be called
            raise AssertionError("executor should not run")

    with pytest.raises(ValueError, match="missing variant implementations"):
        PublicationStudyRunner(tmp_path).run(design, [], IncompleteExecutor())
