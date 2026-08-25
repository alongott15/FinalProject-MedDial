"""Provider-independent LLM interface with per-call metadata recording."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class LLMCallMetadata:
    provider: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    started_at: str = ""
    latency_ms: float | None = None
    request_id: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LLMResponse:
    content: str
    metadata: LLMCallMetadata


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    def generate(self, messages: Sequence[ChatMessage]) -> LLMResponse: ...


class LLMProviderError(RuntimeError):
    pass


class AzureAIFoundryProvider:
    """Azure adapter; Azure packages are imported only when instantiated."""

    provider_name = "azure_ai_foundry"

    def __init__(
        self,
        model_name: str = "gpt-4.1",
        temperature: float = 0.3,
        max_tokens: int = 512,
        endpoint: str | None = None,
        api_key: str | None = None,
        max_retries: int = 5,
        metadata_sink: Callable[[LLMCallMetadata], None] | None = None,
    ) -> None:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        if os.getenv("MEDDIAL_DISABLE_EXTERNAL_CALLS", "").lower() in {"1", "true", "yes"}:
            raise LLMProviderError("External model calls are disabled in this environment")
        try:
            from azure.ai.inference import ChatCompletionsClient
            from azure.core.credentials import AzureKeyCredential
        except ImportError as exc:
            raise LLMProviderError(
                "Azure support is not installed. Install MedDial with the 'azure' extra."
            ) from exc

        self._model_name = model_name
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.endpoint = endpoint or os.getenv("AZURE_AI_ENDPOINT")
        self.api_key = api_key or os.getenv("AZURE_AI_API_KEY")
        self.metadata_sink = metadata_sink
        self.call_history: list[LLMCallMetadata] = []
        if not self.endpoint or not self.api_key:
            raise LLMProviderError("AZURE_AI_ENDPOINT and AZURE_AI_API_KEY must be set")
        self.client = ChatCompletionsClient(
            endpoint=self.endpoint, credential=AzureKeyCredential(self.api_key)
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    def generate(self, messages: Sequence[ChatMessage]) -> LLMResponse:
        from azure.ai.inference.models import AssistantMessage, SystemMessage, UserMessage
        from azure.core.exceptions import HttpResponseError

        azure_messages = []
        message_types = {
            "system": SystemMessage,
            "assistant": AssistantMessage,
            "user": UserMessage,
        }
        for message in messages:
            azure_type = message_types.get(message.role.lower(), UserMessage)
            azure_messages.append(azure_type(content=message.content))

        started = datetime.now(timezone.utc).isoformat()
        start = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self.client.complete(
                    messages=azure_messages,
                    model=self.model_name,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    top_p=0.95,
                    frequency_penalty=0.1,
                    presence_penalty=0.1,
                )
                choice = response.choices[0]
                usage_obj = getattr(response, "usage", None)
                usage = {
                    key: int(getattr(usage_obj, key))
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                    if usage_obj is not None and getattr(usage_obj, key, None) is not None
                }
                metadata = LLMCallMetadata(
                    provider=self.provider_name,
                    model=self.model_name,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    started_at=started,
                    latency_ms=(time.perf_counter() - start) * 1000,
                    request_id=getattr(response, "id", None),
                    usage=usage,
                )
                self.call_history.append(metadata)
                if self.metadata_sink:
                    self.metadata_sink(metadata)
                return LLMResponse(content=choice.message.content.strip(), metadata=metadata)
            except HttpResponseError as exc:
                last_error = exc
                if getattr(exc, "status_code", None) != 429:
                    break
                time.sleep((2**attempt) + (attempt * 0.5))
            except Exception as exc:  # provider SDK exceptions vary by version
                last_error = exc
                break
        raise LLMProviderError(f"Azure generation failed: {last_error}") from last_error


class MockLLMProvider:
    """Deterministic provider for tests; never performs network calls."""

    def __init__(
        self,
        responses: Sequence[str] | Callable[[Sequence[ChatMessage]], str],
        model_name: str = "mock-model",
    ) -> None:
        self._responses = responses
        self._model_name = model_name
        self.calls: list[tuple[ChatMessage, ...]] = []
        self.call_history: list[LLMCallMetadata] = []
        self._index = 0

    @property
    def model_name(self) -> str:
        return self._model_name

    def generate(self, messages: Sequence[ChatMessage]) -> LLMResponse:
        frozen = tuple(messages)
        self.calls.append(frozen)
        if callable(self._responses):
            content = self._responses(messages)
        else:
            if self._index >= len(self._responses):
                raise LLMProviderError("Mock response sequence exhausted")
            content = self._responses[self._index]
            self._index += 1
        metadata = LLMCallMetadata(provider="mock", model=self.model_name)
        self.call_history.append(metadata)
        return LLMResponse(
            content=content,
            metadata=metadata,
        )


# Backward-compatible name retained for existing imports.
AzureAIFoundryClient = AzureAIFoundryProvider


def load_gpt_model(
    model_name: str = "gpt-4.1", temperature: float = 0.3, max_tokens: int = 512
) -> LLMProvider:
    return AzureAIFoundryProvider(
        model_name=model_name, temperature=temperature, max_tokens=max_tokens
    )


def chat_generate(
    llm: LLMProvider, messages: Sequence[Mapping[str, str]], max_retries: int = 5
) -> str:
    """Compatibility wrapper used by legacy agents.

    Retry ownership belongs to the provider. ``max_retries`` remains in the
    signature so existing callers do not break.
    """
    del max_retries
    normalized = [ChatMessage(role=m["role"], content=m["content"]) for m in messages]
    return llm.generate(normalized).content
