import os
from pathlib import Path


def dialogue_to_markdown(dialogue_result: dict) -> str:
    lines = []

    profile_id = dialogue_result.get('profile_id', 'unknown')
    subject_id = dialogue_result.get('subject_id', 'unknown')
    hadm_id = dialogue_result.get('hadm_id', 'unknown')

    lines.append(f"# Patient-Physician Dialogue: {profile_id}\n")

    lines.append("## Profile Information\n")
    lines.append(f"- **Subject ID**: {subject_id}")
    lines.append(f"- **Admission ID**: {hadm_id}")
    lines.append(f"- **Profile Type**: {dialogue_result.get('profile_type', 'UNKNOWN')}")
    lines.append(f"- **Success**: {dialogue_result.get('success', False)}")
    lines.append(f"- **Is Realistic**: {dialogue_result.get('is_realistic', False)}\n")

    attempts_summary = dialogue_result.get('attempts_summary', [])
    if attempts_summary:
        lines.append("## Generation Attempts\n")
        lines.append(f"- **Total Attempts**: {dialogue_result.get('total_attempts', 0)}")
        lines.append(f"- **Best Attempt**: {dialogue_result.get('best_attempt', 0)}\n")

        for attempt in attempts_summary:
            attempt_num = attempt.get('attempt', 0)
            success = attempt.get('success', False)
            decision = attempt.get('decision', 'N/A')
            score = attempt.get('score', 0.0)
            lines.append(f"### Attempt {attempt_num}")
            lines.append(f"- Success: {success}")
            lines.append(f"- Decision: {decision}")
            lines.append(f"- Score: {score:.3f}")
            lines.append("")

    judge_eval = dialogue_result.get('judge_evaluation', {})
    if judge_eval:
        lines.append("## Judge Evaluation\n")
        lines.append(f"- **Decision**: {judge_eval.get('decision', 'N/A')}")
        lines.append(f"- **Score**: {judge_eval.get('score', 0.0):.3f}")
        lines.append(f"- **Justification**: {judge_eval.get('justification', 'N/A')}\n")

        # DeepEval sub-score breakdown
        deepeval = dialogue_result.get('deepeval_scores') or judge_eval.get('deepeval_scores', {})
        if deepeval:
            lines.append("### DeepEval Sub-Scores\n")
            lines.append(f"| Metric | Score |")
            lines.append(f"|---|---|")
            if deepeval.get('naturalness') is not None:
                lines.append(f"| Naturalness | {deepeval['naturalness']:.3f} |")
            if deepeval.get('profile_compliance') is not None:
                lines.append(f"| Profile Compliance ({deepeval.get('profile_type', 'N/A')}) | {deepeval['profile_compliance']:.3f} |")
            if deepeval.get('ragas_faithfulness') is not None:
                lines.append(f"| RAGAS Faithfulness | {deepeval['ragas_faithfulness']:.3f} |")
            lines.append("")

        feedback = judge_eval.get('feedback_for_improvement', {})
        if feedback:
            lines.append("### Feedback for Improvement\n")
            for key, value in feedback.items():
                lines.append(f"**{key.replace('_', ' ').title()}**: {value}\n")

    dialogue = dialogue_result.get('dialogue', [])
    if dialogue:
        lines.append("## Dialogue Transcript\n")
        for turn in dialogue:
            role = turn.get('role', 'Unknown')
            content = turn.get('content', '')
            lines.append(f"**{role}**: {content}\n")

    stats = dialogue_result.get('dialogue_stats', {})
    if stats:
        lines.append("## Dialogue Statistics\n")
        lines.append(f"- **Turn Count**: {stats.get('turn_count', 0)}")
        lines.append(f"- **Word Count**: {stats.get('word_count', 0)}")
        lines.append(f"- **Doctor Turns**: {stats.get('doctor_turns', 0)}")
        lines.append(f"- **Patient Turns**: {stats.get('patient_turns', 0)}\n")

    processing_time = dialogue_result.get('processing_time', 0)
    if processing_time:
        lines.append("## Processing Information\n")
        lines.append(f"- **Processing Time**: {processing_time:.1f}s\n")

    return "\n".join(lines)


def save_dialogue_markdown(dialogue_result: dict, output_path: str) -> None:
    markdown_content = dialogue_to_markdown(dialogue_result)

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(markdown_content)