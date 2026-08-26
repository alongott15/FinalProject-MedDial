"""Pre-specified, paired CMPB publication study design and execution contract."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from meddial.experiments.config import (
    RECOMMENDED_EVALUATOR_MODELS,
    AblationVariant,
    ExperimentConfig,
)


class StudyPhase(str, Enum):
    ARCHITECTURE_ABLATION = "architecture_ablation"
    KNOWLEDGE_POLICY_SENSITIVITY = "knowledge_policy_sensitivity"
    TARGETED_RECOVERY = "targeted_recovery"


@dataclass(frozen=True)
class StudyCell:
    cell_id: str
    phase: StudyPhase
    config: ExperimentConfig
    analysis_role: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "phase": self.phase.value,
            "analysis_role": self.analysis_role,
            "config": self.config.to_dict(),
            "config_hash": self.config.config_hash,
        }


@dataclass(frozen=True)
class PublicationStudyDesign:
    cohort_manifest: str
    cells: tuple[StudyCell, ...]
    pilot_case_count: int = 30
    planned_final_case_count: int = 200
    primary_outcome: str = "all_mandatory_dimensions_pass_first_attempt"
    design_version: str = "cmpb-study-v1"

    def __post_init__(self) -> None:
        if not self.cohort_manifest:
            raise ValueError("A private clinician-validated cohort manifest is required")
        cell_ids = [cell.cell_id for cell in self.cells]
        if len(set(cell_ids)) != len(cell_ids):
            raise ValueError("Study cell IDs must be unique")
        for cell in self.cells:
            families = {
                str(model.get("model_family", ""))
                for model in cell.config.evaluator_models
                if model.get("model_family")
            }
            if len(cell.config.evaluator_models) < 3 or len(families) < 3:
                raise ValueError(f"{cell.cell_id} requires three distinct evaluator model families")
            if cell.config.cohort_manifest != self.cohort_manifest:
                raise ValueError(f"{cell.cell_id} does not use the shared cohort manifest")

    @property
    def plan_hash(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "design_version": self.design_version,
            "cohort_manifest": self.cohort_manifest,
            "pilot_case_count": self.pilot_case_count,
            "planned_final_case_count": self.planned_final_case_count,
            "primary_outcome": self.primary_outcome,
            "secondary_outcomes": [
                "patient_factuality",
                "doctor_factuality",
                "zero_leakage_rate",
                "structural_validity",
                "naturalness",
                "attempt_count",
                "latency",
                "token_or_compute_cost",
            ],
            "paired_by": "clinical_case",
            "cells": [cell.to_dict() for cell in self.cells],
        }

    @classmethod
    def recommended(
        cls,
        cohort_manifest: str,
        generation_models: Mapping[str, str],
        *,
        seed: int = 42,
        max_turns: int = 30,
        pilot_case_count: int = 30,
        planned_final_case_count: int = 200,
    ) -> PublicationStudyDesign:
        evaluators = RECOMMENDED_EVALUATOR_MODELS
        thresholds = {
            "naturalness": 0.60,
            "role_aware_clinical_faithfulness": 0.70,
            "knowledge_boundary": 1.0,
            "structural_validity": 1.0,
        }

        def config(
            *,
            name: str,
            phase: StudyPhase,
            variant: AblationVariant,
            profiles: tuple[str, ...],
            max_attempts: int,
            recovery: bool,
        ) -> ExperimentConfig:
            return ExperimentConfig(
                name=name,
                variant=variant,
                seed=seed,
                max_attempts=max_attempts,
                max_turns=max_turns,
                profile_types=profiles,
                generation_models=dict(generation_models),
                evaluator_models=evaluators,
                acceptance_thresholds=thresholds,
                cohort_manifest=cohort_manifest,
                study_phase=phase.value,
                feature_overrides={"targeted_recovery": recovery}
                if variant is AblationVariant.FULL_MEDDIAL
                else {},
                metadata={
                    "blind_offline_evaluation": True,
                    "same_generator_across_cells": True,
                },
            )

        strict = ("NO_DIAGNOSIS_NO_TREATMENT",)
        cells: list[StudyCell] = []
        for variant in AblationVariant:
            cell_id = f"architecture-{variant.value}"
            cells.append(
                StudyCell(
                    cell_id,
                    StudyPhase.ARCHITECTURE_ABLATION,
                    config(
                        name=f"cmpb-{cell_id}",
                        phase=StudyPhase.ARCHITECTURE_ABLATION,
                        variant=variant,
                        profiles=strict,
                        max_attempts=1,
                        recovery=False,
                    ),
                    "Paired raw first-attempt architecture comparison",
                )
            )

        policies = ("FULL", "NO_DIAGNOSIS", "NO_DIAGNOSIS_NO_TREATMENT")
        for variant in (
            AblationVariant.KNOWLEDGE_CONTROLLED,
            AblationVariant.FULL_MEDDIAL,
        ):
            for policy in policies:
                slug = policy.lower().replace("_", "-")
                cell_id = f"policy-{variant.value}-{slug}"
                cells.append(
                    StudyCell(
                        cell_id,
                        StudyPhase.KNOWLEDGE_POLICY_SENSITIVITY,
                        config(
                            name=f"cmpb-{cell_id}",
                            phase=StudyPhase.KNOWLEDGE_POLICY_SENSITIVITY,
                            variant=variant,
                            profiles=(policy,),
                            max_attempts=1,
                            recovery=False,
                        ),
                        "Paired knowledge-policy sensitivity comparison",
                    )
                )

        for attempts, recovery in ((1, False), (3, True)):
            cell_id = f"recovery-full-meddial-{attempts}-attempts"
            cells.append(
                StudyCell(
                    cell_id,
                    StudyPhase.TARGETED_RECOVERY,
                    config(
                        name=f"cmpb-{cell_id}",
                        phase=StudyPhase.TARGETED_RECOVERY,
                        variant=AblationVariant.FULL_MEDDIAL,
                        profiles=strict,
                        max_attempts=attempts,
                        recovery=recovery,
                    ),
                    "Paired targeted-recovery effectiveness comparison",
                )
            )
        return cls(
            cohort_manifest=cohort_manifest,
            cells=tuple(cells),
            pilot_case_count=pilot_case_count,
            planned_final_case_count=planned_final_case_count,
        )


class StudyCellExecutor(Protocol):
    supported_variants: frozenset[AblationVariant]

    def execute(
        self,
        config: ExperimentConfig,
        profiles: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...


class PublicationStudyRunner:
    """Execute cells only after every named ablation has a declared implementation."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)

    def run(
        self,
        design: PublicationStudyDesign,
        profiles: Sequence[Mapping[str, Any]],
        executor: StudyCellExecutor,
    ) -> dict[str, Any]:
        required = {cell.config.variant for cell in design.cells}
        missing = required - executor.supported_variants
        if missing:
            raise ValueError(
                "Study executor is missing variant implementations: "
                f"{sorted(variant.value for variant in missing)}"
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        plan_path = self.output_dir / "study_plan.json"
        plan_payload = {**design.to_dict(), "plan_hash": design.plan_hash}
        if plan_path.exists():
            with plan_path.open(encoding="utf-8") as handle:
                existing = json.load(handle)
            if existing.get("plan_hash") != design.plan_hash:
                raise ValueError("Existing study plan hash differs; use a new output directory")
        else:
            with plan_path.open("x", encoding="utf-8") as handle:
                json.dump(plan_payload, handle, indent=2, ensure_ascii=False)

        results: list[dict[str, Any]] = []
        for cell in design.cells:
            result_path = self.output_dir / f"{cell.cell_id}.json"
            if result_path.exists():
                with result_path.open(encoding="utf-8") as handle:
                    result = json.load(handle)
            else:
                output = dict(executor.execute(cell.config, profiles))
                result = {
                    "cell_id": cell.cell_id,
                    "phase": cell.phase.value,
                    "config_hash": cell.config.config_hash,
                    "plan_hash": design.plan_hash,
                    "result": output,
                }
                with result_path.open("x", encoding="utf-8") as handle:
                    json.dump(result, handle, indent=2, ensure_ascii=False)
            results.append(result)
        return {
            "plan_hash": design.plan_hash,
            "cell_count": len(results),
            "results": results,
        }


def write_recommended_plan(
    output_path: str | Path,
    cohort_manifest: str,
    generation_model: str,
) -> Path:
    design = PublicationStudyDesign.recommended(
        cohort_manifest,
        generation_models={"doctor": generation_model, "patient": generation_model},
    )
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as handle:
        json.dump({**design.to_dict(), "plan_hash": design.plan_hash}, handle, indent=2)
    return target
