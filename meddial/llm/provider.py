"""The provider protocol and the metadata every call must record.

Implements Implementation Plan §3.1. Two properties matter for the paper:

* every completion carries the identity of the weights that produced it
  (``model_digest``, ``quantisation``) so a run can be attributed to a
  specific artefact rather than a mutable tag (C8, EXP-7);
* a provider signals failure by raising, never by returning text (D-08).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .classification import DataClassification
from .errors import ProviderConfigurationError

DISABLE_NETWORK_ENV = "MEDDIAL_DISABLE_EXTERNAL_CALLS"

Role = str
"""One of ``"system"``, ``"user"``, ``"assistant"``."""


@dataclass(frozen=True)
class ChatMessage:
    """A single turn of the prompt sent to a provider."""

    role: Role
    content: str


def to_chat_messages(raw: Iterable[Mapping[str, str]]) -> list[ChatMessage]:
    """Adapt the ``{"role": ..., "content": ...}`` dicts the agents build."""
    return [ChatMessage(role=m["role"], content=m["content"]) for m in raw]


@dataclass(frozen=True)
class CallMetadata:
    """Provenance for one completion.

    ``model_digest`` is the content hash of the served weights (for example
    the Ollama manifest digest or the vLLM revision SHA), not the tag. Tags
    are mutable; a digest identifies the exact artefact a number came from.
    """

    model_id: str
    model_digest: str
    model_family: str
    quantisation: str
    temperature: float
    top_p: float
    seed: int | None
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    provider_class: str
    classification: DataClassification


@dataclass(frozen=True)
class CompletionResult:
    """A successful completion and its provenance."""

    text: str
    metadata: CallMetadata


def ensure_network_calls_allowed(provider_class: str) -> None:
    """Refuse to open a socket when the test kill-switch is set.

    CI runs with ``MEDDIAL_DISABLE_EXTERNAL_CALLS=1`` so that a test which
    accidentally reaches for a real model fails loudly instead of silently
    hitting whatever endpoint happens to be configured.
    """
    if os.environ.get(DISABLE_NETWORK_ENV) not in (None, "", "0"):
        raise ProviderConfigurationError(
            f"{provider_class} attempted a network call while "
            f"{DISABLE_NETWORK_ENV} is set. Use MockProvider in tests."
        )


@runtime_checkable
class LLMProvider(Protocol):
    """The only interface agents may use to reach a model."""

    @property
    def approved_classifications(self) -> frozenset[DataClassification]:
        """Classifications this provider may receive.

        Enforced by :func:`meddial.llm.classification.ensure_provider_compatible`
        before any network I/O.
        """
        ...

    @property
    def model_family(self) -> str:
        """Family the served model belongs to, for judge-independence checks."""
        ...

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        classification: DataClassification,
        temperature: float,
        max_tokens: int,
        seed: int | None = None,
    ) -> CompletionResult:
        """Return a completion or raise :class:`~meddial.llm.errors.ProviderError`.

        Implementations must never return an error string.
        """
        ...
