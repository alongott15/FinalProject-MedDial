import logging
import json
from typing import Dict
from Utils.llms_utils import load_gpt_model, chat_generate
from Utils.bias_aware_prompts import PROMPT_IMPROVEMENT_PROMPT, PATIENT_PROFILE_TYPE_KNOWLEDGE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PromptImprovementAgent:
    """
    Translates judge feedback into actionable prompt improvements.

    Maintains bias-aware constraints while adjusting stylistic/structural aspects.
    """

    def __init__(self, llm=None):
        """
        Initialize PromptImprovementAgent.

        Args:
            llm: Language model client (if None, will load default)
        """
        if llm:
            self.llm = llm
        else:
            logger.info("Loading LLM for PromptImprovementAgent")
            self.llm = load_gpt_model(temperature=0.3, max_tokens=600)

    def improve_prompts(
        self,
        judge_feedback: Dict,
        dialogue: list,
        current_prompts: Dict = None
    ) -> Dict:
        """
        Generate improved prompts based on judge feedback.

        Args:
            judge_feedback: Feedback dict from JudgeAgent
            dialogue: The dialogue that was evaluated
            current_prompts: Optional current prompt config

        Returns:
            Dict with improvement suggestions:
                - patient_improvements: str
                - doctor_improvements: str
                - general_improvements: str
        """
        logger.info("Generating prompt improvements from judge feedback...")

        # Extract key feedback points
        patient_feedback = judge_feedback.get('feedback_for_improvement', {}).get('patient_side', '')
        doctor_feedback = judge_feedback.get('feedback_for_improvement', {}).get('doctor_side', '')
        flow_feedback = judge_feedback.get('feedback_for_improvement', {}).get('conversation_flow', '')
        score = judge_feedback.get('score', 0.0)

        # Extract deepeval sub-scores for targeted diagnosis
        deepeval_scores = judge_feedback.get('deepeval_scores', {})
        profile_type = deepeval_scores.get('profile_type', 'NO_DIAGNOSIS_NO_TREATMENT')

        # Build improvement prompt — enriched with sub-score breakdown
        messages = [
            {"role": "system", "content": PROMPT_IMPROVEMENT_PROMPT},
            {"role": "user", "content": self._build_improvement_request(
                judge_feedback, dialogue, patient_feedback, doctor_feedback,
                flow_feedback, deepeval_scores, profile_type
            )}
        ]

        try:
            response = chat_generate(self.llm, messages)
            improvements = self._parse_improvements(response)
            logger.info("Generated prompt improvements successfully")
            return improvements

        except Exception as e:
            logger.error(f"Error generating improvements: {e}")
            # Return generic improvements
            return self._fallback_improvements(judge_feedback)

    def _build_improvement_request(
        self,
        judge_feedback: Dict,
        dialogue: list,
        patient_feedback: str,
        doctor_feedback: str,
        flow_feedback: str,
        deepeval_scores: Dict = None,
        profile_type: str = "NO_DIAGNOSIS_NO_TREATMENT",
    ) -> str:
        """Build the improvement request prompt with deepeval sub-score diagnosis."""
        deepeval_scores = deepeval_scores or {}
        dialogue_snippet = "\n".join([
            f"{turn['role']}: {turn['content'][:100]}..."
            for turn in dialogue[:4]
        ])

        # Determine which metric is the primary bottleneck
        nat = deepeval_scores.get('naturalness', None)
        comp = deepeval_scores.get('profile_compliance', None)
        faith = deepeval_scores.get('ragas_faithfulness', None)

        sub_score_section = ""
        bottleneck_hint = ""
        if nat is not None and comp is not None and faith is not None:
            sub_score_section = (
                f"\nDeepEval Sub-Scores (profile type: {profile_type}):\n"
                f"- Naturalness:          {nat:.2f}\n"
                f"- Profile Compliance:   {comp:.2f}\n"
                f"- RAGAS Faithfulness:   {faith:.2f}\n"
            )
            # Identify the lowest scoring metric to prioritise
            worst = min(nat, comp, faith)
            if worst == comp and comp < 0.70:
                knowledge = PATIENT_PROFILE_TYPE_KNOWLEDGE.get(
                    profile_type, PATIENT_PROFILE_TYPE_KNOWLEDGE["NO_DIAGNOSIS_NO_TREATMENT"]
                )
                bottleneck_hint = (
                    f"\n⚠️ PRIMARY BOTTLENECK — Profile Compliance ({comp:.2f}): "
                    f"The patient is disclosing information outside their {profile_type} knowledge boundary. "
                    f"Rules: {knowledge['disclosure_rules']} "
                    f"Your patient improvement MUST specifically address this boundary violation."
                )
            elif worst == faith and faith < 0.70:
                bottleneck_hint = (
                    f"\n⚠️ PRIMARY BOTTLENECK — RAGAS Faithfulness ({faith:.2f}): "
                    "Patient statements are not grounded in the profile. "
                    "Your patient improvement MUST specifically address hallucination of symptoms, "
                    "history, or other details not present in the profile."
                )
            elif worst == nat and nat < 0.60:
                bottleneck_hint = (
                    f"\n⚠️ PRIMARY BOTTLENECK — Naturalness ({nat:.2f}): "
                    "Dialogue sounds robotic or formulaic. "
                    "Focus improvements on varied phrasing, natural hesitations, and conversation flow."
                )

        request = f"""Judge Evaluation Results:
- Score: {judge_feedback.get('score', 0.0):.2f}
- Decision: {judge_feedback.get('decision', 'UNREALISTIC')}
- Justification: {judge_feedback.get('justification', 'N/A')}
{sub_score_section}{bottleneck_hint}

Specific Feedback:
- Patient Side: {patient_feedback}
- Doctor Side: {doctor_feedback}
- Conversation Flow: {flow_feedback}

Dialogue Sample (first 4 turns):
{dialogue_snippet}

Based on this feedback, provide specific improvements for:
1. Patient agent prompts/behavior (respecting profile type {profile_type} boundaries)
2. Doctor agent prompts/behavior
3. Overall dialogue orchestration

IMPORTANT: Maintain all bias-aware and grounding constraints. Only adjust stylistic and structural elements.

Provide your response in JSON format:
{{
  "patient_improvements": "Specific suggestions for patient agent",
  "doctor_improvements": "Specific suggestions for doctor agent",
  "general_improvements": "General dialogue improvements"
}}"""

        return request

    def _parse_improvements(self, response: str) -> Dict:
        """Parse LLM response into improvement dict."""
        try:
            # Try JSON parsing
            import re
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                improvements = json.loads(json_match.group(0))
                return {
                    "patient_improvements": improvements.get('patient_improvements', ''),
                    "doctor_improvements": improvements.get('doctor_improvements', ''),
                    "general_improvements": improvements.get('general_improvements', '')
                }
        except:
            logger.warning("Failed to parse improvements as JSON, using text extraction")

        # Fallback: extract sections from text
        patient_imp = self._extract_section(response, ['patient', 'patient agent', 'patient side'])
        doctor_imp = self._extract_section(response, ['doctor', 'doctor agent', 'physician'])
        general_imp = self._extract_section(response, ['general', 'overall', 'dialogue'])

        return {
            "patient_improvements": patient_imp or "Be more natural and gradual in symptom disclosure",
            "doctor_improvements": doctor_imp or "Ask more focused, empathetic questions",
            "general_improvements": general_imp or "Improve natural conversation flow"
        }

    def _extract_section(self, text: str, keywords: list) -> str:
        """Extract section from text based on keywords."""
        text_lower = text.lower()
        for keyword in keywords:
            idx = text_lower.find(keyword)
            if idx >= 0:
                # Extract next 150 characters
                section = text[idx:idx+200]
                return section.strip()
        return ""

    def _fallback_improvements(self, judge_feedback: Dict) -> Dict:
        """Generate fallback improvements using sub-scores when LLM call fails."""
        score = judge_feedback.get('score', 0.0)
        deepeval = judge_feedback.get('deepeval_scores', {})
        profile_type = deepeval.get('profile_type', 'NO_DIAGNOSIS_NO_TREATMENT')
        nat = deepeval.get('naturalness', score)
        comp = deepeval.get('profile_compliance', score)
        faith = deepeval.get('ragas_faithfulness', score)

        knowledge = PATIENT_PROFILE_TYPE_KNOWLEDGE.get(
            profile_type, PATIENT_PROFILE_TYPE_KNOWLEDGE["NO_DIAGNOSIS_NO_TREATMENT"]
        )

        # Patient improvement: prioritise the worst sub-metric
        if comp < 0.60:
            patient_imp = (
                f"Profile compliance failure ({profile_type}): "
                f"{knowledge['disclosure_rules']} "
                "Ensure the patient STRICTLY follows these knowledge boundaries every turn."
            )
        elif faith < 0.70:
            patient_imp = (
                "RAGAS faithfulness failure: patient is hallucinating details not in the profile. "
                "Only mention symptoms, history, and medications explicitly listed in the profile. "
                "When uncertain about a detail, say so rather than guessing."
            )
        elif nat < 0.60:
            patient_imp = (
                "Naturalness failure: responses sound formulaic. "
                "Vary sentence starters. Use hesitation only when genuinely uncertain. "
                "Keep responses brief (1-3 sentences) and conversational."
            )
        elif score < 0.7:
            patient_imp = "Reveal symptoms more gradually across turns. Add natural hesitations and uncertainty."
        else:
            patient_imp = "Minor refinements to naturalness and timing of disclosures."

        if nat < 0.60:
            doctor_imp = (
                "Doctor responses are too formulaic. "
                "Vary openings — avoid 'Thank you for sharing' or 'I understand' every turn. "
                "Ask ONE focused question per turn; build naturally on what the patient says."
            )
        elif score < 0.7:
            doctor_imp = "Reference earlier patient statements. Use transitions between questions. Show more empathy."
        else:
            doctor_imp = "Fine-tune empathy and question pacing."

        if score < 0.4:
            general_imp = "Build trust gradually. Avoid medical jargon early. Ensure natural turn-taking."
        elif score < 0.7:
            general_imp = "Improve conversation flow and continuity. Make dialogue feel more natural."
        else:
            general_imp = "Small improvements to overall flow and realism."

        return {
            "patient_improvements": patient_imp,
            "doctor_improvements": doctor_imp,
            "general_improvements": general_imp,
        }
