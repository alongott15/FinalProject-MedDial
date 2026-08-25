"""Compatibility module for the renamed role-aware clinical evaluator.

The former implementation used a custom claim-checking algorithm under the
ambiguous label "RAGAS Faithfulness".  The publication implementation is
``RoleAwareClinicalFaithfulness`` and explicitly evaluates both dialogue roles.
"""

from meddial.evaluation.claims import RoleAwareClinicalFaithfulness
from meddial.evaluation.judge import RoleAwareJudgeAgent


class DeepEvalJudgeAgent(RoleAwareJudgeAgent):
    """Deprecated compatibility alias; use :class:`RoleAwareJudgeAgent`."""


ClaimFaithfulness = RoleAwareClinicalFaithfulness

__all__ = ["ClaimFaithfulness", "DeepEvalJudgeAgent", "RoleAwareJudgeAgent"]
