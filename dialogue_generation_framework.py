import json
import logging
import os
import time
from pathlib import Path

from Utils.partial_profile import generate_partial_profiles
from Utils.markdown_gtmf import load_all_gtmfs_from_directory
from Utils.dialogue_markdown import save_dialogue_markdown
from Agents.PatientAgent import PatientAgent
from Agents.DoctorAgent import DoctorAgent
from Agents.DeepEvalJudgeAgent import DeepEvalJudgeAgent
from Agents.PromptImprovementAgent import PromptImprovementAgent
from simulation import simulate_dialogue


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class DialogueGenerationPipeline:
    def __init__(self, max_attempts=3, max_turns=30, judge_threshold=0.7, output_dir="output_dialogue_framework"):
        """
        Initialize the dialogue generation pipeline.

        Args:
            max_attempts: Maximum attempts to generate a realistic dialogue
            max_turns: Maximum turns per dialogue (safety limit, not target). Default 30.
                      Dialogues can end naturally much earlier (6-12 turns typically).
            judge_threshold: Minimum score for dialogue to be considered realistic
            output_dir: Directory for output files
        """
        self.max_attempts = max_attempts
        self.max_turns = max_turns
        self.judge_threshold = judge_threshold
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)

        logger.info("Initializing pipeline agents...")
        # DeepEvalJudgeAgent replaces the legacy JudgeAgent.
        # It uses deepeval GEval metrics + RAGAS faithfulness, all backed by
        # the same Azure AI Foundry / GPT-4.1 endpoint.
        self.judge_agent = DeepEvalJudgeAgent(threshold=judge_threshold)
        self.prompt_improvement_agent = PromptImprovementAgent()

        logger.info("Pipeline initialized successfully")

    def generate_dialogue_with_iterations(
        self,
        patient_profile: dict,
        full_profile: dict,
    ) -> dict:
        profile_id = f"{patient_profile.get('subject_id', 'unknown')}_{patient_profile.get('hadm_id', 'unknown')}"
        logger.info(f"\n{'='*60}")
        logger.info(f"Generating dialogue for profile: {profile_id}")
        logger.info(f"Profile type: {patient_profile.get('profile_type', 'UNKNOWN')}")

        attempts = []
        best_dialogue = None
        best_score = 0.0
        best_attempt_idx = -1

        doctor_agent = DoctorAgent(patient_profile=patient_profile)
        patient_agent = PatientAgent(profile=patient_profile)

        for attempt_idx in range(self.max_attempts):
            logger.info(f"  Attempt {attempt_idx + 1}/{self.max_attempts}")
            attempt_start = time.time()

            try:
                logger.info(f"  Generating dialogue...")
                conversation, transcript = simulate_dialogue(
                    doctor_agent, patient_agent,
                    max_turns=self.max_turns,
                    consecutive_confusion_limit=2,
                    loop_detection_window=4,
                    profile_type=patient_profile.get('profile_type', 'NO_DIAGNOSIS_NO_TREATMENT')
                )

                time.sleep(1)

                if not conversation or len(conversation) < 4:
                    logger.warning(f"  Dialogue too short: {len(conversation)} turns")
                    attempts.append({
                        "attempt": attempt_idx + 1,
                        "success": False,
                        "reason": "Dialogue too short",
                        "turns": len(conversation) if conversation else 0
                    })
                    continue

                logger.info(f"  Dialogue complete ({len(conversation)} turns)")
                time.sleep(1)

                logger.info(f"  Evaluating with DeepEvalJudgeAgent...")
                # Pass patient_profile (the partial profile) so the judge can read
                # profile_type and apply the correct compliance/faithfulness rules.
                # patient_profile still contains all symptoms, history, and
                # medications — only diagnoses/treatments absent from the original
                # are stripped for NO_DIAGNOSIS / NO_DIAGNOSIS_NO_TREATMENT types.
                judge_result = self.judge_agent.evaluate_dialogue(
                    conversation, patient_profile, transcript
                )

                time.sleep(1)

                attempt_time = time.time() - attempt_start

                attempt_record = {
                    "attempt": attempt_idx + 1,
                    "success": judge_result['decision'] == "REALISTIC",
                    "score": judge_result['score'],
                    "decision": judge_result['decision'],
                    "justification": judge_result['justification'],
                    "turns": len(conversation),
                    "time_seconds": attempt_time
                }
                attempts.append(attempt_record)

                logger.info(f"  Result: {judge_result['decision']} (score: {judge_result['score']:.3f})")

                if judge_result['score'] > best_score:
                    best_score = judge_result['score']
                    best_dialogue = {
                        "conversation": conversation,
                        "transcript": transcript,
                        "judge_result": judge_result
                    }
                    best_attempt_idx = attempt_idx + 1

                if judge_result['decision'] == "REALISTIC":
                    logger.info(f"  Dialogue accepted on attempt {attempt_idx + 1}")
                    break

                if attempt_idx < self.max_attempts - 1:
                    logger.info(f"  Generating improvements for next attempt...")
                    improvements = self.prompt_improvement_agent.improve_prompts(
                        judge_result, conversation
                    )

                    time.sleep(1)

                    doctor_agent.update_prompt(improvements.get('doctor_improvements', ''))
                    patient_agent.update_prompt(improvements.get('patient_improvements', ''))
                    logger.info(f"  Prompts updated for attempt {attempt_idx + 2}")

            except Exception as e:
                logger.error(f"  Error in attempt {attempt_idx + 1}: {e}", exc_info=True)
                attempts.append({
                    "attempt": attempt_idx + 1,
                    "success": False,
                    "reason": f"Error: {str(e)}",
                    "turns": 0
                })

        if best_dialogue:
            return {
                "success": True,
                "profile_id": profile_id,
                "best_attempt": best_attempt_idx,
                "attempts_summary": attempts,
                "dialogue": best_dialogue['conversation'],
                "transcript": best_dialogue['transcript'],
                "judge_evaluation": best_dialogue['judge_result']
            }
        else:
            logger.error(f"  Failed to generate acceptable dialogue after {self.max_attempts} attempts")
            return {
                "success": False,
                "profile_id": profile_id,
                "attempts_summary": attempts,
                "dialogue": None,
                "transcript": None
            }

    def process_profile(
        self,
        full_profile: dict,
        profile_type: str = "NO_DIAGNOSIS_NO_TREATMENT"
    ) -> dict:
        profile_id = f"{full_profile.get('subject_id', 'unknown')}_{full_profile.get('hadm_id', 'unknown')}"
        logger.info(f"\n{'='*80}")
        logger.info(f"Processing profile {profile_id} (type: {profile_type})")

        start_time = time.time()

        partial_profile = generate_partial_profiles(full_profile, profile_type)

        dialogue_result = self.generate_dialogue_with_iterations(
            partial_profile, full_profile
        )

        if not dialogue_result['success']:
            return {
                "profile_id": profile_id,
                "success": False,
                "attempts": dialogue_result['attempts_summary'],
                "processing_time": time.time() - start_time
            }

        is_realistic = dialogue_result['judge_evaluation']['decision'] == "REALISTIC"

        processing_time = time.time() - start_time

        result = {
            "profile_id": profile_id,
            "subject_id": full_profile.get('subject_id'),
            "hadm_id": full_profile.get('hadm_id'),
            "profile_type": profile_type,
            "success": True,
            "is_realistic": is_realistic,
            "best_attempt": dialogue_result['best_attempt'],
            "total_attempts": len(dialogue_result['attempts_summary']),
            "attempts_summary": dialogue_result['attempts_summary'],
            "dialogue": dialogue_result['dialogue'],
            "transcript": dialogue_result['transcript'],
            "judge_evaluation": dialogue_result['judge_evaluation'],
            # Breakdown of the three DeepEval sub-scores (naturalness, profile_compliance,
            # ragas_faithfulness). Included here for per-dialogue inspection / logging.
            "deepeval_scores": dialogue_result['judge_evaluation'].get('deepeval_scores', {}),
            "processing_time": processing_time,
            "dialogue_stats": {
                "turn_count": len(dialogue_result['dialogue']),
                "word_count": len(dialogue_result['transcript'].split()),
                "doctor_turns": len([t for t in dialogue_result['dialogue'] if t.get('role') == 'Doctor']),
                "patient_turns": len([t for t in dialogue_result['dialogue'] if t.get('role') == 'Patient'])
            }
        }

        # Include profile type in filename to distinguish different variants
        output_path = self.output_dir / f"dialogue_{profile_id}_{profile_type}.md"
        save_dialogue_markdown(result, str(output_path))
        logger.info(f"  Saved result to {output_path}")

        return result

    def run_pipeline(
        self,
        gtmf_data: list[dict],
        profile_types: list[str] = None,
        resume: bool = True,
    ) -> dict:
        if profile_types is None:
            profile_types = ["FULL", "NO_DIAGNOSIS", "NO_DIAGNOSIS_NO_TREATMENT"]

        logger.info(f"\n{'='*80}")
        logger.info(f"Starting pipeline with {len(gtmf_data)} profiles")
        logger.info(f"Profile types: {profile_types}")
        logger.info(f"Max attempts per dialogue: {self.max_attempts}")
        logger.info(f"Judge threshold: {self.judge_threshold}")
        logger.info(f"Resume mode: {resume}")

        # ── Load existing per-profile records so we can merge at the end ──────
        # Key: "{profile_id}_{profile_type}" → stat record dict
        existing_per_profile: dict[str, dict] = {}
        if resume:
            per_profile_path = self.output_dir / "per_profile_stats.json"
            if per_profile_path.exists():
                try:
                    with open(per_profile_path, encoding='utf-8') as f:
                        for rec in json.load(f):
                            key = f"{rec['profile_id']}_{rec['profile_type']}"
                            existing_per_profile[key] = rec
                    logger.info(
                        f"Resume mode: found {len(existing_per_profile)} already-completed "
                        f"records — those profile+type pairs will be skipped."
                    )
                except Exception as exc:
                    logger.warning(f"Could not load existing per_profile_stats.json: {exc}")

        all_results = []
        stats = {
            "total_profiles": len(gtmf_data),
            "total_dialogues_attempted": 0,
            "successful_dialogues": 0,
            "failed_dialogues": 0,
            "realistic_dialogues": 0,
            "non_realistic_dialogues": 0,
            "by_profile_type": {
                pt: {
                    "success": 0,
                    "fail": 0,
                    "realistic": 0,
                    "non_realistic": 0,
                    "judge_scores": [],
                } for pt in profile_types
            },
            "attempt_distribution": {1: 0, 2: 0, 3: 0},
            "judge_scores": [],
            "processing_times": []
        }

        # Process each profile
        for idx, full_profile in enumerate(gtmf_data):
            profile_id = f"{full_profile.get('subject_id', 'unknown')}_{full_profile.get('hadm_id', 'unknown')}"

            if idx > 0:
                time.sleep(2)

            # ── Resume: determine which profile-type variants still need generating ──
            # Check upfront whether each of the N expected .md files already exists.
            if resume:
                missing_types = [
                    pt for pt in profile_types
                    if not (self.output_dir / f"dialogue_{profile_id}_{pt}.md").exists()
                ]
                if not missing_types:
                    logger.info(
                        f"  Skipping {profile_id} — all {len(profile_types)} variants "
                        f"already completed"
                    )
                    continue
                if len(missing_types) < len(profile_types):
                    done = [pt for pt in profile_types if pt not in missing_types]
                    logger.info(
                        f"  Profile {profile_id}: {len(done)}/{len(profile_types)} variants "
                        f"already done {done}, generating missing: {missing_types}"
                    )
                types_to_run = missing_types
            else:
                types_to_run = profile_types

            # Process each remaining profile type
            for profile_type in types_to_run:
                logger.info(f"  Processing profile type: {profile_type}")

                try:
                    result = self.process_profile(full_profile, profile_type)
                    all_results.append(result)

                    stats["total_dialogues_attempted"] += 1
                    if result['success']:
                        stats["successful_dialogues"] += 1
                        stats["by_profile_type"][profile_type]["success"] += 1
                        stats["attempt_distribution"][result['best_attempt']] += 1
                        stats["judge_scores"].append(result['judge_evaluation']['score'])
                        stats["by_profile_type"][profile_type]["judge_scores"].append(result['judge_evaluation']['score'])

                        if result.get('is_realistic'):
                            stats["realistic_dialogues"] += 1
                            stats["by_profile_type"][profile_type]["realistic"] += 1
                        else:
                            stats["non_realistic_dialogues"] += 1
                            stats["by_profile_type"][profile_type]["non_realistic"] += 1

                        stats["processing_times"].append(result['processing_time'])
                    else:
                        stats["failed_dialogues"] += 1
                        stats["by_profile_type"][profile_type]["fail"] += 1

                    # Add delay between profile types
                    if profile_type != profile_types[-1]:
                        time.sleep(2)

                except Exception as e:
                    logger.error(f"Error processing profile {profile_id} ({profile_type}): {e}", exc_info=True)
                    stats["failed_dialogues"] += 1
                    stats["by_profile_type"][profile_type]["fail"] += 1

        if stats["judge_scores"]:
            stats["avg_judge_score"] = sum(stats["judge_scores"]) / len(stats["judge_scores"])
            stats["avg_processing_time"] = sum(stats["processing_times"]) / len(stats["processing_times"])

        # Calculate averages for each profile type
        for pt in profile_types:
            pt_stats = stats["by_profile_type"][pt]
            if pt_stats["judge_scores"]:
                pt_stats["avg_judge_score"] = sum(pt_stats["judge_scores"]) / len(pt_stats["judge_scores"])
            else:
                pt_stats["avg_judge_score"] = None

        # ── Build per-profile stats for newly processed results ────────────
        new_per_profile = {
            f"{r['profile_id']}_{r['profile_type']}": {
                "profile_id": r.get('profile_id'),
                "profile_type": r.get('profile_type'),
                "success": r.get('success'),
                "is_realistic": r.get('is_realistic'),
                "attempts": r.get('total_attempts'),
                "best_attempt": r.get('best_attempt'),
                "judge_score": r.get('judge_evaluation', {}).get('score'),
                "naturalness_score": r.get('deepeval_scores', {}).get('naturalness'),
                "profile_compliance_score": r.get('deepeval_scores', {}).get('profile_compliance'),
                "ragas_faithfulness_score": r.get('deepeval_scores', {}).get('ragas_faithfulness'),
                "processing_time": r.get('processing_time'),
            }
            for r in all_results if r.get('success')
        }

        # Merge: new results take priority over previously stored records for
        # the same key (allows re-running a single profile by deleting its .md file).
        merged_per_profile = {**existing_per_profile, **new_per_profile}
        per_profile_stats = list(merged_per_profile.values())

        per_profile_path = self.output_dir / "per_profile_stats.json"
        with open(per_profile_path, 'w', encoding='utf-8') as f:
            json.dump(per_profile_stats, f, indent=2)

        # ── Rebuild global stats from the full merged dataset ──────────────
        # This gives accurate aggregate numbers regardless of how many
        # previous runs contributed to the output directory.
        global_stats = self._build_global_stats(per_profile_stats, len(gtmf_data), profile_types)
        stats_path = self.output_dir / "global_stats.json"
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(global_stats, f, indent=2)

        logger.info(f"\n{'='*80}")
        logger.info("PIPELINE SUMMARY")
        logger.info(f"{'='*80}")
        logger.info(f"Total profiles: {global_stats['total_profiles']}")
        logger.info(f"Completed records: {global_stats['completed_records']}")
        logger.info(f"Successful dialogues: {global_stats['successful_dialogues']}")
        logger.info(f"  - Realistic: {global_stats['realistic_dialogues']}")
        logger.info(f"  - Non-realistic: {global_stats['non_realistic_dialogues']}")
        if global_stats.get('avg_judge_score'):
            logger.info(f"Average judge score: {global_stats['avg_judge_score']:.3f}")
            logger.info(f"Average processing time: {global_stats['avg_processing_time']:.1f}s")

        logger.info(f"\n{'='*80}")
        logger.info("STATS BY PROFILE TYPE")
        logger.info(f"{'='*80}")
        for pt in profile_types:
            pt_stats = global_stats["by_profile_type"].get(pt, {})
            logger.info(f"\n{pt}:")
            logger.info(f"  Success: {pt_stats.get('success', 0)}, Fail: {pt_stats.get('fail', 0)}")
            logger.info(f"  Realistic: {pt_stats.get('realistic', 0)}, Non-realistic: {pt_stats.get('non_realistic', 0)}")
            if pt_stats.get('avg_judge_score') is not None:
                logger.info(f"  Avg Judge Score: {pt_stats['avg_judge_score']:.3f}")

        return global_stats

    def _build_global_stats(per_profile_stats: list[dict], total_profiles: int, profile_types: list[str]) -> dict:
        """Rebuild aggregate statistics from the complete per-profile records list.

        Called at the end of every run so global_stats.json always reflects the
        full dataset — including records produced by previous (interrupted) runs.
        """
        judge_scores = [r['judge_score'] for r in per_profile_stats if r.get('judge_score') is not None]
        processing_times = [r['processing_time'] for r in per_profile_stats if r.get('processing_time') is not None]

        by_pt: dict[str, dict] = {pt: {
            "success": 0, "fail": 0,
            "realistic": 0, "non_realistic": 0,
            "judge_scores": [],
        } for pt in profile_types}

        for r in per_profile_stats:
            pt = r.get('profile_type', 'UNKNOWN')
            if pt not in by_pt:
                by_pt[pt] = {"success": 0, "fail": 0, "realistic": 0, "non_realistic": 0, "judge_scores": []}
            if r.get('success'):
                by_pt[pt]["success"] += 1
                if r.get('judge_score') is not None:
                    by_pt[pt]["judge_scores"].append(r['judge_score'])
                if r.get('is_realistic'):
                    by_pt[pt]["realistic"] += 1
                else:
                    by_pt[pt]["non_realistic"] += 1
            else:
                by_pt[pt]["fail"] += 1

        for pt_stats in by_pt.values():
            scores = pt_stats.pop("judge_scores")
            pt_stats["avg_judge_score"] = (sum(scores) / len(scores)) if scores else None

        return {
            "total_profiles": total_profiles,
            "completed_records": len(per_profile_stats),
            "successful_dialogues": sum(1 for r in per_profile_stats if r.get('success')),
            "realistic_dialogues": sum(1 for r in per_profile_stats if r.get('is_realistic')),
            "non_realistic_dialogues": sum(1 for r in per_profile_stats if r.get('success') and not r.get('is_realistic')),
            "avg_judge_score": (sum(judge_scores) / len(judge_scores)) if judge_scores else None,
            "avg_processing_time": (sum(processing_times) / len(processing_times)) if processing_times else None,
            "by_profile_type": by_pt,
        }


def main():
    logger.info("Starting Synthetic Patient-Physician Conversation Framework")

    # Load GTMF data from Markdown files (light cases only)
    gtmf_dir = "gtmf"  # Directory containing Markdown GTMF files

    if not os.path.exists(gtmf_dir):
        logger.error(f"GTMF directory not found: {gtmf_dir}")
        logger.info("Please run gtmf_creation.py first to generate light-case GTMFs")
        return

    logger.info(f"Loading GTMFs from {gtmf_dir}/...")
    gtmf_data = load_all_gtmfs_from_directory(gtmf_dir)

    if not gtmf_data:
        logger.error(f"No GTMF files found in {gtmf_dir}/")
        logger.info("Please run gtmf_creation.py first to generate GTMFs")
        return

    logger.info(f"Loaded {len(gtmf_data)} GTMF profiles from Markdown files")

    pipeline = DialogueGenerationPipeline(
        max_attempts=3,
        max_turns=30,  # Safety limit - dialogues can end naturally much earlier (6-12 turns)
        judge_threshold=0.70,
        output_dir="output_dialogue_framework",
    )

    stats = pipeline.run_pipeline(
        gtmf_data=gtmf_data,
        profile_types=None
    )

    logger.info("\nPipeline completed successfully!")
    logger.info(f"Results saved to: {pipeline.output_dir}")


if __name__ == "__main__":
    main()