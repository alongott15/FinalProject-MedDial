"""Five explicit experiment architectures and the registry that binds them.

EXP-5 is not satisfied by five labels over one execution path.  Each variant
below has its own implementation class, immutable implementation identifier,
stage declaration, and backend method.  The registry rejects duplicate
fingerprints and missing implementations before a run directory is created.

The concrete model-serving code remains outside this module.  A caller supplies
a backend implementing the five named methods; this makes the architecture
boundary testable offline while keeping provider construction in the run
composition layer.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol


class VariantName(str, Enum):
    DIRECT_LLM = "direct_llm"
    STRUCTURED_SINGLE_AGENT = "structured_single_agent"
    BASIC_MULTI_AGENT = "basic_multi_agent"
    KNOWLEDGE_CONTROLLED = "knowledge_controlled"
    FULL_MEDDIAL = "full_meddial"


class VariantError(RuntimeError):
    """Base class for an invalid or unavailable architecture implementation."""


class UnimplementedVariantError(VariantError):
    """A run named an architecture for which no executable is registered."""


class VariantAliasError(VariantError):
    """Two architecture names resolve to the same implementation."""


@dataclass(frozen=True)
class VariantRequest:
    """One architecture-neutral generation request.

    ``case`` is deliberately opaque to W7.  Cohort/reference modules own its
    schema; the experiment harness only hashes it, assigns a deterministic
    seed, and delivers it to the selected architecture.
    """

    case_id: str
    case: Mapping[str, Any]
    config: Any
    attempt_index: int
    seed: int
    repair: Mapping[str, Any] | None = None
    prior_attempts: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id is required")
        if self.attempt_index < 1:
            raise ValueError("attempt_index must be >= 1")
        object.__setattr__(self, "case", MappingProxyType(dict(self.case)))
        if self.repair is not None:
            object.__setattr__(self, "repair", MappingProxyType(dict(self.repair)))


class VariantBackend(Protocol):
    """Composition surface implemented by the generation application."""

    def direct_llm(self, request: VariantRequest) -> Mapping[str, Any]: ...

    def structured_single_agent(self, request: VariantRequest) -> Mapping[str, Any]: ...

    def basic_multi_agent(self, request: VariantRequest) -> Mapping[str, Any]: ...

    def knowledge_controlled(self, request: VariantRequest) -> Mapping[str, Any]: ...

    def full_meddial(self, request: VariantRequest) -> Mapping[str, Any]: ...


class VariantImplementation(ABC):
    """A distinct architecture, not merely a feature flag."""

    variant: VariantName
    implementation_id: str
    stages: tuple[str, ...]
    backend_method: str

    @property
    def fingerprint(self) -> str:
        payload = {
            "class": f"{type(self).__module__}.{type(self).__qualname__}",
            "implementation_id": self.implementation_id,
            "stages": self.stages,
            "backend_method": self.backend_method,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @abstractmethod
    def execute(self, backend: VariantBackend, request: VariantRequest) -> Mapping[str, Any]:
        """Run this architecture's concrete backend method."""

    def _dispatch(self, backend: VariantBackend, request: VariantRequest) -> Mapping[str, Any]:
        method = getattr(backend, self.backend_method, None)
        if not callable(method):
            raise UnimplementedVariantError(
                f"backend {type(backend).__name__} does not implement "
                f"{self.variant.value!r} via {self.backend_method}()"
            )
        result = method(request)
        if not isinstance(result, Mapping):
            raise VariantError(
                f"{self.implementation_id} returned {type(result).__name__}; expected a mapping"
            )
        return result


class DirectLLMVariant(VariantImplementation):
    variant = VariantName.DIRECT_LLM
    implementation_id = "direct_llm.v2"
    stages = ("unstructured_case_prompt", "single_completion")
    backend_method = "direct_llm"

    def execute(self, backend: VariantBackend, request: VariantRequest) -> Mapping[str, Any]:
        return self._dispatch(backend, request)


class StructuredSingleAgentVariant(VariantImplementation):
    variant = VariantName.STRUCTURED_SINGLE_AGENT
    implementation_id = "structured_single_agent.v2"
    stages = ("structured_reference", "single_dialogue_agent")
    backend_method = "structured_single_agent"

    def execute(self, backend: VariantBackend, request: VariantRequest) -> Mapping[str, Any]:
        return self._dispatch(backend, request)


class BasicMultiAgentVariant(VariantImplementation):
    variant = VariantName.BASIC_MULTI_AGENT
    implementation_id = "basic_multi_agent.v2"
    stages = ("structured_reference", "doctor_agent", "patient_agent")
    backend_method = "basic_multi_agent"

    def execute(self, backend: VariantBackend, request: VariantRequest) -> Mapping[str, Any]:
        return self._dispatch(backend, request)


class KnowledgeControlledVariant(VariantImplementation):
    variant = VariantName.KNOWLEDGE_CONTROLLED
    implementation_id = "knowledge_controlled.v2"
    stages = (
        "structured_reference",
        "knowledge_policy",
        "doctor_agent",
        "patient_agent",
    )
    backend_method = "knowledge_controlled"

    def execute(self, backend: VariantBackend, request: VariantRequest) -> Mapping[str, Any]:
        return self._dispatch(backend, request)


class FullMedDialVariant(VariantImplementation):
    variant = VariantName.FULL_MEDDIAL
    implementation_id = "full_meddial.v2"
    stages = (
        "structured_reference",
        "knowledge_policy",
        "doctor_agent",
        "patient_agent",
        "role_aware_evaluation",
        "targeted_repair",
    )
    backend_method = "full_meddial"

    def execute(self, backend: VariantBackend, request: VariantRequest) -> Mapping[str, Any]:
        return self._dispatch(backend, request)


class VariantRegistry:
    """Fail-closed binding from a public variant name to one implementation."""

    def __init__(self) -> None:
        self._implementations: dict[VariantName, VariantImplementation] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(variant.value for variant in self._implementations))

    def register(self, implementation: VariantImplementation) -> None:
        if not isinstance(implementation, VariantImplementation):
            raise TypeError("implementation must be a VariantImplementation")
        variant = VariantName(implementation.variant)
        if variant in self._implementations:
            raise VariantAliasError(f"{variant.value!r} is already registered")

        for existing in self._implementations.values():
            if type(existing) is type(implementation):
                raise VariantAliasError(
                    f"{variant.value!r} aliases {existing.variant.value!r}: same implementation class"
                )
            if existing.implementation_id == implementation.implementation_id:
                raise VariantAliasError(
                    f"{variant.value!r} aliases {existing.variant.value!r}: "
                    f"implementation_id {implementation.implementation_id!r} is reused"
                )
            if existing.fingerprint == implementation.fingerprint:
                raise VariantAliasError(
                    f"{variant.value!r} aliases {existing.variant.value!r}: identical fingerprint"
                )
        self._implementations[variant] = implementation

    def resolve(self, variant: VariantName | str) -> VariantImplementation:
        name = VariantName(variant)
        try:
            return self._implementations[name]
        except KeyError:
            raise UnimplementedVariantError(
                f"variant {name.value!r} has no registered implementation"
            ) from None

    def require(self, variants: tuple[VariantName | str, ...]) -> None:
        missing = [VariantName(item).value for item in variants if VariantName(item) not in self._implementations]
        if missing:
            raise UnimplementedVariantError(
                f"missing variant implementations: {', '.join(sorted(missing))}"
            )

    def require_all(self) -> None:
        self.require(tuple(VariantName))


def default_variant_registry() -> VariantRegistry:
    registry = VariantRegistry()
    for implementation in (
        DirectLLMVariant(),
        StructuredSingleAgentVariant(),
        BasicMultiAgentVariant(),
        KnowledgeControlledVariant(),
        FullMedDialVariant(),
    ):
        registry.register(implementation)
    registry.require_all()
    return registry


__all__ = [
    "BasicMultiAgentVariant",
    "DirectLLMVariant",
    "FullMedDialVariant",
    "KnowledgeControlledVariant",
    "StructuredSingleAgentVariant",
    "UnimplementedVariantError",
    "VariantAliasError",
    "VariantBackend",
    "VariantError",
    "VariantImplementation",
    "VariantName",
    "VariantRegistry",
    "VariantRequest",
    "default_variant_registry",
]
