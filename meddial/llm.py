"""Provider-independent LLM interface with per-call metadata recording."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class DataClassification(str, Enum):
    """Classification attached to content before it reaches a model provider."""

    PUBLIC_OR_SYNTHETIC = "public_or_synthetic"
    RESTRICTED_CLINICAL = "restricted_clinical"


class ProviderBoundary(str, Enum):
    """Where inference occurs relative to the approved research environment."""

    LOCAL_CONTROLLED = "local_controlled"
    EXTERNAL_SERVICE = "external_service"
    TEST_ONLY = "test_only"


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


class DataBoundaryError(LLMProviderError):
    """A provider is not permitted to receive the classified input."""


def ensure_provider_compatible(
    provider: LLMProvider,
    data_classification: DataClassification,
) -> None:
    """Fail closed when restricted clinical text could leave the controlled boundary."""

    boundary = getattr(provider, "provider_boundary", None)
    if data_classification is DataClassification.RESTRICTED_CLINICAL and boundary not in {
        ProviderBoundary.LOCAL_CONTROLLED,
        ProviderBoundary.TEST_ONLY,
    }:
        raise DataBoundaryError(
            "Restricted clinical content requires a provider that explicitly declares a "
            "local controlled or test-only boundary. Use LocalOpenAICompatibleProvider "
            "inside the approved research environment."
        )


class AzureAIFoundryProvider:
    """Azure adapter; Azure packages are imported only when instantiated."""

    provider_name = "azure_ai_foundry"
    provider_boundary = ProviderBoundary.EXTERNAL_SERVICE

    def __init__(
        self,
        model_name: str = "gpt-4.1",
        temperature: float = 0.3,
        max_tokens: int = 512,
        endpoint: str | None = None,
        api_key: str | None = None,
        max_retries: int = 5,
        metadata_sink: Callable[[LLMCallMetadata], None] | None = None,
        data_classification: DataClassification = DataClassification.PUBLIC_OR_SYNTHETIC,
    ) -> None:
        ensure_provider_compatible(self, data_classification)
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


def _is_controlled_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.strip("[]").lower()
    if normalized in {"localhost", "host.docker.internal"}:
        return True
    if normalized.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


class LocalOpenAICompatibleProvider:
    """Local/self-hosted adapter for vLLM and compatible inference servers.

    Public internet hosts are rejected by default. A non-local hostname may be
    enabled only when the caller has separately approved that private research
    endpoint; the decision is then recorded in call metadata.
    """

    provider_name = "local_openai_compatible"
    provider_boundary = ProviderBoundary.LOCAL_CONTROLLED

    def __init__(
        self,
        model_name: str,
        base_url: str = "http://127.0.0.1:8000/v1",
        temperature: float = 0.15,
        max_tokens: int = 1024,
        timeout_seconds: float = 120.0,
        api_key: str | None = None,
        approved_private_host: bool = False,
        metadata_sink: Callable[[LLMCallMetadata], None] | None = None,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise LLMProviderError("Local model base_url must be an HTTP(S) URL")
        if not _is_controlled_host(parsed.hostname) and not approved_private_host:
            raise DataBoundaryError(
                f"Model host {parsed.hostname!r} is not local/private. "
                "Set approved_private_host=True only for an institution-approved endpoint."
            )
        self._model_name = model_name
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key
        self.approved_private_host = approved_private_host
        self.metadata_sink = metadata_sink
        self.call_history: list[LLMCallMetadata] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    def generate(self, messages: Sequence[ChatMessage]) -> LLMResponse:
        payload = {
            "model": self.model_name,
            "messages": [asdict(message) for message in messages],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        started = datetime.now(timezone.utc).isoformat()
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise LLMProviderError(f"Local model generation failed: {exc}") from exc
        try:
            content = str(body["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(
                "Local model returned an invalid chat-completion response"
            ) from exc
        usage_payload = body.get("usage", {})
        usage = {
            key: int(value)
            for key, value in usage_payload.items()
            if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
            and isinstance(value, (int, float))
        }
        metadata = LLMCallMetadata(
            provider=self.provider_name,
            model=self.model_name,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            started_at=started,
            latency_ms=(time.perf_counter() - start) * 1000,
            request_id=str(body.get("id")) if body.get("id") else None,
            usage=usage,
            extra={
                "provider_boundary": self.provider_boundary.value,
                "approved_private_host": self.approved_private_host,
            },
        )
        self.call_history.append(metadata)
        if self.metadata_sink:
            self.metadata_sink(metadata)
        return LLMResponse(content=content, metadata=metadata)


class MockLLMProvider:
    """Deterministic provider for tests; never performs network calls."""

    provider_name = "mock"
    provider_boundary = ProviderBoundary.TEST_ONLY

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
        model_name=model_name,
        temperature=temperature,
        max_tokens=max_tokens,
        data_classification=DataClassification.PUBLIC_OR_SYNTHETIC,
    )


def load_restricted_clinical_model(
    model_name: str | None = None,
    temperature: float = 0.15,
    max_tokens: int = 1024,
) -> LLMProvider:
    """Load the local controlled provider used for MIMIC and its derivatives."""

    resolved_model = model_name
    if resolved_model is None:
        resolved_model = os.getenv("MEDDIAL_LOCAL_LLM_MODEL") or "gpt-oss-20b"
    return LocalOpenAICompatibleProvider(
        model_name=resolved_model,
        base_url=os.getenv("MEDDIAL_LOCAL_LLM_BASE_URL", "http://127.0.0.1:8000/v1"),
        api_key=os.getenv("MEDDIAL_LOCAL_LLM_API_KEY"),
        temperature=temperature,
        max_tokens=max_tokens,
        approved_private_host=os.getenv("MEDDIAL_APPROVED_PRIVATE_LLM_HOST", "").lower()
        in {"1", "true", "yes"},
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
