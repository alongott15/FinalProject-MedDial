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

import ipaddress
import time
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from .classification import DataClassification, ensure_provider_compatible
from .errors import (
    ProviderClassificationError,
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


def _is_loopback_base_url(base_url: str) -> bool:
    """Return whether ``base_url`` is unambiguously local to this host.

    Merely naming this class ``LocalOpenAICompatibleProvider`` is not a
    security boundary: without validating the endpoint, a caller could point
    it at a hosted OpenAI-compatible API and the classification gate would
    still approve restricted clinical text.  Hostnames other than
    ``localhost`` are deliberately refused because DNS or ``/etc/hosts`` can
    map them to a remote machine.
    """
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    normalised = host.rstrip(".").lower()
    if normalised == "localhost" or normalised.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalised).is_loopback
    except ValueError:
        return False


def resolve_ollama_digest(base_url: str, model_id: str, *, timeout_s: float = 30.0) -> str:
    """Return the manifest digest Ollama reports for ``model_id``.

    Raises :class:`ProviderConfigurationError` if the server does not know
    the model or reports no digest, because a run without a digest is not
    reproducible and must not start.
    """
    if not _is_loopback_base_url(base_url):
        raise ProviderConfigurationError(
            f"Ollama digest resolution requires a loopback base URL, got {base_url!r}."
        )
    ensure_network_calls_allowed("resolve_ollama_digest")
    root = base_url.rstrip("/").removesuffix("/v1")
    show_url = f"{root}/api/show"
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
        # Ollama 0.17 dropped the digest from /api/show but still reports it
        # in /api/tags. Fall back rather than fail: without a digest a run is
        # not reproducible, and a tag alone can be repointed at new weights.
        digest = _digest_from_tags(root, model_id, timeout_s=timeout_s)
    if not digest:
        raise ProviderConfigurationError(
            f"Ollama reported no digest for {model_id!r} at {show_url} or {root}/api/tags; "
            "cannot record provenance."
        )
    return str(digest)


def _with_implicit_tag(name: str) -> str:
    """``mymodel`` -> ``mymodel:latest``; an explicit tag is left alone.

    Ollama's own naming rule, not a loosening of the match below. A model
    created locally -- ``ollama create meddial-extractor`` -- is listed as
    ``meddial-extractor:latest``, so asking for it by the name it was created
    under matched nothing and the run stopped with "no digest ... cannot record
    provenance". Pulled tags never showed this because they always carry an
    explicit tag.
    """
    return name if ":" in name else f"{name}:latest"


def _digest_from_tags(root: str, model_id: str, *, timeout_s: float) -> str | None:
    """Look the digest up in the model list. ``None`` when the server has no answer."""
    tags_url = f"{root}/api/tags"
    try:
        response = httpx.get(tags_url, timeout=timeout_s)
        response.raise_for_status()
        models = response.json().get("models") or []
    except httpx.HTTPError as exc:
        raise ProviderConfigurationError(
            f"Could not resolve a weight digest for {model_id!r} from {tags_url}: {exc}"
        ) from exc

    wanted = _with_implicit_tag(model_id)
    for entry in models:
        # Match the tag exactly, once both sides are written the same way.
        # ``qwen3.5:9b`` and ``qwen3.5:4b`` are different weights, and picking
        # the wrong one mislabels every score.
        names = {
            _with_implicit_tag(str(value))
            for key in ("name", "model")
            if (value := entry.get(key)) is not None
        }
        if wanted in names:
            return entry.get("digest") or None
    return None


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
        reasoning_effort: str | None = None,
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
        # A reasoning model spends its completion budget on reasoning tokens
        # before it emits any content, and those tokens are invisible in the
        # returned text while still being paid for. On a structured-extraction
        # prompt qwen3.5:9b consumed all 2048 tokens reasoning and returned an
        # empty message. ``"none"`` turns that off where the server supports
        # it; ``None`` leaves the server's default alone.
        self._reasoning_effort = reasoning_effort
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
        if (
            classification is DataClassification.RESTRICTED_CLINICAL
            and not _is_loopback_base_url(self._base_url)
        ):
            # This check precedes both the CI network guard and construction
            # of an HTTP request.  A mislabelled "local" provider therefore
            # cannot transmit even one restricted payload.
            raise ProviderClassificationError(
                "Restricted clinical data may be sent only to a loopback "
                f"OpenAI-compatible endpoint; refusing {self._base_url!r} "
                "before network I/O."
            )
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
        if self._reasoning_effort is not None:
            payload["reasoning_effort"] = self._reasoning_effort

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
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(
                f"{self._model_id} returned a response without a message body."
            ) from exc
        if not isinstance(text, str) or not text.strip():
            # "Empty completion" is the symptom, not the cause. A reasoning
            # model that hits the token ceiling mid-thought returns exactly
            # this, and reporting it as an empty answer sends the reader
            # looking at the prompt instead of the budget.
            message = choice.get("message") or {}
            reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
            if choice.get("finish_reason") == "length":
                raise ProviderResponseError(
                    f"{self._model_id} hit the max_tokens ceiling before emitting any "
                    f"content ({len(str(reasoning))} characters of reasoning were "
                    "produced instead). Raise max_tokens, or construct the provider "
                    'with reasoning_effort="none" so the budget goes to the answer.'
                )
            raise ProviderResponseError(
                f"{self._model_id} returned an empty completion."
            )
        return text
