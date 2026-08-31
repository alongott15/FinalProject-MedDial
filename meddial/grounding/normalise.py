"""Public normalisation API for the frozen grounding matchers.

Normalisation rules are intentionally not hard-coded here.  They are loaded
from a :class:`~meddial.grounding.spec.MatcherSpec`, whose content hash and
freeze timestamp make the transformation used by a study run auditable.
"""

from __future__ import annotations

from meddial.grounding.matcher import Matcher, Normalised
from meddial.grounding.spec import MatcherSpec


def normalise(text: str, spec: MatcherSpec) -> Normalised:
    """Normalise ``text`` exactly as ``spec`` declares, retaining a rule trace."""

    return Matcher(spec).normalise(text)


__all__ = ["Normalised", "normalise"]
