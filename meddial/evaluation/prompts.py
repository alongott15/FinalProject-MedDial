"""Versioned prompt templates for the evaluation layer.

Implements Implementation Plan Appendix D. An evaluator prompt is a file, not
a string literal buried in a scorer, and its version is the hash of that file.
Editing a template therefore changes every ``prompt_version`` it produces, so
scores from before and after an edit can never be silently pooled.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from meddial.llm import ChatMessage

PROMPT_DIR = Path(__file__).resolve().parent / "templates"

_SYSTEM_HEADING = "## SYSTEM"
_USER_HEADING = "## USER"
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
_VERSION_CHARS = 12


class PromptError(Exception):
    """A template is missing, malformed, or rendered with the wrong slots."""


@dataclass(frozen=True)
class PromptTemplate:
    """A system/user prompt pair and the digest that identifies it."""

    name: str
    system: str
    user: str
    version: str

    def render(self, **values: str) -> list[ChatMessage]:
        """Fill ``{{slot}}`` placeholders and return provider-ready messages.

        Raises :class:`PromptError` if any placeholder is left unfilled — a
        prompt sent with a literal ``{{claims}}`` in it would produce a
        confidently wrong score rather than an obvious failure.
        """
        system = self._substitute(self.system, values)
        user = self._substitute(self.user, values)
        return [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)]

    def _substitute(self, text: str, values: dict[str, str]) -> str:
        rendered = _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)
        leftover = sorted(set(_PLACEHOLDER.findall(rendered)))
        if leftover:
            raise PromptError(f"{self.name}: unfilled placeholder(s) {leftover}")
        return rendered


@cache
def load_prompt(name: str) -> PromptTemplate:
    """Load ``<name>.md`` from :data:`PROMPT_DIR` and hash it for versioning."""
    path = PROMPT_DIR / f"{name}.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptError(f"no prompt template at {path}") from exc

    if _SYSTEM_HEADING not in raw or _USER_HEADING not in raw:
        raise PromptError(f"{path} must contain both '{_SYSTEM_HEADING}' and '{_USER_HEADING}'")

    _, remainder = raw.split(_SYSTEM_HEADING, 1)
    system, user = remainder.split(_USER_HEADING, 1)

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_VERSION_CHARS]
    return PromptTemplate(
        name=name,
        system=system.strip(),
        user=user.strip(),
        version=f"{name}@{digest}",
    )
