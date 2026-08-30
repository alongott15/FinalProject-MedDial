"""Provider for a locally served OpenAI-compatible endpoint (Ollama, vLLM).

Decision D2: inference is served locally, so this is the only provider
approved for ``RESTRICTED_CLINICAL`` payloads — MIMIC-derived text never
leaves the host.

The digest of the served weights is required at construction. A tag such as
``llama3.1:8b`` is mutable and cannot identify which artefact produced a
number in the paper; :func:`resolve_ollama_digest` reads the immutable
digest from a running Ollama server so a config layer can supply it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

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

_APPROVED = frozenset(DataClassification)


def resolve_ollama_digest(base_url: str, model_id: str, *, timeout_s: float = 30.0) -> str:
    """Return the manifest digest Ollama reports for ``model_id``.

    Raises :class:`ProviderConfigurationError` if the server does not know
    the model or reports no digest, because a run without a digest is not
    reproducible and must not start.
    """
    ensure_network_calls_allowed("resolve_ollama_digest")
    show_url = base_url.rstrip("/").removesuffix("/v1") + "/api/show"
    try:
        response = httpx.post(show_url, json={"name": model_id}, timeout=timeout_s)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise ProviderConfigurationError(
            f"Could not resolve a weight digest for {model_id!r} from {show_url}: {exc}"
        ) from exc

    digest = payload.get("digest") or (payload.get("details") or {}).get("digest")
    if not digest:
        raise ProviderConfigurationError(
            f"Ollama reported no digest for {model_id!r}; cannot record provenance."
        )
    return str(digest)


class LocalOpenAICompatibleProvider:
    """Chat completions against a local ``/v1/chat/completions`` endpoint."""

    def __init__(
        self,
        base_url: str,
        model_id: str,
        *,
        model_digest: str,
        model_family: str,
        quantisation: str,
        top_p: float = 1.0,
        timeout_s: float = 300.0,
        max_retries: int = 3,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url:
            raise ProviderConfigurationError("base_url is required.")
        if not model_digest:
            raise ProviderConfigurationError(
                f"model_digest is required for {model_id!r} (EXP-7): a tag is "
                "mutable and cannot identify the weights behind a result."
            )
        self._base_url = base_url.rstrip("/")
        self._model_id = model_id
        self._model_digest = model_digest
        self._model_family = model_family
        self._quantisation = quantisation
        self._top_p = top_p
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._client = client
        self._sleep = sleep

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
        ensure_provider_compatible(self, classification)
        ensure_network_calls_allowed(type(self).__name__)

        payload: dict[str, Any] = {
            "model": self._model_id,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "top_p": self._top_p,
            "max_tokens": max_tokens,
        }
        if seed is not None:
            payload["seed"] = seed

        started = time.monotonic()
        data = self._post_with_retries(payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        text = self._extract_text(data)
        usage = data.get("usage") or {}
        return CompletionResult(
            text=text,
            metadata=CallMetadata(
                model_id=self._model_id,
                model_digest=self._model_digest,
                model_family=self._model_family,
                quantisation=self._quantisation,
                temperature=temperature,
                top_p=self._top_p,
                seed=seed,
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                latency_ms=latency_ms,
                provider_class=type(self).__name__,
                classification=classification,
            ),
        )

    def _post_with_retries(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/chat/completions"
        client = self._client or httpx.Client(timeout=self._timeout_s)
        owns_client = self._client is None
        try:
            for attempt in range(self._max_retries):
                try:
                    response = client.post(url, json=payload, timeout=self._timeout_s)
                except httpx.TimeoutException as exc:
                    if attempt == self._max_retries - 1:
                        raise ProviderTimeoutError(
                            f"{self._model_id} did not respond within "
                            f"{self._timeout_s}s after {self._max_retries} attempts."
                        ) from exc
                    self._sleep(2**attempt)
                    continue
                except httpx.HTTPError as exc:
                    raise ProviderResponseError(
                        f"Transport failure calling {url}: {exc}"
                    ) from exc

                if response.status_code == 429:
                    if attempt == self._max_retries - 1:
                        raise ProviderRateLimitError(
                            f"{url} rate-limited the request after "
                            f"{self._max_retries} attempts."
                        )
                    self._sleep(2**attempt)
                    continue

                if response.status_code >= 400:
                    raise ProviderResponseError(
                        f"{url} returned HTTP {response.status_code}: "
                        f"{response.text[:200]}"
                    )

                try:
                    return dict(response.json())
                except ValueError as exc:
                    raise ProviderResponseError(
                        f"{url} returned a non-JSON body."
                    ) from exc

            raise ProviderResponseError(
                f"Exhausted {self._max_retries} attempts against {url}."
            )
        finally:
            if owns_client:
                client.close()

    def _extract_text(self, data: dict[str, Any]) -> str:
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(
                f"{self._model_id} returned a response without a message body."
            ) from exc
        if not isinstance(text, str) or not text.strip():
            raise ProviderResponseError(
                f"{self._model_id} returned an empty completion."
            )
        return text
