"""Versioned, hash-stable experiment configuration (EXP-1, EXP-2, EXP-6).

Configuration is the experimental treatment, not a bag of command-line
defaults.  Every field capable of changing a result is represented here and
included in a canonical SHA-256 digest: policy and doctor conditioning,
reference mode, seeds and budgets, thresholds, exact model weights, prompt
versions, input identity, and batch settings.

Structural validation is performed at construction.  Confirmation-specific
checks (frozen timestamp, immutable model/input digests, and refusal of a
deprecated policy) are explicit through :meth:`RunConfig.validate` so the E0
comparison can still replay the deprecated thesis policies.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from meddial.evaluation import ReferenceMode
from meddial.knowledge import KnowledgePolicy, PolicyRegistry

from .variants import VariantName

CONFIG_SCHEMA_VERSION = "2.0"
_PINNED_SHA256 = re.compile(r"^sha256:[0-9a-fA-F]{64}$")


class RunConfigError(ValueError):
    """A run configuration is incomplete, ambiguous, or not reproducible."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _thaw(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    if isinstance(value, ReferenceMode):
        return value.value
    if isinstance(value, VariantName):
        return value.value
    return value


def _require_nonempty(value: str, field_name: str) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        raise RunConfigError(f"{field_name} is required")
    return cleaned


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RunConfigError(f"frozen_at is not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise RunConfigError("frozen_at must include a timezone")
    return parsed


def _split_policy_reference(policy_id: str, version: str | None) -> tuple[str, str | None]:
    raw = _require_nonempty(policy_id, "patient_policy_id")
    if "@" not in raw:
        return raw, version
    parsed_id, parsed_version = raw.rsplit("@", 1)
    if not parsed_id or not parsed_version:
        raise RunConfigError(f"invalid patient policy reference {raw!r}")
    if version is not None and str(version) != parsed_version:
        raise RunConfigError(
            f"patient policy version is ambiguous: {raw!r} conflicts with {version!r}"
        )
    return parsed_id, parsed_version


@dataclass(frozen=True)
class ModelSpec:
    """Exact weights and serving form assigned to one experiment role."""

    id: str
    digest: str
    family: str
    quantisation: str
    provider_class: str = "LocalOpenAICompatibleProvider"
    prompt_cost_per_million: float = 0.0
    completion_cost_per_million: float = 0.0

    def __post_init__(self) -> None:
        for name in ("id", "digest", "family", "quantisation", "provider_class"):
            object.__setattr__(self, name, _require_nonempty(getattr(self, name), name))
        for name in ("prompt_cost_per_million", "completion_cost_per_million"):
            value = float(getattr(self, name))
            if value < 0:
                raise RunConfigError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)

    @property
    def model_id(self) -> str:
        return self.id

    @property
    def model_digest(self) -> str:
        return self.digest

    @property
    def model_family(self) -> str:
        return self.family

    @property
    def pinned(self) -> bool:
        """Whether ``digest`` identifies immutable SHA-256-addressed weights."""
        return bool(_PINNED_SHA256.fullmatch(self.digest))

    def estimated_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError("token counts must be non-negative")
        return (
            prompt_tokens * self.prompt_cost_per_million
            + completion_tokens * self.completion_cost_per_million
        ) / 1_000_000

    def as_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "digest": self.digest,
            "family": self.family,
            "quantisation": self.quantisation,
            "provider_class": self.provider_class,
            "prompt_cost_per_million": self.prompt_cost_per_million,
            "completion_cost_per_million": self.completion_cost_per_million,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ModelSpec:
        data = dict(value)
        aliases = {
            "model_id": "id",
            "model_digest": "digest",
            "model_family": "family",
            "quant": "quantisation",
        }
        for source, target in aliases.items():
            if source in data:
                if target in data and data[target] != data[source]:
                    raise RunConfigError(f"model spec gives conflicting {source}/{target}")
                data[target] = data.pop(source)
        allowed = {
            "id",
            "digest",
            "family",
            "quantisation",
            "provider_class",
            "prompt_cost_per_million",
            "completion_cost_per_million",
        }
        unknown = set(data) - allowed
        if unknown:
            raise RunConfigError(f"unknown ModelSpec fields: {', '.join(sorted(unknown))}")
        try:
            return cls(**data)
        except TypeError as exc:
            raise RunConfigError(f"invalid ModelSpec: {exc}") from exc


@dataclass(frozen=True)
class RunConfig:
    """The complete versioned contract for one experimental cell."""

    name: str
    variant: str
    patient_policy_id: str
    doctor_guidance_id: str
    reference_mode: ReferenceMode
    seed: int
    max_turns: int
    max_attempts: int
    thresholds: Mapping[str, float]
    models: Mapping[str, ModelSpec]
    prompt_versions: Mapping[str, str]
    frozen_at: str | None = None
    input_manifest_hash: str = "UNSPECIFIED"
    patient_policy_version: str | None = None
    schema_version: str = CONFIG_SCHEMA_VERSION
    batch_config: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_nonempty(self.name, "name"))
        try:
            variant = VariantName(self.variant).value
        except ValueError as exc:
            raise RunConfigError(f"unknown variant {self.variant!r}") from exc
        object.__setattr__(self, "variant", variant)

        policy_id, policy_version = _split_policy_reference(
            self.patient_policy_id, self.patient_policy_version
        )
        object.__setattr__(self, "patient_policy_id", policy_id)
        object.__setattr__(self, "patient_policy_version", policy_version)
        object.__setattr__(
            self,
            "doctor_guidance_id",
            _require_nonempty(self.doctor_guidance_id, "doctor_guidance_id"),
        )
        try:
            object.__setattr__(self, "reference_mode", ReferenceMode(self.reference_mode))
        except ValueError as exc:
            raise RunConfigError(f"unknown reference_mode {self.reference_mode!r}") from exc

        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise RunConfigError("seed must be an integer")
        if self.max_turns < 1 or self.max_attempts < 1:
            raise RunConfigError("max_turns and max_attempts must be positive")

        thresholds: dict[str, float] = {}
        for dimension, raw in self.thresholds.items():
            name = _require_nonempty(str(dimension), "threshold dimension")
            value = float(raw)
            if not 0.0 <= value <= 1.0:
                raise RunConfigError(f"threshold {name!r}={value} is outside [0, 1]")
            thresholds[name] = value
        if not thresholds:
            raise RunConfigError("thresholds must not be empty")
        object.__setattr__(self, "thresholds", _freeze(thresholds))

        models: dict[str, ModelSpec] = {}
        for role, raw in self.models.items():
            role_name = _require_nonempty(str(role), "model role")
            models[role_name] = raw if isinstance(raw, ModelSpec) else ModelSpec.from_mapping(raw)
        if not models:
            raise RunConfigError("models must not be empty")
        object.__setattr__(self, "models", _freeze(models))

        prompts = {
            _require_nonempty(str(role), "prompt role"): _require_nonempty(
                str(version), "prompt version"
            )
            for role, version in self.prompt_versions.items()
        }
        if not prompts:
            raise RunConfigError("prompt_versions must not be empty")
        object.__setattr__(self, "prompt_versions", _freeze(prompts))

        if self.frozen_at is not None:
            _parse_timestamp(self.frozen_at)
        object.__setattr__(
            self, "input_manifest_hash", _require_nonempty(self.input_manifest_hash, "input_manifest_hash")
        )
        object.__setattr__(
            self, "schema_version", _require_nonempty(self.schema_version, "schema_version")
        )
        object.__setattr__(self, "batch_config", _freeze(self.batch_config))
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @property
    def patient_policy_ref(self) -> str:
        if self.patient_policy_version is None:
            return self.patient_policy_id
        return f"{self.patient_policy_id}@{self.patient_policy_version}"

    def prompt_set_hash(self) -> str:
        return _hash(self.prompt_versions)

    def config_hash(self) -> str:
        return _hash(self.as_record())

    def as_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "variant": self.variant,
            "patient_policy_id": self.patient_policy_id,
            "patient_policy_version": self.patient_policy_version,
            "doctor_guidance_id": self.doctor_guidance_id,
            "reference_mode": self.reference_mode.value,
            "seed": self.seed,
            "max_turns": self.max_turns,
            "max_attempts": self.max_attempts,
            "thresholds": dict(self.thresholds),
            "models": {role: spec.as_record() for role, spec in sorted(self.models.items())},
            "prompt_versions": dict(self.prompt_versions),
            "frozen_at": self.frozen_at,
            "input_manifest_hash": self.input_manifest_hash,
            "batch_config": _thaw(self.batch_config),
            "metadata": _thaw(self.metadata),
        }

    def validate(
        self,
        *,
        policy_registry: PolicyRegistry | None = None,
        confirmatory: bool = False,
    ) -> KnowledgePolicy:
        """Resolve the policy and apply checks that depend on run phase.

        Deprecated policies remain legal for exploratory E0 replay.  A
        confirmatory run additionally requires an explicit policy version, a
        freeze timestamp, a pinned input manifest, and pinned model digests.
        """
        if confirmatory:
            if self.frozen_at is None:
                raise RunConfigError("confirmatory runs require frozen_at")
            if self.patient_policy_version is None:
                raise RunConfigError(
                    "confirmatory runs require an explicit patient_policy_version"
                )
            if not _PINNED_SHA256.fullmatch(self.input_manifest_hash):
                raise RunConfigError(
                    "confirmatory input_manifest_hash must be a pinned sha256:<64 hex> digest"
                )
            unpinned = [role for role, spec in self.models.items() if not spec.pinned]
            if unpinned:
                raise RunConfigError(
                    "confirmatory model digest is not pinned for roles: "
                    + ", ".join(sorted(unpinned))
                )

        registry = policy_registry or PolicyRegistry()
        if confirmatory:
            return registry.for_confirmatory_run(
                self.patient_policy_id, self.patient_policy_version
            )
        return registry.load(self.patient_policy_id, self.patient_policy_version)

    def freeze(self, frozen_at: str | None = None) -> RunConfig:
        timestamp = frozen_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _parse_timestamp(timestamp)
        return replace(self, frozen_at=timestamp)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RunConfig:
        data = dict(value)
        # Derived fields are accepted in manifests but never trusted as input.
        data.pop("config_hash", None)
        data.pop("prompt_set_hash", None)
        data["models"] = {
            role: raw if isinstance(raw, ModelSpec) else ModelSpec.from_mapping(raw)
            for role, raw in data.get("models", {}).items()
        }
        if "reference_mode" in data:
            data["reference_mode"] = ReferenceMode(data["reference_mode"])
        allowed = {
            "name",
            "variant",
            "patient_policy_id",
            "patient_policy_version",
            "doctor_guidance_id",
            "reference_mode",
            "seed",
            "max_turns",
            "max_attempts",
            "thresholds",
            "models",
            "prompt_versions",
            "frozen_at",
            "input_manifest_hash",
            "schema_version",
            "batch_config",
            "metadata",
        }
        unknown = set(data) - allowed
        if unknown:
            raise RunConfigError(f"unknown RunConfig fields: {', '.join(sorted(unknown))}")
        try:
            return cls(**data)
        except TypeError as exc:
            raise RunConfigError(f"invalid RunConfig: {exc}") from exc


def load_run_config(path: str | Path) -> RunConfig:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunConfigError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise RunConfigError(f"{source} must contain one JSON object")
    return RunConfig.from_mapping(value)


def hash_input_manifest(value: Mapping[str, Any] | Sequence[Any]) -> str:
    """Hash a manifest's semantic JSON content, independent of key order."""
    return f"sha256:{_hash(value)}"


@dataclass(frozen=True)
class EXP8ControlConfigs:
    patient_policy_varied: tuple[RunConfig, ...]
    doctor_guidance_varied: tuple[RunConfig, ...]


def _policy_parts(reference: str) -> tuple[str, str | None]:
    return _split_policy_reference(reference, None)


def vary_patient_policy(
    base: RunConfig,
    patient_policies: Sequence[str],
    *,
    pinned_doctor_guidance_id: str,
) -> tuple[RunConfig, ...]:
    """EXP-8 arm: vary patient knowledge while doctor conditioning is fixed."""
    configs: list[RunConfig] = []
    for reference in patient_policies:
        policy_id, version = _policy_parts(reference)
        configs.append(
            replace(
                base,
                name=f"{base.name}-exp8-patient-{policy_id.lower()}-{version or 'active'}",
                patient_policy_id=policy_id,
                patient_policy_version=version,
                doctor_guidance_id=pinned_doctor_guidance_id,
                metadata={**_thaw(base.metadata), "exp8_control": "patient_policy_varied"},
            )
        )
    if len({config.patient_policy_ref for config in configs}) != len(configs):
        raise RunConfigError("patient_policies must be distinct")
    return tuple(configs)


def vary_doctor_guidance(
    base: RunConfig,
    doctor_guidance_ids: Sequence[str],
    *,
    pinned_patient_policy: str,
) -> tuple[RunConfig, ...]:
    """EXP-8 converse: vary doctor conditioning while patient policy is fixed."""
    policy_id, version = _policy_parts(pinned_patient_policy)
    configs = tuple(
        replace(
            base,
            name=f"{base.name}-exp8-doctor-{guidance.lower()}",
            patient_policy_id=policy_id,
            patient_policy_version=version,
            doctor_guidance_id=guidance,
            metadata={**_thaw(base.metadata), "exp8_control": "doctor_guidance_varied"},
        )
        for guidance in doctor_guidance_ids
    )
    if len({config.doctor_guidance_id for config in configs}) != len(configs):
        raise RunConfigError("doctor_guidance_ids must be distinct")
    return configs


def build_exp8_control_configs(
    base: RunConfig,
    *,
    patient_policies: Sequence[str],
    doctor_guidance_ids: Sequence[str],
    pinned_doctor_guidance_id: str,
    pinned_patient_policy: str,
) -> EXP8ControlConfigs:
    return EXP8ControlConfigs(
        patient_policy_varied=vary_patient_policy(
            base,
            patient_policies,
            pinned_doctor_guidance_id=pinned_doctor_guidance_id,
        ),
        doctor_guidance_varied=vary_doctor_guidance(
            base,
            doctor_guidance_ids,
            pinned_patient_policy=pinned_patient_policy,
        ),
    )


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "EXP8ControlConfigs",
    "ModelSpec",
    "RunConfig",
    "RunConfigError",
    "build_exp8_control_configs",
    "hash_input_manifest",
    "load_run_config",
    "vary_doctor_guidance",
    "vary_patient_policy",
]
