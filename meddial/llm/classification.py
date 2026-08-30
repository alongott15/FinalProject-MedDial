"""Data classification and the provider compatibility gate (GOV-3).

MIMIC-III notes, the references extracted from them, the patient profiles
derived from those references and the dialogues generated from those
profiles are all ``RESTRICTED_CLINICAL``. Sending any of them to a provider
that is not approved for restricted data is a governance breach, so the
check raises *before* any network I/O rather than relying on reviewer
discipline.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from .errors import ProviderClassificationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .provider import LLMProvider


class DataClassification(str, Enum):
    """How sensitive the payload of a model call is."""

    PUBLIC = "public"
    SYNTHETIC = "synthetic"
    RESTRICTED_CLINICAL = "restricted_clinical"


def ensure_provider_compatible(
    provider: LLMProvider, classification: DataClassification
) -> None:
    """Raise :class:`ProviderClassificationError` before any network I/O.

    Providers call this as the first statement of ``complete()``, before
    constructing a client, opening a connection or serialising a payload.
    """
    approved = provider.approved_classifications
    if classification not in approved:
        approved_names = ", ".join(sorted(c.value for c in approved))
        raise ProviderClassificationError(
            f"{type(provider).__name__} is not approved for "
            f"{classification.value} data (approved: {approved_names or 'none'}). "
            "Refusing before any network I/O."
        )
