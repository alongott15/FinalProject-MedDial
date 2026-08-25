"""Compatibility imports for targeted, failure-classified recovery."""

from meddial.recovery import FailureClassifier, RecoveryStrategy, TargetedRecoveryAgent


class PromptImprovementAgent(TargetedRecoveryAgent):
    """Deprecated name retained for existing pipeline imports."""


__all__ = [
    "FailureClassifier",
    "PromptImprovementAgent",
    "RecoveryStrategy",
    "TargetedRecoveryAgent",
]
