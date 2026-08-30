"""Provider error hierarchy.

Closes PRD defect D-08. Every failure mode that the legacy
``Utils.llms_utils.chat_generate`` returned as an ``"[ERROR: ...]"`` string
is raised here instead, so no provider failure can ever be mistaken for
model output and scored as dialogue content.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for every provider failure. Never returned as text."""


class ProviderConfigurationError(ProviderError):
    """The provider is missing credentials, an endpoint, or required metadata."""


class ProviderClassificationError(ProviderError):
    """A call was attempted with a data classification the provider is not approved for.

    Raised before any network I/O (GOV-3).
    """


class ProviderResponseError(ProviderError):
    """The provider returned a malformed, empty, or unusable response."""


class ProviderRateLimitError(ProviderError):
    """The provider rate-limited the request and retries were exhausted."""


class ProviderTimeoutError(ProviderError):
    """The provider did not respond within the configured timeout."""
