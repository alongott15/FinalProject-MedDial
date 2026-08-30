"""A deterministic in-process provider for tests and CI.

Makes no network calls, so it is approved for every classification: it is
the only provider CI may use (``MEDDIAL_DISABLE_EXTERNAL_CALLS=1``).
Determinism is the point — the same prompt and seed must yield the same
text, so tests asserting on pipeline behaviour do not depend on sampling.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from .classification import DataClassification, ensure_provider_compatible
from .errors import ProviderError
from .provider import CallMetadata, ChatMessage, CompletionResult

_ALL_CLASSIFICATIONS = frozenset(DataClassification)


@dataclass
class RecordedCall:
    """One call captured by :class:`MockProvider`, for assertions."""

    messages: tuple[ChatMessage, ...]
    classification: DataClassification
    temperature: float
    max_tokens: int
    seed: int | None


class MockProvider:
    """Replay canned responses, or synthesise deterministic filler text.

    Parameters
    ----------
    responses:
        Returned in order. When exhausted (or empty) the provider falls back
        to text derived from a digest of the prompt and seed.
    failure:
        Raised instead of completing, to exercise error paths. Must be a
        :class:`~meddial.llm.errors.ProviderError`; the mock never returns
        an error string.
    """

    def __init__(
        self,
        responses: Sequence[str] | None = None,
        *,
        failure: ProviderError | None = None,
        model_id: str = "mock-model",
        model_family: str = "mock",
        approved: frozenset[DataClassification] = _ALL_CLASSIFICATIONS,
    ) -> None:
        self._responses = list(responses or [])
        self._failure = failure
        self._model_id = model_id
        self._model_family = model_family
        self._approved = approved
        self.calls: list[RecordedCall] = []

    @property
    def approved_classifications(self) -> frozenset[DataClassification]:
        return self._approved

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

        self.calls.append(
            RecordedCall(
                messages=tuple(messages),
                classification=classification,
                temperature=temperature,
                max_tokens=max_tokens,
                seed=seed,
            )
        )

        if self._failure is not None:
            raise self._failure

        if self._responses:
            text = self._responses.pop(0)
        else:
            text = self._synthesise(messages, seed)

        prompt_tokens = sum(len(m.content.split()) for m in messages)
        return CompletionResult(
            text=text,
            metadata=CallMetadata(
                model_id=self._model_id,
                model_digest=self._digest(messages, seed),
                model_family=self._model_family,
                quantisation="none",
                temperature=temperature,
                top_p=1.0,
                seed=seed,
                prompt_tokens=prompt_tokens,
                completion_tokens=len(text.split()),
                latency_ms=0,
                provider_class=type(self).__name__,
                classification=classification,
            ),
        )

    @staticmethod
    def _digest(messages: Sequence[ChatMessage], seed: int | None) -> str:
        payload = "\n".join(f"{m.role}:{m.content}" for m in messages)
        payload += f"\nseed={seed}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _synthesise(self, messages: Sequence[ChatMessage], seed: int | None) -> str:
        return f"mock completion {self._digest(messages, seed)[:16]}"
