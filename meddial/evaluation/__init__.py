"""Fail-closed, role-aware evaluation components."""

from meddial.evaluation.acceptance import AcceptanceCriteria, AcceptanceDecision
from meddial.evaluation.boundary import KnowledgeBoundaryValidator, LeakageEvent
from meddial.evaluation.claims import (
    Claim,
    ClaimType,
    RoleAwareClinicalFaithfulness,
    RuleBasedClaimExtractor,
)
from meddial.evaluation.ensemble import EnsembleConfig, IndependentEvaluatorEnsemble
from meddial.evaluation.judge import RoleAwareJudgeAgent
from meddial.evaluation.models import EvaluationStatus, MetricResult
from meddial.evaluation.structural import DeterministicStructuralValidator

__all__ = [
    "AcceptanceCriteria",
    "AcceptanceDecision",
    "Claim",
    "ClaimType",
    "DeterministicStructuralValidator",
    "EnsembleConfig",
    "EvaluationStatus",
    "IndependentEvaluatorEnsemble",
    "KnowledgeBoundaryValidator",
    "LeakageEvent",
    "MetricResult",
    "RoleAwareClinicalFaithfulness",
    "RoleAwareJudgeAgent",
    "RuleBasedClaimExtractor",
]
