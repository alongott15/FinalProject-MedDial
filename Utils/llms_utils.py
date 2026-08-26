"""Compatibility imports for the provider-independent LLM layer."""

from meddial.llm import (
    AzureAIFoundryClient,
    AzureAIFoundryProvider,
    ChatMessage,
    DataBoundaryError,
    DataClassification,
    LLMCallMetadata,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    LocalOpenAICompatibleProvider,
    MockLLMProvider,
    ProviderBoundary,
    chat_generate,
    ensure_provider_compatible,
    load_gpt_model,
    load_restricted_clinical_model,
)

__all__ = [
    "AzureAIFoundryClient",
    "AzureAIFoundryProvider",
    "ChatMessage",
    "DataBoundaryError",
    "DataClassification",
    "LLMCallMetadata",
    "LLMProvider",
    "LLMProviderError",
    "LLMResponse",
    "LocalOpenAICompatibleProvider",
    "MockLLMProvider",
    "ProviderBoundary",
    "chat_generate",
    "ensure_provider_compatible",
    "load_gpt_model",
    "load_restricted_clinical_model",
]
