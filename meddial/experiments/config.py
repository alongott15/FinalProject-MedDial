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


@dataclass(frozen=True)
class ExperimentConfig:
    name: str = "meddial-publication"
    variant: AblationVariant = AblationVariant.FULL_MEDDIAL
    seed: int = 42
    max_attempts: int = 3
    max_turns: int = 30
    profile_types: tuple[str, ...] = (
        "FULL",
        "NO_DIAGNOSIS",
        "NO_DIAGNOSIS_NO_TREATMENT",
    )
    generation_models: Mapping[str, str] = field(default_factory=dict)
    evaluator_models: tuple[Mapping[str, str], ...] = field(default_factory=tuple)
    acceptance_thresholds: Mapping[str, float] = field(default_factory=dict)
    cohort_manifest: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def features(self) -> Mapping[str, bool]:
        return _VARIANT_FEATURES[self.variant]

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
