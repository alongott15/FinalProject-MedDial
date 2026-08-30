"""Provider for Azure AI Foundry.

Constraint C2: MIMIC-III notes and everything derived from them must not
reach a third-party API that retains data. This provider is therefore
approved for ``PUBLIC`` and ``SYNTHETIC`` payloads only, and never for
``RESTRICTED_CLINICAL``.

The classification gate and the credential check both run before the SDK
client is constructed, so a restricted call cannot open a socket. The SDK
is an optional dependency (``pip install -e ".[azure]"``) and is imported
lazily for the same reason.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from typing import Any

from .classification import DataClassification, ensure_provider_compatible
from .errors import (
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from .provider import (
    CallMetadata,
    ChatMessage,
    CompletionResult,
    ensure_network_calls_allowed,
)

_APPROVED = frozenset({DataClassification.PUBLIC, DataClassification.SYNTHETIC})


class AzureProvider:
    """Chat completions against an Azure AI Foundry deployment.

    ``model_digest`` cannot be derived from a hosted deployment, so the
    caller supplies whatever identifier the service exposes (typically the
    deployment name plus a dated model version). Recording it as ``hosted:``
    keeps it visibly distinct from a real weight hash in the run manifest.
    """

    def __init__(
        self,
        model_id: str,
        *,
        model_family: str,
        endpoint: str | None = None,
        api_key: str | None = None,
        model_version: str = "unknown",
        top_p: float = 1.0,
        timeout_s: float = 120.0,
        client: Any | None = None,
    ) -> None:
        self._model_id = model_id
        self._model_family = model_family
        self._endpoint = endpoint or os.getenv("AZURE_AI_ENDPOINT")
        self._api_key = api_key or os.getenv("AZURE_AI_API_KEY")
        self._model_version = model_version
        self._top_p = top_p
        self._timeout_s = timeout_s
        self._client = client

    @property
    def approved_classifications(self) -> frozenset[DataClassification]:
        return _APPROVED

    @property
    def model_family(self) -> str:
        return self._model_family

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        classification: DataClassification,
        temperature: float,
        max_tokens: int,
        seed: int | None = None,
    ) -> CompletionResult:
        # Order matters: both checks must precede client construction.
        ensure_provider_compatible(self, classification)
        ensure_network_calls_allowed(type(self).__name__)

        client = self._client or self._build_client()
        sdk_messages = self._to_sdk_messages(messages)

        started = time.monotonic()
        response = self._invoke(client, sdk_messages, temperature, max_tokens, seed)
        latency_ms = int((time.monotonic() - started) * 1000)

        text = self._extract_text(response)
        usage = getattr(response, "usage", None)
        return CompletionResult(
            text=text,
            metadata=CallMetadata(
                model_id=self._model_id,
                model_digest=f"hosted:{self._model_id}@{self._model_version}",
                model_family=self._model_family,
                quantisation="unknown",
                temperature=temperature,
                top_p=self._top_p,
                seed=seed,
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                latency_ms=latency_ms,
                provider_class=type(self).__name__,
                classification=classification,
            ),
        )

    def _build_client(self) -> Any:
        if not self._endpoint or not self._api_key:
            raise ProviderConfigurationError(
                "AzureProvider needs AZURE_AI_ENDPOINT and AZURE_AI_API_KEY."
            )
        try:
            from azure.ai.inference import ChatCompletionsClient
            from azure.core.credentials import AzureKeyCredential
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ProviderConfigurationError(
                'azure-ai-inference is not installed; run pip install -e ".[azure]".'
            ) from exc

        return ChatCompletionsClient(
            endpoint=self._endpoint,
            credential=AzureKeyCredential(self._api_key),
        )

    @staticmethod
    def _to_sdk_messages(messages: Sequence[ChatMessage]) -> list[dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in messages]

    def _invoke(
        self,
        client: Any,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        seed: int | None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "model": self._model_id,
            "temperature": temperature,
            "top_p": self._top_p,
            "max_tokens": max_tokens,
        }
        if seed is not None:
            kwargs["seed"] = seed
        try:
            return client.complete(**kwargs)
        except Exception as exc:
            raise self._normalise(exc) from exc

    @staticmethod
    def _normalise(exc: Exception) -> Exception:
        status = getattr(exc, "status_code", None)
        if status == 429:
            return ProviderRateLimitError("Azure rate-limited the request.")
        if status == 408 or isinstance(exc, TimeoutError):
            return ProviderTimeoutError("Azure did not respond in time.")
        if status is not None:
            return ProviderResponseError(f"Azure returned HTTP {status}.")
        return ProviderResponseError(f"Azure call failed: {exc}")

    def _extract_text(self, response: Any) -> str:
        try:
            text = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise ProviderResponseError(
                f"{self._model_id} returned a response without a message body."
            ) from exc
        if not isinstance(text, str) or not text.strip():
            raise ProviderResponseError(f"{self._model_id} returned an empty completion.")
        return text
