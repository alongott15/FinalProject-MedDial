"""Provider and compliance layer (W1).

Agents receive an :class:`LLMProvider` by constructor injection and never
construct one themselves, so a run's model configuration is decided in one
place and recorded in the manifest.
"""

from .classification import DataClassification, ensure_provider_compatible
from .errors import (
    ProviderClassificationError,
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from .local_openai import LocalOpenAICompatibleProvider, resolve_ollama_digest
from .mock import MockProvider
from .provider import (
    CallMetadata,
    ChatMessage,
    CompletionResult,
    LLMProvider,
    ensure_network_calls_allowed,
    to_chat_messages,
)

__all__ = [
    "CallMetadata",
    "ChatMessage",
    "CompletionResult",
    "DataClassification",
    "LLMProvider",
    "LocalOpenAICompatibleProvider",
    "MockProvider",
    "ProviderClassificationError",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderResponseError",
    "ProviderTimeoutError",
    "ensure_network_calls_allowed",
    "ensure_provider_compatible",
    "resolve_ollama_digest",
    "to_chat_messages",
]
