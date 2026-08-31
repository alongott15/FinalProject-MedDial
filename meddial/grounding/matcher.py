"""Applying a frozen specification to one extracted string.

The rules live in :mod:`meddial.grounding.spec` and on disk; this module only
executes them. Nothing here decides what counts as a match — the spec does —
and every result says at what granularity it matched and which rules fired, so
a number in a results table can be traced back to the transformation that
produced it.

Two deliberate omissions:

* **No fuzzy string distance.** Edit distance would match "hypotension" to
  "hypertension" at a threshold that also matches genuinely related terms, and
  the failure would be invisible in an aggregate. Agreement here comes from a
  declared substitution or from token overlap, both of which a reviewer can
  inspect.
* **No stemming.** It would need its own validation, and every rule that is not
  in the spec file is a rule the reader cannot audit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from meddial.grounding.spec import (
    GRANULARITY_RANK,
    EntityKind,
    Granularity,
    MatcherSpec,
)


@dataclass(frozen=True)
class CodedEntity:
    """One row of clinician-authored ground truth.

    For diagnoses: an ICD-9 code and its ``D_ICD_DIAGNOSES`` long title. For
    medications: the ``PRESCRIPTIONS`` drug name, with ``code`` empty.
    """

    description: str
    code: str = ""

    @property
    def label(self) -> str:
        return self.code or self.description


@dataclass(frozen=True)
class Normalised:
    """A string after the spec's rules, with the trace that produced it."""

    text: str
    trace: tuple[str, ...] = ()

    @property
    def tokens(self) -> frozenset[str]:
        return frozenset(self.text.split())

    def fired(self, rule: str) -> bool:
        return rule in self.trace


@dataclass(frozen=True)
class MatchResult:
    """Whether two entities agree, how strongly, and on what evidence."""

    granularity: Granularity
    score: float
    coded: CodedEntity | None = None
    normalised_extracted: str = ""
    normalised_coded: str = ""
    trace: tuple[str, ...] = field(default=())

    @property
    def matched(self) -> bool:
        return self.granularity is not Granularity.NONE

    @property
    def rank(self) -> int:
        return GRANULARITY_RANK[self.granularity]


NO_MATCH = MatchResult(granularity=Granularity.NONE, score=0.0)

_PUNCTUATION = re.compile(r"[^\w\s]+")
_WHITESPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")


def icd9_category(code: str) -> str:
    """The three-digit category, which is where ICD-9 groups clinically.

    E-codes carry their category in four characters (``E849``), everything else
    in three (``4280`` → ``428``, ``V5861`` → ``V58``).
    """
    normalised = code.strip().upper().replace(".", "")
    if not normalised:
        return ""
    return normalised[:4] if normalised.startswith("E") else normalised[:3]


class Matcher:
    """Executes one frozen :class:`MatcherSpec`."""

    def __init__(self, spec: MatcherSpec) -> None:
        self.spec = spec
        self._rules = {
            "lowercase": self._lowercase,
            "expand_abbreviations": self._expand_abbreviations,
            "apply_synonyms": self._apply_synonyms,
            "brand_to_generic": self._brand_to_generic,
            "strip_dose_and_form": self._strip_dose_and_form,
            "strip_salt_forms": self._strip_salt_forms,
            "drop_coding_qualifiers": self._drop_coding_qualifiers,
            "drop_stopwords": self._drop_stopwords,
            "strip_punctuation": self._strip_punctuation,
            "collapse_whitespace": self._collapse_whitespace,
        }
        # The spec validated that every declared rule is in NORMALISATION_RULES;
        # this asserts the other direction — that this class implements it.
        for rule in spec.normalisation:
            if rule not in self._rules:
                raise NotImplementedError(
                    f"{spec.key} declares rule {rule!r}, registered in the spec "
                    "module but not implemented here"
                )

    # -- normalisation ---------------------------------------------------

    def normalise(self, text: str) -> Normalised:
        """Run the spec's rules in order, recording which ones changed anything."""
        current = text
        trace: list[str] = []
        for rule in self.spec.normalisation:
            after = self._rules[rule](current)
            if after != current:
                trace.append(rule)
            current = after
        return Normalised(text=current.strip(), trace=tuple(trace))

    def _lowercase(self, text: str) -> str:
        return text.lower()

    def _strip_punctuation(self, text: str) -> str:
        return _PUNCTUATION.sub(" ", text)

    def _collapse_whitespace(self, text: str) -> str:
        return _WHITESPACE.sub(" ", text).strip()

    def _expand_abbreviations(self, text: str) -> str:
        return _substitute(text, self.spec.table("abbreviations"))

    def _apply_synonyms(self, text: str) -> str:
        return _substitute(text, self.spec.table("synonyms"))

    def _brand_to_generic(self, text: str) -> str:
        return _substitute(text, self.spec.table("brand_to_generic"))

    def _drop_coding_qualifiers(self, text: str) -> str:
        return _remove(text, self.spec.table("coding_qualifiers"))

    def _drop_stopwords(self, text: str) -> str:
        return _remove(text, self.spec.table("stopwords"))

    def _strip_salt_forms(self, text: str) -> str:
        return _remove(text, self.spec.table("salt_forms"))

    def _strip_dose_and_form(self, text: str) -> str:
        result = text
        for unit in sorted(self.spec.table("dose_units"), key=len, reverse=True):
            result = re.sub(rf"\b\d+(?:\.\d+)?\s*{re.escape(unit)}\b", " ", result)
        result = _NUMBER.sub(" ", result)
        result = _remove(result, self.spec.table("dose_units"))
        return _remove(result, self.spec.table("dose_forms"))

    # -- matching --------------------------------------------------------

    def match_one(
        self,
        extracted: str,
        coded: CodedEntity,
        *,
        extracted_code: str = "",
    ) -> MatchResult:
        """Compare one extracted string against one coded row.

        ``extracted_code`` is passed only when the extraction itself produced a
        code. It is never inferred from the text: guessing a code from a
        description and then reporting an ``exact_code`` match would be circular.
        """
        allowed = self.spec.granularities
        left = self.normalise(extracted)
        right = self.normalise(coded.description)
        trace = tuple(sorted(set(left.trace) | set(right.trace)))
        common = {
            "coded": coded,
            "normalised_extracted": left.text,
            "normalised_coded": right.text,
            "trace": trace,
        }

        if (
            Granularity.EXACT_CODE in allowed
            and extracted_code
            and coded.code
            and _bare_code(extracted_code) == _bare_code(coded.code)
        ):
            return MatchResult(Granularity.EXACT_CODE, 1.0, **common)

        if left.text and left.text == right.text:
            # Same string, but *why* differs: a brand substitution means the
            # match rests on the spec's drug table rather than on the text.
            resolved_by_table = left.fired("brand_to_generic") or right.fired(
                "brand_to_generic"
            )
            if resolved_by_table and Granularity.GENERIC_EQUIVALENT in allowed:
                return MatchResult(Granularity.GENERIC_EQUIVALENT, 1.0, **common)
            if Granularity.EXACT_DESCRIPTION in allowed:
                return MatchResult(Granularity.EXACT_DESCRIPTION, 1.0, **common)

        if (
            Granularity.CATEGORY_3DIGIT in allowed
            and extracted_code
            and coded.code
            and icd9_category(extracted_code) == icd9_category(coded.code)
        ):
            return MatchResult(Granularity.CATEGORY_3DIGIT, 1.0, **common)

        if Granularity.DESCRIPTION_OVERLAP in allowed:
            score = dice(left.tokens, right.tokens)
            if score >= self.spec.description_overlap_threshold:
                return MatchResult(Granularity.DESCRIPTION_OVERLAP, score, **common)
            return MatchResult(Granularity.NONE, score, **common)

        return MatchResult(Granularity.NONE, 0.0, **common)

    def match(
        self,
        extracted: str,
        candidates: Sequence[CodedEntity],
        *,
        extracted_code: str = "",
    ) -> MatchResult:
        """The strongest match among ``candidates``, or a NONE result.

        Ties on granularity are broken by score, and remaining ties by the order
        the candidates were given, so the result does not depend on dict or set
        iteration order.
        """
        best = NO_MATCH
        for candidate in candidates:
            result = self.match_one(extracted, candidate, extracted_code=extracted_code)
            # `best is NO_MATCH` takes the first candidate unconditionally: the
            # sentinel carries no normalised text, and a failure that cannot show
            # what normalisation produced is a failure nobody can diagnose.
            if best is NO_MATCH or (result.rank, result.score) > (best.rank, best.score):
                best = result
        return best


def dice(left: Iterable[str], right: Iterable[str]) -> float:
    """Sørensen–Dice over token sets.

    Chosen over Jaccard because ICD-9 long titles are systematically longer than
    what a clinician writes: "pneumonia" against "pneumonia, organism
    unspecified" scores 0.5 under Jaccard and 0.67 under Dice, and the extra
    tokens are coding boilerplate rather than clinical disagreement. Chosen over
    plain containment because containment would match a single shared word —
    "heart" would find "congestive heart failure".
    """
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def _bare_code(code: str) -> str:
    return code.strip().upper().replace(".", "")


def _substitute(text: str, table: Mapping[str, str]) -> str:
    """Whole-phrase replacement, longest key first so a short key cannot
    pre-empt a longer phrase containing it."""
    result = text
    for key in sorted(table, key=len, reverse=True):
        result = re.sub(rf"\b{re.escape(key)}\b", table[key], result)
    return result


def _remove(text: str, phrases: Iterable[str]) -> str:
    result = text
    for phrase in sorted(phrases, key=len, reverse=True):
        result = re.sub(rf"\b{re.escape(phrase)}\b", " ", result)
    return _WHITESPACE.sub(" ", result).strip()


def matcher_for(spec: MatcherSpec, entity_kind: EntityKind) -> Matcher:
    """Build a matcher, refusing a spec written for a different entity kind."""
    if spec.entity_kind is not entity_kind:
        raise ValueError(
            f"{spec.key} is a {spec.entity_kind.value} matcher; "
            f"{entity_kind.value} entities need their own specification"
        )
    return Matcher(spec)
