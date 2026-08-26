"""Reproducible experiment and CMPB ablation configuration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class AblationVariant(str, Enum):
    DIRECT_LLM = "direct_llm"
    STRUCTURED_SINGLE_AGENT = "structured_single_agent"
    BASIC_MULTI_AGENT = "basic_multi_agent"
    KNOWLEDGE_CONTROLLED = "knowledge_controlled"
    FULL_MEDDIAL = "full_meddial"


_VARIANT_FEATURES: Mapping[AblationVariant, Mapping[str, bool]] = {
    AblationVariant.DIRECT_LLM: {
        "structured_reference": False,
        "multi_agent": False,
        "knowledge_control": False,
        "role_aware_evaluation": False,
        "targeted_recovery": False,
    },
    AblationVariant.STRUCTURED_SINGLE_AGENT: {
        "structured_reference": True,
        "multi_agent": False,
        "knowledge_control": False,
        "role_aware_evaluation": False,
        "targeted_recovery": False,
    },
    AblationVariant.BASIC_MULTI_AGENT: {
        "structured_reference": True,
        "multi_agent": True,
        "knowledge_control": False,
        "role_aware_evaluation": False,
        "targeted_recovery": False,
    },
    AblationVariant.KNOWLEDGE_CONTROLLED: {
        "structured_reference": True,
        "multi_agent": True,
        "knowledge_control": True,
        "role_aware_evaluation": False,
        "targeted_recovery": False,
    },
    AblationVariant.FULL_MEDDIAL: {
        "structured_reference": True,
        "multi_agent": True,
        "knowledge_control": True,
        "role_aware_evaluation": True,
        "targeted_recovery": True,
    },
}

RECOMMENDED_EVALUATOR_MODELS: tuple[Mapping[str, str], ...] = (
    {
        "evaluator_id": "judge-gpt-oss",
        "provider": "local_openai_compatible",
        "model": "gpt-oss-120b",
        "model_family": "gpt-oss",
    },
    {
        "evaluator_id": "judge-qwen",
        "provider": "local_openai_compatible",
        "model": "Qwen/Qwen3-32B",
        "model_family": "qwen",
    },
    {
        "evaluator_id": "judge-mistral",
        "provider": "local_openai_compatible",
        "model": "mistralai/Mistral-Small-3.2-24B-Instruct-2506",
        "model_family": "mistral",
    },
)


@dataclass(frozen=True)
class ExperimentConfig:
    name: str = "meddial-publication"
    variant: AblationVariant = AblationVariant.FULL_MEDDIAL
    seed: int = 42
    max_attempts: int = 3
    max_turns: int = 30
    profile_types: tuple[str, ...] = ("NO_DIAGNOSIS_NO_TREATMENT",)
    generation_models: Mapping[str, str] = field(default_factory=dict)
    evaluator_models: tuple[Mapping[str, str], ...] = field(default_factory=tuple)
    acceptance_thresholds: Mapping[str, float] = field(default_factory=dict)
    cohort_manifest: str | None = None
    study_phase: str = "unspecified"
    replicate: int = 1
    data_classification: str = "restricted_clinical"
    requires_clinical_review: bool = True
    feature_overrides: Mapping[str, bool] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_turns < 1:
            raise ValueError("max_attempts and max_turns must be positive")
        unknown = set(self.feature_overrides) - set(_VARIANT_FEATURES[self.variant])
        if unknown:
            raise ValueError(f"Unknown feature overrides: {sorted(unknown)}")
        if self.requires_clinical_review and not self.profile_types:
            raise ValueError("Publication configurations require at least one profile type")

    @property
    def features(self) -> Mapping[str, bool]:
        return {**_VARIANT_FEATURES[self.variant], **dict(self.feature_overrides)}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["variant"] = self.variant.value
        data["features"] = dict(self.features)
        return data

    @property
    def config_hash(self) -> str:
        payload = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExperimentConfig:
        known = dict(data)
        known.pop("features", None)
        known["variant"] = AblationVariant(known.get("variant", AblationVariant.FULL_MEDDIAL))
        for key in ("profile_types", "evaluator_models"):
            if key in known:
                known[key] = tuple(known[key])
        return cls(**known)
