"""W1 tests for the provider and compliance layer.

Covers PRD GOV-3 (classification gate before I/O), GOV-4 (provider
injection), EXP-7 (weight provenance) and defect D-08 (errors raised, never
returned as text).
"""

from __future__ import annotations

import httpx
import pytest

from meddial.llm import (
    DataClassification,
    LocalOpenAICompatibleProvider,
    MockProvider,
    ProviderClassificationError,
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    local_openai,
    resolve_ollama_digest,
)
from meddial.llm.provider import ChatMessage

RESTRICTED = DataClassification.RESTRICTED_CLINICAL
SYNTHETIC = DataClassification.SYNTHETIC

MESSAGES = [
    ChatMessage(role="system", content="You are a patient."),
    ChatMessage(role="user", content="What brings you in today?"),
]


@pytest.fixture(autouse=True)
def _provider_unit_tests_use_only_stubbed_http(monkeypatch) -> None:
    """Let MockTransport exercise HTTP semantics under CI's network guard.

    CI exports ``MEDDIAL_DISABLE_EXTERNAL_CALLS=1`` for the full suite.  The
    tests in this module deliberately instantiate the real provider class,
    but every request is intercepted by ``httpx.MockTransport`` or a
    monkeypatched ``httpx.get/post``.  Clear the process-level guard for each
    such test; the dedicated kill-switch test sets it again explicitly.
    """
    monkeypatch.delenv("MEDDIAL_DISABLE_EXTERNAL_CALLS", raising=False)


def _ok_response(text: str = "Chest pain since this morning.") -> dict[str, object]:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5},
    }


def _local_provider(handler: httpx.MockTransport, **kwargs: object):
    return LocalOpenAICompatibleProvider(
        "http://localhost:11434/v1",
        "llama3.1:8b",
        model_digest="sha256:abc123",
        model_family="llama",
        quantisation="Q4_K_M",
        client=httpx.Client(transport=handler),
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_local_provider_is_approved_for_restricted_data() -> None:
    """D2: local serving is what makes restricted generation permissible."""
    provider = _local_provider(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=_ok_response()))
    )
    result = provider.complete(
        MESSAGES, classification=RESTRICTED, temperature=0.7, max_tokens=256
    )
    assert result.text == "Chest pain since this morning."


@pytest.mark.parametrize(
    ("responder", "expected"),
    [
        (lambda _r: httpx.Response(429), ProviderRateLimitError),
        (lambda _r: httpx.Response(500, text="upstream exploded"), ProviderResponseError),
        (lambda _r: httpx.Response(200, json={"choices": []}), ProviderResponseError),
        (
            lambda _r: httpx.Response(
                200, json={"choices": [{"message": {"content": "   "}}]}
            ),
            ProviderResponseError,
        ),
    ],
)
def test_provider_error_raises_not_returns_string(responder, expected) -> None:
    """D-08: every failure mode raises, so none can be scored as an utterance."""
    provider = _local_provider(httpx.MockTransport(responder))

    with pytest.raises(expected) as excinfo:
        provider.complete(
            MESSAGES, classification=RESTRICTED, temperature=0.7, max_tokens=256
        )

    assert "[ERROR:" not in str(excinfo.value)


def test_provider_timeout_raises_timeout_error() -> None:
    def _timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    provider = _local_provider(httpx.MockTransport(_timeout))

    with pytest.raises(ProviderTimeoutError):
        provider.complete(
            MESSAGES, classification=RESTRICTED, temperature=0.7, max_tokens=256
        )


def test_call_metadata_records_digest_and_quant() -> None:
    """EXP-7/C8: a result must name the artefact that produced it."""
    provider = _local_provider(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=_ok_response()))
    )

    metadata = provider.complete(
        MESSAGES,
        classification=RESTRICTED,
        temperature=0.3,
        max_tokens=256,
        seed=7,
    ).metadata

    assert metadata.model_digest == "sha256:abc123"
    assert metadata.quantisation == "Q4_K_M"
    assert metadata.model_family == "llama"
    assert metadata.seed == 7
    assert metadata.temperature == 0.3
    assert metadata.prompt_tokens == 11
    assert metadata.completion_tokens == 5
    assert metadata.classification is RESTRICTED
    assert metadata.provider_class == "LocalOpenAICompatibleProvider"


def test_local_provider_refuses_to_start_without_a_digest() -> None:
    """A run whose weights cannot be identified must not begin."""
    with pytest.raises(ProviderConfigurationError, match="model_digest"):
        LocalOpenAICompatibleProvider(
            "http://localhost:11434/v1",
            "llama3.1:8b",
            model_digest="",
            model_family="llama",
            quantisation="Q4_K_M",
        )


def test_mock_provider_is_deterministic() -> None:
    """Pipeline tests must not depend on sampling."""
    first = MockProvider().complete(
        MESSAGES, classification=RESTRICTED, temperature=0.7, max_tokens=64, seed=42
    )
    second = MockProvider().complete(
        MESSAGES, classification=RESTRICTED, temperature=0.7, max_tokens=64, seed=42
    )
    other_seed = MockProvider().complete(
        MESSAGES, classification=RESTRICTED, temperature=0.7, max_tokens=64, seed=43
    )

    assert first.text == second.text
    assert first.metadata.model_digest == second.metadata.model_digest
    assert first.text != other_seed.text


def test_restricted_call_to_an_unapproved_provider_raises_before_io() -> None:
    """GOV-3 / C2: MIMIC-derived text must not reach a provider not approved
    for it, not even once.

    The gate is a property of the layer, not of any one provider: it runs in
    ``ensure_provider_compatible`` before the provider records or transmits
    anything, so an empty call log is the evidence that nothing was sent.
    """
    provider = MockProvider(approved=frozenset({DataClassification.PUBLIC, SYNTHETIC}))

    with pytest.raises(ProviderClassificationError) as excinfo:
        provider.complete(
            MESSAGES, classification=RESTRICTED, temperature=0.7, max_tokens=64
        )

    assert provider.calls == []
    assert "restricted_clinical" in str(excinfo.value)

    # The gate must not be so blunt that it blocks approved traffic.
    provider.complete(MESSAGES, classification=SYNTHETIC, temperature=0.7, max_tokens=64)
    assert len(provider.calls) == 1


def test_restricted_call_to_non_loopback_local_provider_raises_before_io() -> None:
    """A class name cannot turn a hosted endpoint into an approved provider."""
    requests: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_ok_response())

    provider = LocalOpenAICompatibleProvider(
        "https://hosted.example/v1",
        "llama3.1:8b",
        model_digest="sha256:abc123",
        model_family="llama",
        quantisation="Q4_K_M",
        client=httpx.Client(transport=httpx.MockTransport(_record)),
    )

    with pytest.raises(ProviderClassificationError, match="loopback"):
        provider.complete(
            MESSAGES,
            classification=RESTRICTED,
            temperature=0.0,
            max_tokens=64,
        )

    assert requests == []


def test_network_kill_switch_blocks_real_providers(monkeypatch) -> None:
    """CI sets this so a stray real call fails loudly instead of dialling out."""
    monkeypatch.setenv("MEDDIAL_DISABLE_EXTERNAL_CALLS", "1")
    provider = _local_provider(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=_ok_response()))
    )

    with pytest.raises(ProviderConfigurationError, match="MEDDIAL_DISABLE_EXTERNAL_CALLS"):
        provider.complete(
            MESSAGES, classification=RESTRICTED, temperature=0.7, max_tokens=64
        )


def test_network_kill_switch_does_not_block_the_mock(monkeypatch) -> None:
    monkeypatch.setenv("MEDDIAL_DISABLE_EXTERNAL_CALLS", "1")
    result = MockProvider(["hello"]).complete(
        MESSAGES, classification=RESTRICTED, temperature=0.7, max_tokens=64
    )
    assert result.text == "hello"


# --------------------------------------------------------------------------
# EXP-7: resolving the weight digest from a running Ollama server
# --------------------------------------------------------------------------

DIGEST = "6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7"


def _stub_ollama(monkeypatch, *, show: dict, tags: dict | None = None) -> list[str]:
    """Stand in for a running server, recording which endpoints were consulted."""
    seen: list[str] = []

    def _post(url: str, **_kwargs) -> httpx.Response:
        seen.append(url)
        return httpx.Response(200, json=show, request=httpx.Request("POST", url))

    def _get(url: str, **_kwargs) -> httpx.Response:
        seen.append(url)
        return httpx.Response(200, json=tags or {}, request=httpx.Request("GET", url))

    monkeypatch.setattr(local_openai.httpx, "post", _post)
    monkeypatch.setattr(local_openai.httpx, "get", _get)
    return seen


def test_digest_comes_from_show_when_the_server_reports_one(monkeypatch) -> None:
    seen = _stub_ollama(monkeypatch, show={"digest": DIGEST})

    assert resolve_ollama_digest("http://localhost:11434/v1", "qwen3.5:9b") == DIGEST
    assert seen == ["http://localhost:11434/api/show"]


def test_digest_falls_back_to_tags_when_show_omits_it(monkeypatch) -> None:
    """Ollama 0.17 dropped the digest from /api/show; the run must not stop."""
    seen = _stub_ollama(
        monkeypatch,
        show={"details": {"family": "qwen35"}},
        tags={"models": [{"name": "qwen3.5:9b", "digest": DIGEST}]},
    )

    assert resolve_ollama_digest("http://localhost:11434/v1", "qwen3.5:9b") == DIGEST
    assert seen[-1] == "http://localhost:11434/api/tags"


def test_the_fallback_matches_the_tag_exactly(monkeypatch) -> None:
    """qwen3.5:9b and qwen3.5:4b are different weights."""
    _stub_ollama(
        monkeypatch,
        show={"details": {}},
        tags={"models": [{"name": "qwen3.5:4b", "digest": "other"}]},
    )

    with pytest.raises(ProviderConfigurationError, match="no digest"):
        resolve_ollama_digest("http://localhost:11434/v1", "qwen3.5:9b")


def test_a_run_cannot_start_against_unidentifiable_weights(monkeypatch) -> None:
    """EXP-7: a number whose weights cannot be named is not reproducible."""
    _stub_ollama(monkeypatch, show={"details": {}}, tags={"models": []})

    with pytest.raises(ProviderConfigurationError, match="cannot record provenance"):
        resolve_ollama_digest("http://localhost:11434/v1", "qwen3.5:9b")
