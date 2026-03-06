import logging
from typing import Dict
from Utils.llms_utils import load_gpt_model, chat_generate
from Utils.bias_aware_prompts import EHR_SUMMARIZER_PROMPT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class EHRSummarizerAgent:
    """
    Summarizes EHR/clinical notes using bias-aware prompts.

    Only extracts information clearly present in the text.
    """

    def __init__(self, llm=None):
        """
        Initialize EHRSummarizerAgent.

        Args:
            llm: Language model client (if None, will load default)
        """
        if llm:
            self.llm = llm
        else:
            logger.info("Loading LLM for EHRSummarizerAgent")
            self.llm = load_gpt_model(temperature=0.1, max_tokens=400)

    def summarize(self, ehr_text: str, metadata: Dict = None) -> str:
        """
        Summarize EHR text.

        Args:
            ehr_text: Full clinical note text
            metadata: Optional metadata (age, sex, etc.)

        Returns:
            Summary string (5-8 sentences)
        """
        logger.info("Summarizing EHR text...")

        # Add metadata context if available
        metadata_str = ""
        if metadata:
            demographics = metadata.get('Patient_Demographics', {})
            if demographics:
                age = demographics.get('Age', 'Unknown')
                sex = demographics.get('Sex', 'Unknown')
                metadata_str = f"\nPatient: {age} year old {sex}"

        # Build prompt
        messages = [
            {"role": "system", "content": EHR_SUMMARIZER_PROMPT},
            {"role": "user", "content": f"""Clinical Note:{metadata_str}

{ehr_text[:2000]}

Provide a concise summary (5-8 sentences) covering:
- Main complaint
- Key symptoms
- Diagnosis (if documented)
- Basic treatment/advice

Summary:"""}
        ]

        try:
            summary = chat_generate(self.llm, messages)
            logger.info(f"EHR summary generated: {len(summary)} characters")
            return summary.strip()

        except Exception as e:
            logger.error(f"Error summarizing EHR: {e}")
            return "Unable to generate summary"
