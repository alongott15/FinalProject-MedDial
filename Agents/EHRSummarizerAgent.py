import logging
from typing import Dict
from meddial.llm import DataClassification, LLMProvider, to_chat_messages
from Utils.bias_aware_prompts import EHR_SUMMARIZER_PROMPT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class EHRSummarizerAgent:
    """
    Summarizes EHR/clinical notes using bias-aware prompts.

    Only extracts information clearly present in the text.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        temperature: float = 0.1,
        max_tokens: int = 400,
        seed: int | None = None,
    ):
        """
        Initialize EHRSummarizerAgent.

        Args:
            provider: Provider to use. Injected, never constructed here, so the
                run manifest records one model configuration for the whole run.
            temperature: Sampling temperature.
            max_tokens: Completion budget.
            seed: Sampling seed, when the provider honours one.
        """
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._seed = seed

    def summarize(self, ehr_text: str, metadata: Dict = None) -> str:
        """
        Summarize EHR text.

        Args:
            ehr_text: Full clinical note text
            metadata: Optional metadata (age, sex, etc.)

        Returns:
            Summary string (5-8 sentences)

        Raises:
            ProviderError: If the model call fails. The failure is not caught
                here — a placeholder summary would silently become the
                grounding for a whole dialogue (D-08).
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

        result = self._provider.complete(
            to_chat_messages(messages),
            # The note is MIMIC-III text, so only a local provider may see it.
            classification=DataClassification.RESTRICTED_CLINICAL,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            seed=self._seed,
        )
        logger.info(f"EHR summary generated: {len(result.text)} characters")
        return result.text.strip()
