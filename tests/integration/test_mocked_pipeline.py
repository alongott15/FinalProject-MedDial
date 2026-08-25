from __future__ import annotations

import json

import dialogue_generation_framework as framework
from dialogue_generation_framework import DialogueGenerationPipeline
from meddial.llm import MockLLMProvider


class FakeAgent:
    def __init__(self, *, llm=None, **kwargs):
        self.llm = llm or MockLLMProvider([])
        self.kwargs = kwargs

    def update_prompt(self, value):
        self.value = value


class FakeJudge:
    def evaluate_dialogue(self, dialogue, patient_profile, transcript, evaluator_context=None):
        assert evaluator_context is not None
        return {
            "decision": "REALISTIC",
            "evaluation_status": "PASS",
            "score": 0.9,
            "composite_score": 0.9,
            "justification": "all dimensions passed",
            "metrics": {
                "naturalness": {"score": 0.9, "status": "PASS"},
                "role_aware_clinical_faithfulness": {"score": 0.9, "status": "PASS"},
                "knowledge_boundary": {"score": 1.0, "status": "PASS"},
                "structural_validity": {"score": 1.0, "status": "PASS"},
            },
            "deepeval_scores": {
                "naturalness": 0.9,
                "profile_compliance": 1.0,
                "claim_faithfulness": 0.9,
                "knowledge_boundary": 1.0,
                "structural_validity": 1.0,
                "profile_type": patient_profile["profile_type"],
            },
            "feedback_for_improvement": {},
        }


def test_pipeline_writes_immutable_records_and_separate_stats(
    monkeypatch, tmp_path, clinical_reference
):
    dialogue = [
        {"role": "Doctor", "content": "What brings you in?"},
        {"role": "Patient", "content": "I have a dry cough."},
        {"role": "Doctor", "content": "How long has it lasted?"},
        {"role": "Patient", "content": "Three days."},
    ]
    monkeypatch.setattr(
        framework,
        "simulate_dialogue",
        lambda *args, **kwargs: (dialogue, "\n".join(f"{t['role']}: {t['content']}" for t in dialogue)),
    )
    pipeline = DialogueGenerationPipeline(
        max_attempts=2,
        output_dir=str(tmp_path),
        generation_llm=MockLLMProvider([]),
        judge_agent=FakeJudge(),
        doctor_factory=FakeAgent,
        patient_factory=FakeAgent,
    )
    stats = pipeline.run_pipeline(
        [clinical_reference], profile_types=["NO_DIAGNOSIS_NO_TREATMENT"]
    )
    assert stats["successful_dialogues"] == 1
    assert stats["statistics_source"] == "immutable_attempt_records"
    attempt_files = list((pipeline.run_dir / "attempt_records").glob("*.json"))
    assert len(attempt_files) == 1
    with attempt_files[0].open() as handle:
        record = json.load(handle)
    assert record["run_id"] == stats["run_id"]
    assert record["config_hash"] == stats["config_hash"]
    assert (pipeline.run_dir / "global_stats.json").exists()
    assert (pipeline.run_dir / "per_profile_stats.json").exists()

    # Same config/data resumes without duplicating immutable records.
    resumed = pipeline.run_pipeline(
        [clinical_reference], profile_types=["NO_DIAGNOSIS_NO_TREATMENT"], resume=True
    )
    assert resumed["run_id"] == stats["run_id"]
    assert len(list((pipeline.run_dir / "attempt_records").glob("*.json"))) == 1
