"""Matcher specifications — the rules, written down before they are used.

GRND-4. `DIAGNOSES_ICD` and `PRESCRIPTIONS` are the only human-authored ground
truth this project has: hospital coders assigned those codes during real care.
Everything the study claims about extraction accuracy is a claim about how well
a matcher lines an extracted string up against one of those rows — so the
matcher is an instrument, and an instrument tuned after seeing the results it
is judging is not evidence.

Hence three properties, all enforced here rather than assumed:

* **Written down as data.** Normalisation is an ordered list of named rules and
  the tables they consult, in JSON under ``configs/matchers/``. A reviewer can
  read what "match" means without reading Python.
* **Fail closed.** A rule the code does not implement is an error, not a silent
  no-op, and a rule whose table is missing is rejected at load time. Neither can
  quietly weaken matching.
* **Frozen before use.** ``content_hash`` is checked against
  ``MATCHER_HASHES.json``, and ``frozen_at`` is inside the hashed body — so
  backdating the freeze to sneak past ``ensure_frozen_before`` breaks the hash.
  Changing a rule after seeing study output requires a new version and a re-run.

The matcher's own measured error rate lives in :mod:`meddial.grounding.evaluate`
and is reported alongside every result that depends on it.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

MATCHER_DIR_ENV = "MEDDIAL_MATCHER_DIR"
HASH_LOCK_FILENAME = "MATCHER_HASHES.json"

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATCHER_DIR = _REPO_ROOT / "configs" / "matchers"


class MatcherError(Exception):
    """Base class for every failure in this package."""


class MatcherValidationError(MatcherError):
    """A specification is incomplete, inconsistent, or has been edited."""


class UnknownMatcherError(MatcherError):
    """No specification with that key exists on disk."""


class MatcherNotFrozenError(MatcherError):
    """The matcher was frozen at or after the run it is being used to judge."""


class EntityKind(str, Enum):
    DIAGNOSIS = "diagnosis"
    MEDICATION = "medication"


class Granularity(str, Enum):
    """What kind of agreement was found, strongest first.

    Reported separately rather than collapsed into one match rate. A
    three-digit-category agreement is a much weaker claim than an exact code,
    and averaging them together would overstate the anchor.
    """

    EXACT_CODE = "exact_code"
    EXACT_DESCRIPTION = "exact_description"
    GENERIC_EQUIVALENT = "generic_equivalent"
    CATEGORY_3DIGIT = "category_3digit"
    DESCRIPTION_OVERLAP = "description_overlap"
    NONE = "none"


#: Strength order, used to pick the best of several candidate matches.
GRANULARITY_RANK: dict[Granularity, int] = {
    Granularity.EXACT_CODE: 5,
    Granularity.EXACT_DESCRIPTION: 4,
    Granularity.GENERIC_EQUIVALENT: 3,
    Granularity.CATEGORY_3DIGIT: 2,
    Granularity.DESCRIPTION_OVERLAP: 1,
    Granularity.NONE: 0,
}

#: Every normalisation rule the code implements, and the spec tables each one
#: consults. Declaring a rule whose table is absent is a validation error: the
#: rule would otherwise run as a no-op and silently widen what counts as a
#: match. Adding a rule here without implementing it in ``matcher.py`` will fail
#: the round-trip test.
NORMALISATION_RULES: dict[str, frozenset[str]] = {
    "lowercase": frozenset(),
    "expand_abbreviations": frozenset({"abbreviations"}),
    "apply_synonyms": frozenset({"synonyms"}),
    "brand_to_generic": frozenset({"brand_to_generic"}),
    "strip_dose_and_form": frozenset({"dose_units", "dose_forms"}),
    "strip_salt_forms": frozenset({"salt_forms"}),
    "drop_coding_qualifiers": frozenset({"coding_qualifiers"}),
    "drop_stopwords": frozenset({"stopwords"}),
    "strip_punctuation": frozenset(),
    "collapse_whitespace": frozenset(),
}

_TABLE_KEYS = frozenset(
    {
        "abbreviations",
        "synonyms",
        "brand_to_generic",
        "dose_units",
        "dose_forms",
        "salt_forms",
        "coding_qualifiers",
        "stopwords",
    }
)

_REQUIRED_KEYS = frozenset(
    {
        "matcher_id",
        "version",
        "entity_kind",
        "normalisation",
        "granularities",
        "rationale",
        "frozen_at",
    }
)

_OPTIONAL_KEYS = _TABLE_KEYS | frozenset({"description_overlap_threshold"})


@dataclass(frozen=True)
class MatcherSpec:
    """One matcher, exactly as it is written on disk."""

    matcher_id: str
    version: str
    entity_kind: EntityKind
    normalisation: tuple[str, ...]
    granularities: frozenset[Granularity]
    rationale: str
    frozen_at: str
    description_overlap_threshold: float
    tables: Mapping[str, Any]

    @property
    def key(self) -> str:
        return f"{self.matcher_id}@{self.version}"

    @property
    def frozen_at_utc(self) -> datetime:
        return _parse_timestamp(self.frozen_at)

    @property
    def content_hash(self) -> str:
        """SHA-256 of everything except the version.

        ``frozen_at`` is inside the body deliberately: the freeze timestamp is
        what ``ensure_frozen_before`` trusts, so moving it must break the lock.
        """
        body = {
            "matcher_id": self.matcher_id,
            "entity_kind": self.entity_kind.value,
            "normalisation": list(self.normalisation),
            "granularities": sorted(g.value for g in self.granularities),
            "rationale": self.rationale,
            "frozen_at": self.frozen_at,
            "description_overlap_threshold": self.description_overlap_threshold,
            "tables": _canonical_tables(self.tables),
        }
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def table(self, name: str) -> Any:
        return self.tables[name]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> MatcherSpec:
        missing = _REQUIRED_KEYS - set(data)
        if missing:
            raise MatcherValidationError(
                f"matcher is missing required keys: {', '.join(sorted(missing))}"
            )
        unknown = set(data) - _REQUIRED_KEYS - _OPTIONAL_KEYS
        if unknown:
            raise MatcherValidationError(
                f"matcher has unrecognised keys: {', '.join(sorted(unknown))}"
            )
        try:
            entity_kind = EntityKind(str(data["entity_kind"]))
        except ValueError as exc:
            raise MatcherValidationError(
                f"unknown entity_kind {data['entity_kind']!r}"
            ) from exc

        granularities = set()
        for name in data["granularities"]:
            try:
                granularities.add(Granularity(str(name)))
            except ValueError as exc:
                raise MatcherValidationError(
                    f"unknown granularity {name!r}; implemented: "
                    f"{', '.join(g.value for g in Granularity)}"
                ) from exc

        spec = cls(
            matcher_id=str(data["matcher_id"]),
            version=str(data["version"]),
            entity_kind=entity_kind,
            normalisation=tuple(str(rule) for rule in data["normalisation"]),
            granularities=frozenset(granularities),
            rationale=str(data["rationale"]),
            frozen_at=str(data["frozen_at"]),
            description_overlap_threshold=float(
                data.get("description_overlap_threshold", 1.0)
            ),
            tables={key: data[key] for key in _TABLE_KEYS if key in data},
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        """Raise unless every declared rule exists and has the tables it reads."""
        for rule in self.normalisation:
            required = NORMALISATION_RULES.get(rule)
            if required is None:
                raise MatcherValidationError(
                    f"{self.key} declares normalisation rule {rule!r}, which is not "
                    f"implemented. Implemented rules: "
                    f"{', '.join(sorted(NORMALISATION_RULES))}."
                )
            absent = required - set(self.tables)
            if absent:
                raise MatcherValidationError(
                    f"{self.key} declares rule {rule!r} but supplies no "
                    f"{', '.join(sorted(absent))}. A rule without its table would "
                    "run as a no-op and silently widen what counts as a match."
                )

        if Granularity.NONE in self.granularities:
            raise MatcherValidationError(
                f"{self.key} lists 'none' as a granularity. 'none' is the absence "
                "of a match, not a way of matching."
            )
        if not self.granularities:
            raise MatcherValidationError(f"{self.key} declares no granularities")

        if not 0.0 < self.description_overlap_threshold <= 1.0:
            raise MatcherValidationError(
                f"{self.key} has description_overlap_threshold "
                f"{self.description_overlap_threshold}, which is outside (0, 1]"
            )

        _parse_timestamp(self.frozen_at)


def ensure_frozen_before(spec: MatcherSpec, run_started_at: datetime) -> None:
    """GRND-4: a run may only use a matcher frozen before that run began.

    Without this, a rule could be relaxed after a disappointing match rate and
    the study re-run against the more generous matcher, with nothing in the
    record showing that the instrument moved.
    """
    frozen_at = spec.frozen_at_utc
    started = _as_utc(run_started_at)
    if frozen_at >= started:
        raise MatcherNotFrozenError(
            f"{spec.key} was frozen at {frozen_at.isoformat()}, which is not "
            f"before the run started at {started.isoformat()}. A matcher edited "
            "during or after a run cannot judge it; bump the version, freeze it, "
            "and re-run."
        )


class MatcherRegistry:
    """The matcher specifications on disk, keyed by ``matcher_id@version``."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = _resolve_directory(directory)
        self._specs: dict[str, MatcherSpec] | None = None
        self._hashes: dict[str, str] | None = None

    def _load_hashes(self) -> dict[str, str]:
        if self._hashes is None:
            lock = self.directory / HASH_LOCK_FILENAME
            self._hashes = json.loads(lock.read_text()) if lock.exists() else {}
        return self._hashes

    def _load(self) -> dict[str, MatcherSpec]:
        if self._specs is not None:
            return self._specs
        if not self.directory.is_dir():
            raise MatcherValidationError(f"no matcher directory at {self.directory}")
        specs: dict[str, MatcherSpec] = {}
        for path in sorted(self.directory.glob("*.json")):
            if path.name == HASH_LOCK_FILENAME:
                continue
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                raise MatcherValidationError(
                    f"{path.name} is not valid JSON: {exc}"
                ) from exc
            spec = MatcherSpec.from_mapping(data)
            if spec.key in specs:
                raise MatcherValidationError(f"{spec.key} is defined twice")
            specs[spec.key] = spec
        self._verify_hashes(specs)
        self._specs = specs
        return specs

    def _verify_hashes(self, specs: Mapping[str, MatcherSpec]) -> None:
        """A body change without a version bump must fail."""
        recorded = self._load_hashes()
        if not recorded:
            return
        for key, spec in specs.items():
            expected = recorded.get(key)
            if expected is None:
                raise MatcherValidationError(
                    f"{key} has no recorded hash. If this is a new version, add it "
                    f"to {HASH_LOCK_FILENAME}; if you edited an existing matcher, "
                    "bump its version instead."
                )
            if expected != spec.content_hash:
                raise MatcherValidationError(
                    f"{key} has changed since its hash was recorded "
                    f"({spec.content_hash[:12]} != {expected[:12]}). A frozen "
                    "matcher is not editable: bump the version and re-run "
                    "everything that depended on the old one."
                )

    def all(self) -> list[MatcherSpec]:
        return list(self._load().values())

    def get(self, key: str) -> MatcherSpec:
        specs = self._load()
        try:
            return specs[key]
        except KeyError as exc:
            raise UnknownMatcherError(
                f"no matcher {key!r}; available: {', '.join(sorted(specs))}"
            ) from exc


def _resolve_directory(directory: Path | str | None) -> Path:
    if directory is not None:
        return Path(directory)
    override = os.environ.get(MATCHER_DIR_ENV)
    return Path(override) if override else DEFAULT_MATCHER_DIR


def _canonical_tables(tables: Mapping[str, Any]) -> dict[str, Any]:
    """Order-independent view of the rule tables, so reordering JSON is a no-op."""
    canonical: dict[str, Any] = {}
    for name, table in sorted(tables.items()):
        if isinstance(table, Mapping):
            canonical[name] = {str(k): str(v) for k, v in sorted(table.items())}
        else:
            canonical[name] = sorted(str(item) for item in table)
    return canonical


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MatcherValidationError(
            f"frozen_at {value!r} is not an ISO-8601 timestamp"
        ) from exc
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise MatcherValidationError(
            f"timestamp {value.isoformat()} has no timezone; a naive freeze time "
            "cannot be compared across machines"
        )
    return value.astimezone(timezone.utc)
