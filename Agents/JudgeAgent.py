import logging
import json
import re
from typing import Dict, Tuple
from Utils.llms_utils import load_gpt_model, chat_generate
from Utils.bias_aware_prompts import JUDGE_AGENT_PROMPT
from Utils.mts_dialog_loader import load_mts_dialog_examples

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class JudgeAgent:
    """
    LLM-based judge that evaluates dialogue naturalness and groundedness.

    Key responsibilities:
    - Decide if dialogue is REALISTIC or UNREALISTIC
    - Provide numeric score (0.0-1.0)
    - Give actionable feedback for improvement
    - Check for hallucinations and unsupported content
    """

    def __init__(self, llm=None, few_shot_examples: list = None, threshold: float = 0.70, mts_dialog_csv_path: str = None):
        if llm:
            self.llm = llm
        else:
            logger.info("Loading LLM for JudgeAgent")
            self.llm = load_gpt_model(temperature=0.1, max_tokens=800)

        if mts_dialog_csv_path:
            logger.info(f"Loading MTS-Dialog examples from {mts_dialog_csv_path}")
            mts_examples = load_mts_dialog_examples(mts_dialog_csv_path, max_examples=3)
            self.few_shot_examples = mts_examples
        else:
            self.few_shot_examples = few_shot_examples or []

        self.threshold = threshold
        logger.info(f"JudgeAgent initialized with threshold={threshold}, examples={len(self.few_shot_examples)}")

    def evaluate_dialogue(
        self,
        dialogue: list,
        patient_profile: dict,
        dialogue_transcript: str = None
    ) -> Dict:
        """
        Evaluate a dialogue for naturalness and groundedness.

        Args:
            dialogue: List of dialogue turns (dicts with 'role' and 'content')
            patient_profile: Full patient profile (GTMF)
            dialogue_transcript: Optional formatted transcript string

        Returns:
            Dict with keys:
                - decision: "REALISTIC" or "UNREALISTIC"
                - score: float 0.0-1.0
                - justification: str
                - feedback_for_improvement: dict with patient_side, doctor_side, etc.
        """
        logger.info("JudgeAgent evaluating dialogue...")

        # Format dialogue if not provided
        if dialogue_transcript is None:
            dialogue_transcript = self._format_dialogue(dialogue)

        # Format patient profile
        profile_summary = self._format_profile(patient_profile)

        # Build evaluation prompt
        messages = [
            {"role": "system", "content": JUDGE_AGENT_PROMPT},
            {"role": "user", "content": self._build_evaluation_prompt(
                dialogue_transcript, profile_summary
            )}
        ]

        # Get LLM response
        try:
            response = chat_generate(self.llm, messages)
            logger.debug(f"Judge response: {response[:200]}...")

            # Parse response
            evaluation = self._parse_evaluation_response(response)

            # Apply threshold to determine decision
            if evaluation['score'] >= self.threshold:
                evaluation['decision'] = "REALISTIC"
            else:
                evaluation['decision'] = "UNREALISTIC"

            logger.info(f"Judge decision: {evaluation['decision']} (score: {evaluation['score']:.2f})")
            return evaluation

        except Exception as e:
            logger.error(f"Error in JudgeAgent evaluation: {e}")
            # Return conservative default
            return {
                "decision": "UNREALISTIC",
                "score": 0.0,
                "justification": f"Evaluation failed: {str(e)}",
                "feedback_for_improvement": {
                    "patient_side": "Unable to evaluate",
                    "doctor_side": "Unable to evaluate",
                    "conversation_flow": "Unable to evaluate"
                }
            }

    def _format_dialogue(self, dialogue: list) -> str:
        """Format dialogue turns into readable transcript."""
        lines = []
        for turn in dialogue:
            role = turn.get('role', 'Unknown')
            content = turn.get('content', '')
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def _format_profile(self, profile: dict) -> str:
        """Format patient profile for judge."""
        core_fields = profile.get('Core_Fields', {})
        context_fields = profile.get('Context_Fields', {})
        additional_context = profile.get('Additional_Context', {})

        # Extract key information
        symptoms = core_fields.get('Symptoms', [])
        symptom_list = [s.get('description', '') for s in symptoms if isinstance(s, dict)]

        diagnoses = core_fields.get('Diagnoses', [])
        diagnosis_list = [d.get('primary', '') for d in diagnoses if isinstance(d, dict)]

        demographics = context_fields.get('Patient_Demographics', {})
        age = demographics.get('Age', 'Unknown')
        sex = demographics.get('Sex', 'Unknown')

        chief_complaint = additional_context.get('Chief_Complaint', 'Not specified')

        profile_text = f"""Patient Profile Summary:
- Age: {age}, Sex: {sex}
- Chief Complaint: {chief_complaint}
- Symptoms: {', '.join(symptom_list) if symptom_list else 'None specified'}
- Diagnoses: {', '.join(diagnosis_list) if diagnosis_list else 'None specified'}
- Case Type: Light, common symptoms"""

        return profile_text

    def _build_evaluation_prompt(self, dialogue_transcript: str, profile_summary: str) -> str:
        """Build the evaluation prompt with few-shot examples if available."""
        prompt_parts = []

        # Add few-shot examples if available
        if self.few_shot_examples:
            prompt_parts.append("Here are examples of REALISTIC medical dialogues:\n")
            for i, example in enumerate(self.few_shot_examples[:2], 1):
                prompt_parts.append(f"Example {i}:\n{example}\n")
            prompt_parts.append("\n")

        # Add the dialogue to evaluate
        prompt_parts.append(f"{profile_summary}\n\n")
        prompt_parts.append("Dialogue to Evaluate:\n")
        prompt_parts.append(dialogue_transcript)
        prompt_parts.append("\n\nProvide your evaluation in the following JSON format:\n")
        prompt_parts.append("""{
  "decision": "REALISTIC or UNREALISTIC",
  "score": 0.0-1.0,
  "justification": "Brief explanation",
  "feedback_for_improvement": {
    "patient_side": "Specific feedback for patient agent",
    "doctor_side": "Specific feedback for doctor agent",
    "conversation_flow": "Overall dialogue flow feedback",
    "safety_or_clarity": "Any safety or clarity concerns"
  }
}""")

        return "\n".join(prompt_parts)

    def _parse_evaluation_response(self, response: str) -> Dict:
        """Parse LLM evaluation response into structured format."""
        # Try to extract JSON
        try:
            # Look for JSON block
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                eval_dict = json.loads(json_match.group(0))

                # Validate and extract fields
                decision = eval_dict.get('decision', 'UNREALISTIC')
                score = float(eval_dict.get('score', 0.0))
                score = max(0.0, min(1.0, score))  # Clamp to [0,1]
                justification = eval_dict.get('justification', 'No justification provided')
                feedback = eval_dict.get('feedback_for_improvement', {})

                return {
                    "decision": decision,
                    "score": score,
                    "justification": justification,
                    "feedback_for_improvement": feedback
                }
        except Exception as e:
            logger.warning(f"Failed to parse JSON from judge response: {e}")

        # Fallback: heuristic parsing
        score = 0.5
        decision = "UNREALISTIC"
        justification = response[:200]

        # Try to extract score from text
        score_patterns = [
            r'score[:\s]*(\d+\.?\d*)',
            r'(\d+\.?\d*)\s*/\s*1\.?0?',
            r'rating[:\s]*(\d+\.?\d*)'
        ]
        for pattern in score_patterns:
            match = re.search(pattern, response.lower())
            if match:
                try:
                    score = float(match.group(1))
                    if score > 1.0:  # Handle cases like "8/10"
                        score = score / 10.0
                    score = max(0.0, min(1.0, score))
                    break
                except:
                    pass

        # Determine decision from keywords
        if any(word in response.lower() for word in ['realistic', 'natural', 'good', 'appropriate']):
            if score >= 0.5:
                decision = "REALISTIC"

        feedback = {
            "patient_side": "See justification",
            "doctor_side": "See justification",
            "conversation_flow": "See justification",
            "safety_or_clarity": "See justification"
        }

        return {
            "decision": decision,
            "score": score,
            "justification": justification,
            "feedback_for_improvement": feedback
        }

    def set_few_shot_examples(self, examples: list):
        """Update few-shot examples."""
        self.few_shot_examples = examples
        logger.info(f"Updated judge with {len(examples)} few-shot examples")