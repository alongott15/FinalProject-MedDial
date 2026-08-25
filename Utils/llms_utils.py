"""Compatibility imports for the provider-independent LLM layer."""

from meddial.llm import (
    AzureAIFoundryClient,
    AzureAIFoundryProvider,
    ChatMessage,
    LLMCallMetadata,
    LLMProvider,
    LLMProviderError,
    LLMResponse,
    MockLLMProvider,
    chat_generate,
    load_gpt_model,
)

__all__ = [
    "AzureAIFoundryClient",
    "AzureAIFoundryProvider",
    "ChatMessage",
    "LLMCallMetadata",
    "LLMProvider",
    "LLMProviderError",
    "LLMResponse",
    "MockLLMProvider",
    "chat_generate",
    "load_gpt_model",
]
