"""Index-diagnosis term redaction (KNOW-5).

Masking a field is not enough. Under a no-diagnosis policy the index
diagnosis still appears verbatim in free text the patient legitimately
knows — the past medical history and the chief complaint. Those fields are
kept, with the index-diagnosis terms removed, so the patient can talk about
their history without handing the doctor the answer (defect D-04).

What counts as a term is deliberately generous: the diagnosis string, its
comma- and slash-separated components, and the acronym of any multi-word
term ("congestive heart failure" also removes "CHF"). Over-redaction
degrades a patient utterance; under-redaction invalidates the arm.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from meddial.knowledge.reference import StructuredClinicalReference

REDACTED = "[REDACTED]"

_SPLIT = re.compile(r"[,;/()\[\]]| - | vs\.? | and/or ", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")

#: Words too generic to redact on their own; removing them would gut the
#: text without hiding anything.
_STOPWORDS = frozenset(
    {
        "acute",
        "and",
        "chronic",
        "disease",
        "disorder",
        "for",
        "history",
        "left",
        "mild",
        "moderate",
        "of",
        "or",
        "patient",
        "right",
        "severe",
        "status",
        "the",
        "with",
        "without",
    }
)

_MIN_TERM_LENGTH = 4


def _components(phrase: str) -> Iterable[str]:
    for part in _SPLIT.split(phrase):
        part = part.strip(" .\t\n")
        if part:
            yield part


def _acronym(phrase: str) -> str | None:
    words = [w for w in _WORD.findall(phrase) if w.lower() not in _STOPWORDS]
    if len(words) < 2:
        return None
    return "".join(w[0] for w in words).upper()


def index_diagnosis_terms(reference: StructuredClinicalReference) -> frozenset[str]:
    """Every surface form of this admission's diagnoses worth removing."""
    terms: set[str] = set()
    for diagnosis in reference.core.diagnoses:
        for phrase in _components(diagnosis.primary):
            if len(phrase) >= _MIN_TERM_LENGTH and phrase.lower() not in _STOPWORDS:
                terms.add(phrase)
            acronym = _acronym(phrase)
            if acronym:
                terms.add(acronym)
    return frozenset(terms)


def _pattern(terms: Iterable[str]) -> re.Pattern[str] | None:
    # Longest first: "congestive heart failure" must win over "failure".
    ordered = sorted({t for t in terms if t.strip()}, key=len, reverse=True)
    if not ordered:
        return None
    return re.compile(
        r"\b(?:" + "|".join(re.escape(t) for t in ordered) + r")\b", re.IGNORECASE
    )


def redact_text(text: str, terms: Iterable[str]) -> tuple[str, int]:
    """Replace every term occurrence with :data:`REDACTED`."""
    pattern = _pattern(terms)
    if pattern is None:
        return text, 0
    replaced, count = pattern.subn(REDACTED, text)
    return replaced, count


def redact_value(value: Any, terms: Iterable[str]) -> tuple[Any, int]:
    """Redact every string reachable from ``value``, in place for containers."""
    if isinstance(value, str):
        return redact_text(value, terms)
    total = 0
    if isinstance(value, dict):
        for key, nested in value.items():
            value[key], count = redact_value(nested, terms)
            total += count
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            value[index], count = redact_value(nested, terms)
            total += count
    return value, total


@dataclass(frozen=True)
class RedactionReport:
    """What redaction did, so a run can report it rather than assert it."""

    terms: frozenset[str] = frozenset()
    replacements: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.replacements.values())
