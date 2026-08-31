"""Measuring the matcher against a hand-built fixture with known codes.

GRND-4 requires the matcher's own error rate to be reported alongside every
result that depends on it. Without that number, a low agreement between
extracted diagnoses and `DIAGNOSES_ICD` has two indistinguishable readings —
the extraction was wrong, or the matcher failed to recognise a correct
extraction — and the project's only external anchor becomes uninterpretable.

The fixture is deliberately not tuned to score well. It carries known-hard
cases where the matcher is expected to fail, because a fixture on which the
instrument scores 1.0 measures nothing and would let a real weakness through.
Every disagreement is returned, not just counted, so the failures can be read.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from meddial.grounding.matcher import CodedEntity, Matcher
from meddial.grounding.spec import (
    DEFAULT_MATCHER_DIR,
    Granularity,
    MatcherValidationError,
)

DEFAULT_FIXTURE_DIR = DEFAULT_MATCHER_DIR / "fixtures"

_REQUIRED_FIXTURE_KEYS = frozenset(
    {"fixture_id", "matcher", "provenance", "coded", "cases"}
)


@dataclass(frozen=True)
class FixtureCase:
    """One extracted string and the coded row it should resolve to.

    ``expects`` is the ``code`` for diagnoses and the ``description`` for
    medications, or ``None`` for a case that must not match anything — the
    negative cases are what stop a permissive threshold from looking good.
    """

    extracted: str
    expects: str | None
    note: str = ""
    extracted_code: str = ""


@dataclass(frozen=True)
class MatcherFixture:
    fixture_id: str
    matcher_key: str
    provenance: str
    coded: tuple[CodedEntity, ...]
    cases: tuple[FixtureCase, ...]

    @classmethod
    def load(cls, path: Path | str) -> MatcherFixture:
        path = Path(path)
        data = json.loads(path.read_text())
        missing = _REQUIRED_FIXTURE_KEYS - set(data)
        if missing:
            raise MatcherValidationError(
                f"{path.name} is missing fixture keys: {', '.join(sorted(missing))}"
            )
        coded = tuple(
            CodedEntity(description=row["description"], code=row.get("code", ""))
            for row in data["coded"]
        )
        labels = {entity.label for entity in coded}
        cases = []
        for row in data["cases"]:
            expects = row.get("expects")
            if expects is not None and expects not in labels:
                raise MatcherValidationError(
                    f"{path.name}: case {row['extracted']!r} expects {expects!r}, "
                    "which is not among the fixture's coded rows"
                )
            cases.append(
                FixtureCase(
                    extracted=row["extracted"],
                    expects=expects,
                    note=row.get("note", ""),
                    extracted_code=row.get("extracted_code", ""),
                )
            )
        return cls(
            fixture_id=str(data["fixture_id"]),
            matcher_key=str(data["matcher"]),
            provenance=str(data["provenance"]),
            coded=coded,
            cases=tuple(cases),
        )

    @classmethod
    def load_all(cls, directory: Path | str | None = None) -> list[MatcherFixture]:
        directory = Path(directory) if directory else DEFAULT_FIXTURE_DIR
        return [cls.load(path) for path in sorted(directory.glob("*.json"))]


@dataclass(frozen=True)
class Disagreement:
    """One case the matcher got wrong, with enough context to see why."""

    extracted: str
    expected: str | None
    got: str | None
    granularity: Granularity
    score: float
    normalised_extracted: str
    normalised_coded: str
    note: str

    def __str__(self) -> str:
        return (
            f"{self.extracted!r} expected {self.expected!r}, got {self.got!r} "
            f"({self.granularity.value}, score {self.score:.2f}): "
            f"{self.normalised_extracted!r} vs {self.normalised_coded!r}"
        )


@dataclass(frozen=True)
class MatcherErrorRate:
    """What the matcher gets right on the fixture, and what it does not."""

    matcher_key: str
    spec_hash: str
    fixture_id: str
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    by_granularity: dict[Granularity, int]
    disagreements: tuple[Disagreement, ...]

    @property
    def n_cases(self) -> int:
        return (
            self.true_positives
            + self.false_positives
            + self.false_negatives
            + self.true_negatives
            - self._double_counted
        )

    @property
    def _double_counted(self) -> int:
        """A wrong match counts as both a false positive and a false negative."""
        return sum(
            1 for d in self.disagreements if d.expected is not None and d.got is not None
        )

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def summary(self) -> str:
        """The line that accompanies every result computed with this matcher."""
        return (
            f"{self.matcher_key} (spec {self.spec_hash[:12]}) on {self.fixture_id}: "
            f"precision {self.precision:.3f}, recall {self.recall:.3f}, "
            f"F1 {self.f1:.3f} over {self.n_cases} cases "
            f"({len(self.disagreements)} disagreements)"
        )


def evaluate_matcher(matcher: Matcher, fixture: MatcherFixture) -> MatcherErrorRate:
    """Run every fixture case and report where the matcher and the truth differ.

    A match to the wrong row counts as both a false positive and a false
    negative. Counting it only as a miss would flatter precision, which is the
    number a reader leans on when deciding whether a reported agreement rate
    means anything.
    """
    if fixture.matcher_key != matcher.spec.key:
        raise MatcherValidationError(
            f"fixture {fixture.fixture_id} is written for {fixture.matcher_key}, "
            f"not {matcher.spec.key}. A matcher validated on another matcher's "
            "fixture has not been validated."
        )

    tp = fp = fn = tn = 0
    by_granularity: dict[Granularity, int] = {}
    disagreements: list[Disagreement] = []

    for case in fixture.cases:
        result = matcher.match(
            case.extracted, fixture.coded, extracted_code=case.extracted_code
        )
        got = result.coded.label if result.matched and result.coded else None

        if case.expects is not None and got == case.expects:
            tp += 1
            by_granularity[result.granularity] = (
                by_granularity.get(result.granularity, 0) + 1
            )
            continue
        if case.expects is None and got is None:
            tn += 1
            continue

        if got is not None:
            fp += 1
        if case.expects is not None:
            fn += 1
        disagreements.append(
            Disagreement(
                extracted=case.extracted,
                expected=case.expects,
                got=got,
                granularity=result.granularity,
                score=result.score,
                normalised_extracted=result.normalised_extracted,
                normalised_coded=result.normalised_coded,
                note=case.note,
            )
        )

    return MatcherErrorRate(
        matcher_key=matcher.spec.key,
        spec_hash=matcher.spec.content_hash,
        fixture_id=fixture.fixture_id,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        by_granularity=by_granularity,
        disagreements=tuple(disagreements),
    )


def format_report(rates: Sequence[MatcherErrorRate]) -> str:
    """A plain-text block suitable for pasting beside any GRND-1/2 result."""
    lines: list[str] = []
    for rate in rates:
        lines.append(rate.summary())
        for granularity, count in sorted(
            rate.by_granularity.items(), key=lambda item: item[0].value
        ):
            lines.append(f"    matched at {granularity.value}: {count}")
        for disagreement in rate.disagreements:
            lines.append(f"    ! {disagreement}")
    return "\n".join(lines)
