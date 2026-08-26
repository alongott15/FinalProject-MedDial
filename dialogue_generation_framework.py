"""MedDial dialogue generation orchestration.

Generation writes immutable per-attempt records. Aggregate statistics are
derived afterward by pure analysis functions in ``meddial.experiments``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from Agents.DoctorAgent import DoctorAgent
from Agents.PatientAgent import PatientAgent
from Agents.PromptImprovementAgent import PromptImprovementAgent
from meddial.evaluation.judge import RoleAwareJudgeAgent
from meddial.experiments.aggregation import aggregate_attempt_records, build_global_stats
from meddial.experiments.config import ExperimentConfig
from meddial.experiments.records import AttemptRecord, AttemptStore, RunManager
from meddial.knowledge import ConversationContexts, build_conversation_contexts
from meddial.llm import DataClassification, LLMProvider, ensure_provider_compatible
from meddial.recovery import FailureClassifier
from simulation import simulate_dialogue
from Utils.dialogue_markdown import save_dialogue_markdown
from Utils.markdown_gtmf import load_all_gtmfs_from_directory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class DialogueGenerationPipeline:
    def __init__(
        self,
        max_attempts: int = 3,
        max_turns: int = 30,
        judge_threshold: float = 0.7,
        output_dir: str = "output_dialogue_framework",
        *,
        experiment_config: ExperimentConfig | None = None,
        run_id: str | None = None,
        generation_llm: LLMProvider | None = None,
        evaluator_llm: LLMProvider | None = None,
        judge_agent: Any | None = None,
        recovery_agent: Any | None = None,
        doctor_factory: Callable[..., Any] = DoctorAgent,
        patient_factory: Callable[..., Any] = PatientAgent,
    ) -> None:
        self.max_attempts = max_attempts
        self.max_turns = max_turns
        self.judge_threshold = judge_threshold
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.requested_run_id = run_id
        self.experiment_config = experiment_config or ExperimentConfig(
            max_attempts=max_attempts, max_turns=max_turns
        )
        if generation_llm is not None:
            ensure_provider_compatible(generation_llm, DataClassification.RESTRICTED_CLINICAL)
        if evaluator_llm is not None:
            ensure_provider_compatible(evaluator_llm, DataClassification.RESTRICTED_CLINICAL)
        self.generation_llm = generation_llm
        self.judge_agent = judge_agent or RoleAwareJudgeAgent(
            llm=evaluator_llm or generation_llm,
            threshold=judge_threshold,
        )
        self.recovery_agent = recovery_agent or PromptImprovementAgent()
        self.failure_classifier = FailureClassifier()
        self.doctor_factory = doctor_factory
        self.patient_factory = patient_factory
        self.run_context = None
        self.attempt_store: AttemptStore | None = None
        self.run_dir: Path | None = None
        generation_model = (
            generation_llm.model_name if generation_llm is not None else "local:gpt-oss-20b"
        )
        evaluator_provider = getattr(self.judge_agent, "llm", None)
        evaluator_model = getattr(evaluator_provider, "model_name", type(self.judge_agent).__name__)
        self.experiment_config = replace(
            self.experiment_config,
            generation_models={
                "doctor": generation_model,
                "patient": generation_model,
                **dict(self.experiment_config.generation_models),
            },
            evaluator_models=(
                self.experiment_config.evaluator_models
                or ({"evaluator_id": "primary", "model": evaluator_model},)
            ),
            acceptance_thresholds={
                "legacy_composite_reporting_threshold": judge_threshold,
                **dict(self.experiment_config.acceptance_thresholds),
            },
        )

    @staticmethod
    def _profile_id(profile: Mapping[str, Any]) -> str:
        return f"{profile.get('subject_id', 'unknown')}_{profile.get('hadm_id', 'unknown')}"

    @staticmethod
    def _call_history(provider: Any) -> list[Mapping[str, Any]]:
        history = getattr(provider, "call_history", [])
        return [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in history]

    def _model_calls_since(
        self, agents: list[Any], before: Mapping[int, int]
    ) -> tuple[Mapping[str, Any], ...]:
        calls: list[Mapping[str, Any]] = []
        seen: set[int] = set()
        for agent in agents:
            provider = getattr(agent, "llm", None)
            if provider is None or id(provider) in seen:
                continue
            seen.add(id(provider))
            history = self._call_history(provider)
            calls.extend(history[before.get(id(provider), 0) :])
        return tuple(calls)

    def _record_attempt(
        self,
        *,
        profile_id: str,
        profile_type: str,
        attempt: int,
        started_at: str,
        duration: float,
        dialogue: list[Mapping[str, str]],
        transcript: str | None,
        evaluation: Mapping[str, Any],
        accepted: bool,
        failure_class: str | None,
        error: str | None,
        model_calls: tuple[Mapping[str, Any], ...],
    ) -> None:
        if self.attempt_store is None or self.run_context is None:
            return
        self.attempt_store.append(
            AttemptRecord(
                run_id=self.run_context.run_id,
                config_hash=self.run_context.config_hash,
                profile_id=profile_id,
                profile_type=profile_type,
                attempt=attempt,
                status=(
                    str(evaluation.get("evaluation_status"))
                    if evaluation
                    else ("ERROR" if error else "FAIL")
                ),
                accepted=accepted,
                started_at=started_at,
                duration_seconds=duration,
                dialogue=tuple(dict(turn) for turn in dialogue),
                transcript=transcript,
                evaluation=dict(evaluation),
                failure_class=failure_class,
                error=error,
                model_calls=model_calls,
            )
        )

    def _make_agents(
        self, contexts: ConversationContexts, improvements: Mapping[str, str]
    ) -> tuple[Any, Any]:
        doctor = self.doctor_factory(
            doctor_context=contexts.doctor,
            llm=self.generation_llm,
        )
        patient = self.patient_factory(
            profile=contexts.patient.as_dict(),
            llm=self.generation_llm,
        )
        if improvements.get("doctor_improvements"):
            doctor.update_prompt(improvements["doctor_improvements"])
        if improvements.get("patient_improvements"):
            patient.update_prompt(improvements["patient_improvements"])
        return doctor, patient

    def generate_dialogue_with_iterations(
        self,
        patient_profile: Mapping[str, Any],
        full_profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        profile_id = self._profile_id(full_profile)
        profile_type = str(patient_profile.get("profile_type", "NO_DIAGNOSIS_NO_TREATMENT"))
        contexts = build_conversation_contexts(full_profile, profile_type)
        prior_records = (
            self.attempt_store.attempts_for(profile_id, profile_type)
            if self.attempt_store is not None
            else []
        )
        attempts: list[dict[str, Any]] = [
            {
                "attempt": record.get("attempt"),
                "success": record.get("accepted", False),
                "decision": record.get("evaluation", {}).get("decision", record.get("status")),
                "score": record.get("evaluation", {}).get("composite_score"),
                "reason": record.get("error") or record.get("evaluation", {}).get("justification"),
                "turns": len(record.get("dialogue", [])),
                "time_seconds": record.get("duration_seconds", 0.0),
                "failure_class": record.get("failure_class"),
            }
            for record in prior_records
        ]
        accepted_dialogue: dict[str, Any] | None = None
        best_candidate: dict[str, Any] | None = None
        best_score = -1.0
        improvements: Mapping[str, str] = {}

        prior_accepted = next((record for record in prior_records if record.get("accepted")), None)
        if prior_accepted is not None:
            accepted_dialogue = {
                "conversation": prior_accepted.get("dialogue", []),
                "transcript": prior_accepted.get("transcript"),
                "judge_result": prior_accepted.get("evaluation", {}),
                "attempt": prior_accepted.get("attempt"),
            }

        next_attempt = (
            max((int(record.get("attempt", 0)) for record in prior_records), default=0) + 1
        )
        for attempt_index in (
            range(next_attempt, self.max_attempts + 1) if accepted_dialogue is None else range(0)
        ):
            started_at = datetime.now(timezone.utc).isoformat()
            attempt_start = time.perf_counter()
            doctor, patient = self._make_agents(contexts, improvements)
            tracked_agents = [doctor, patient, self.judge_agent]
            before = {
                id(agent.llm): len(self._call_history(agent.llm))
                for agent in tracked_agents
                if getattr(agent, "llm", None) is not None
            }
            conversation: list[Mapping[str, str]] = []
            transcript: str | None = None
            evaluation: dict[str, Any] = {}
            error: str | None = None
            failure_class: str | None = None
            accepted = False
            try:
                conversation, transcript = simulate_dialogue(
                    doctor,
                    patient,
                    max_turns=self.max_turns,
                    consecutive_confusion_limit=2,
                    loop_detection_window=4,
                    profile_type=profile_type,
                )
                if not conversation or len(conversation) < 4:
                    error = "Dialogue failed deterministic precheck: fewer than 4 turns"
                else:
                    evaluation = self.judge_agent.evaluate_dialogue(
                        conversation,
                        contexts.patient.as_dict(),
                        transcript,
                        evaluator_context=contexts.evaluator,
                    )
                    accepted = evaluation.get("decision") == "REALISTIC"
                    failure_class = self.failure_classifier.classify(evaluation).value
                    score = evaluation.get("composite_score")
                    numeric_score = float(score) if score is not None else -1.0
                    if numeric_score > best_score:
                        best_score = numeric_score
                        best_candidate = {
                            "conversation": conversation,
                            "transcript": transcript,
                            "judge_result": evaluation,
                            "attempt": attempt_index,
                        }
                    if accepted:
                        accepted_dialogue = {
                            "conversation": conversation,
                            "transcript": transcript,
                            "judge_result": evaluation,
                            "attempt": attempt_index,
                        }
            except Exception as exc:
                logger.exception("Attempt %s failed", attempt_index)
                error = f"{type(exc).__name__}: {exc}"

            duration = time.perf_counter() - attempt_start
            model_calls = self._model_calls_since(tracked_agents, before)
            self._record_attempt(
                profile_id=profile_id,
                profile_type=profile_type,
                attempt=attempt_index,
                started_at=started_at,
                duration=duration,
                dialogue=list(conversation),
                transcript=transcript,
                evaluation=evaluation,
                accepted=accepted,
                failure_class=failure_class,
                error=error,
                model_calls=model_calls,
            )
            attempts.append(
                {
                    "attempt": attempt_index,
                    "success": accepted,
                    "decision": evaluation.get("decision", "ERROR" if error else "UNSCORABLE"),
                    "score": evaluation.get("composite_score"),
                    "reason": error or evaluation.get("justification"),
                    "turns": len(conversation),
                    "time_seconds": duration,
                    "failure_class": failure_class,
                }
            )
            if accepted:
                break
            if evaluation and attempt_index < self.max_attempts:
                improvements = self.recovery_agent.improve_prompts(evaluation, list(conversation))

        if accepted_dialogue is not None:
            return {
                "success": True,
                "profile_id": profile_id,
                "best_attempt": accepted_dialogue["attempt"],
                "attempts_summary": attempts,
                "dialogue": accepted_dialogue["conversation"],
                "transcript": accepted_dialogue["transcript"],
                "judge_evaluation": accepted_dialogue["judge_result"],
            }
        return {
            "success": False,
            "profile_id": profile_id,
            "best_attempt": best_candidate.get("attempt") if best_candidate else None,
            "attempts_summary": attempts,
            "dialogue": best_candidate.get("conversation") if best_candidate else None,
            "transcript": best_candidate.get("transcript") if best_candidate else None,
            "judge_evaluation": best_candidate.get("judge_result", {}) if best_candidate else {},
        }

    def process_profile(
        self,
        full_profile: Mapping[str, Any],
        profile_type: str = "NO_DIAGNOSIS_NO_TREATMENT",
    ) -> dict[str, Any]:
        profile_id = self._profile_id(full_profile)
        contexts = build_conversation_contexts(full_profile, profile_type)
        started = time.perf_counter()
        dialogue_result = self.generate_dialogue_with_iterations(
            contexts.patient.as_dict(), full_profile
        )
        processing_time = time.perf_counter() - started
        evaluation = dialogue_result.get("judge_evaluation", {})
        result: dict[str, Any] = {
            "profile_id": profile_id,
            "subject_id": full_profile.get("subject_id"),
            "hadm_id": full_profile.get("hadm_id"),
            "profile_type": profile_type,
            "success": bool(dialogue_result["success"]),
            "is_realistic": evaluation.get("decision") == "REALISTIC",
            "best_attempt": dialogue_result.get("best_attempt"),
            "total_attempts": len(dialogue_result["attempts_summary"]),
            "attempts_summary": dialogue_result["attempts_summary"],
            "dialogue": dialogue_result.get("dialogue"),
            "transcript": dialogue_result.get("transcript"),
            "judge_evaluation": evaluation,
            "deepeval_scores": evaluation.get("deepeval_scores", {}),
            "processing_time": processing_time,
        }
        if result["dialogue"]:
            result["dialogue_stats"] = {
                "turn_count": len(result["dialogue"]),
                "word_count": len((result["transcript"] or "").split()),
                "doctor_turns": sum(
                    str(turn.get("role", "")).lower() == "doctor" for turn in result["dialogue"]
                ),
                "patient_turns": sum(
                    str(turn.get("role", "")).lower() == "patient" for turn in result["dialogue"]
                ),
            }
        if result["success"] and self.run_dir is not None:
            output_path = self.run_dir / f"dialogue_{profile_id}_{profile_type}.md"
            save_dialogue_markdown(result, str(output_path))
            result["output_path"] = str(output_path)
        if self.attempt_store is not None:
            self.attempt_store.finalize(
                profile_id,
                profile_type,
                {
                    "success": result["success"],
                    "best_attempt": result["best_attempt"],
                    "evaluation_status": evaluation.get("evaluation_status"),
                    "composite_score": evaluation.get("composite_score"),
                },
            )
        return result

    def run_pipeline(
        self,
        gtmf_data: list[dict[str, Any]],
        profile_types: list[str] | None = None,
        resume: bool = True,
    ) -> dict[str, Any]:
        selected_types = tuple(profile_types or self.experiment_config.profile_types)
        cohort_identity = sorted(
            (
                profile.get("subject_id"),
                profile.get("hadm_id"),
                profile.get("row_id"),
            )
            for profile in gtmf_data
        )
        cohort_hash = hashlib.sha256(
            json.dumps(cohort_identity, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        effective_config = replace(
            self.experiment_config,
            max_attempts=self.max_attempts,
            max_turns=self.max_turns,
            profile_types=selected_types,
            metadata={
                **dict(self.experiment_config.metadata),
                "input_reference_manifest_hash": cohort_hash,
                "input_reference_count": len(gtmf_data),
            },
        )
        self.experiment_config = effective_config
        self.run_context = RunManager(self.output_dir).resolve(
            effective_config,
            requested_run_id=self.requested_run_id,
            resume=resume,
        )
        self.run_dir = self.run_context.run_dir
        self.attempt_store = AttemptStore(
            self.run_dir, self.run_context.run_id, self.run_context.config_hash
        )

        for full_profile in gtmf_data:
            profile_id = self._profile_id(full_profile)
            for profile_type in selected_types:
                if resume and self.attempt_store.is_complete(profile_id, profile_type):
                    logger.info("Skipping completed record %s/%s", profile_id, profile_type)
                    continue
                self.process_profile(full_profile, profile_type)

        attempts = self.attempt_store.load_attempts()
        per_profile_stats = aggregate_attempt_records(attempts, selected_types)
        global_stats = build_global_stats(per_profile_stats, len(gtmf_data), selected_types)
        global_stats.update(
            {
                "run_id": self.run_context.run_id,
                "config_hash": self.run_context.config_hash,
                "statistics_source": "immutable_attempt_records",
            }
        )
        with (self.run_dir / "per_profile_stats.json").open("w", encoding="utf-8") as handle:
            json.dump(per_profile_stats, handle, indent=2)
        with (self.run_dir / "global_stats.json").open("w", encoding="utf-8") as handle:
            json.dump(global_stats, handle, indent=2)
        return global_stats

    @staticmethod
    def _build_global_stats(
        per_profile_stats: list[dict[str, Any]],
        total_profiles: int,
        profile_types: list[str],
    ) -> dict[str, Any]:
        """Compatibility wrapper for the corrected pure aggregation function."""
        return build_global_stats(per_profile_stats, total_profiles, profile_types)


def main() -> None:
    logger.info("Starting the MedDial synthetic clinical-dialogue framework")
    scr_dir = "gtmf"  # historical directory name retained for existing artifacts
    if not os.path.exists(scr_dir):
        logger.error("SCR directory not found: %s", scr_dir)
        return
    references = load_all_gtmfs_from_directory(scr_dir)
    if not references:
        logger.error("No Structured Clinical Reference files found in %s", scr_dir)
        return
    pipeline = DialogueGenerationPipeline()
    stats = pipeline.run_pipeline(references)
    logger.info(
        "Run %s completed; results are under %s",
        stats["run_id"],
        pipeline.run_dir,
    )


if __name__ == "__main__":
    main()
