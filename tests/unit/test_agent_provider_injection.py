"""Agent-level regression tests for D-08 and GOV-4.

The unit tests in ``test_llm_provider.py`` prove the provider layer raises.
These prove the agents let it raise, instead of substituting a placeholder
string that would flow into a transcript and be scored as content.
"""

from __future__ import annotations

import pytest

from Agents.EHRSummarizerAgent import EHRSummarizerAgent
from meddial.llm import (
    DataClassification,
    MockProvider,
    ProviderClassificationError,
    ProviderResponseError,
)

NOTE = "Patient presented with substernal chest pain radiating to the left arm."


def test_summarizer_requires_an_injected_provider() -> None:
    """GOV-4: no agent may construct its own model client."""
    with pytest.raises(TypeError):
        EHRSummarizerAgent()  # type: ignore[call-arg]


def test_summarizer_returns_the_provider_text() -> None:
    provider = MockProvider(["Chest pain, ruled out MI."])
    assert EHRSummarizerAgent(provider).summarize(NOTE) == "Chest pain, ruled out MI."


def test_summarizer_sends_clinical_text_as_restricted() -> None:
    """C2: a MIMIC note must be labelled so the gate can act on it."""
    provider = MockProvider(["summary"])
    EHRSummarizerAgent(provider).summarize(NOTE)

    assert provider.calls[0].classification is DataClassification.RESTRICTED_CLINICAL


def test_summarizer_raises_instead_of_returning_a_placeholder() -> None:
    """D-08: the old code returned "Unable to generate summary" here."""
    provider = MockProvider(failure=ProviderResponseError("model returned nothing"))

    with pytest.raises(ProviderResponseError):
        EHRSummarizerAgent(provider).summarize(NOTE)


def test_summarizer_cannot_reach_a_provider_barred_from_clinical_data() -> None:
    provider = MockProvider(
        ["summary"], approved=frozenset({DataClassification.SYNTHETIC})
    )

    with pytest.raises(ProviderClassificationError):
        EHRSummarizerAgent(provider).summarize(NOTE)
