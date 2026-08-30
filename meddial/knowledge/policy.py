"""Knowledge policies: what each participant is allowed to know.

A policy is data, not code (KNOW-2). Adding an experimental arm means
writing a JSON file under ``configs/policies/``; the leak test then covers
it automatically because it enumerates the registry.

Three properties the code enforces rather than assumes:

* **Fail closed.** A field the policy does not classify is invisible, and
  validation refuses a policy that leaves any surface field unclassified —
  so extending the reference cannot silently widen disclosure (KNOW-3).
* **Versioned.** ``content_hash`` is checked against a recorded lock file,
  so editing a policy in place without bumping its version fails loudly
  (KNOW-4).
* **Retired arms stay reproducible.** The thesis policies are kept and
  marked deprecated: they can be replayed as an E0 comparison arm, but a
  confirmatory run refuses them.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from meddial.knowledge import fieldpath
from meddial.knowledge.redaction import (
    RedactionReport,
    index_diagnosis_terms,
    redact_value,
)
from meddial.knowledge.reference import StructuredClinicalReference

POLICY_DIR_ENV = "MEDDIAL_POLICY_DIR"
HASH_LOCK_FILENAME = "POLICY_HASHES.json"

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_DIR = _REPO_ROOT / "configs" / "policies"

_REQUIRED_KEYS = frozenset(
    {
        "policy_id",
        "version",
        "patient_visible",
        "patient_masked",
        "redact_index_diagnosis_terms_in",
        "doctor_visible",
    }
)
_OPTIONAL_KEYS = frozenset({"rationale", "deprecated", "supersedes"})


class ParticipantRole(str, Enum):
    """Who is asking. Distinct from the chat ``Role`` in :mod:`meddial.llm`."""

    PATIENT = "patient"
    DOCTOR = "doctor"
    EVALUATOR = "evaluator"


class PolicyError(Exception):
    """Base class for every knowledge-policy failure."""


class PolicyValidationError(PolicyError):
    """A policy file is malformed, incomplete, or has drifted from its hash."""


class UnknownPolicyError(PolicyError):
    """No policy with the requested id or version is registered."""


class DeprecatedPolicyError(PolicyError):
    """A confirmatory run asked for a retired arm."""


@dataclass(frozen=True)
class KnowledgePolicy:
    """One disclosure condition, applied to a reference to build contexts."""

    policy_id: str
    version: str
    patient_visible: frozenset[str]
    patient_masked: frozenset[str]
    redact_index_diagnosis_terms_in: frozenset[str]
    doctor_visible: frozenset[str]
    rationale: str = ""
    deprecated: bool = False

    @property
    def key(self) -> str:
        return f"{self.policy_id}@{self.version}"

    @property
    def content_hash(self) -> str:
        """SHA-256 of everything except the version.

        Excluding the version is the point: if the body changes and the
        version does not, the hash no longer matches the recorded one.
        """
        body = {
            "policy_id": self.policy_id,
            "patient_visible": sorted(self.patient_visible),
            "patient_masked": sorted(self.patient_masked),
            "redact_index_diagnosis_terms_in": sorted(
                self.redact_index_diagnosis_terms_in
            ),
            "doctor_visible": sorted(self.doctor_visible),
            "rationale": self.rationale,
            "deprecated": self.deprecated,
        }
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> KnowledgePolicy:
        missing = _REQUIRED_KEYS - set(data)
        if missing:
            raise PolicyValidationError(
                f"policy is missing required keys: {', '.join(sorted(missing))}"
            )
        unknown = set(data) - _REQUIRED_KEYS - _OPTIONAL_KEYS
        if unknown:
            raise PolicyValidationError(
                f"policy has unrecognised keys: {', '.join(sorted(unknown))}"
            )
        policy = cls(
            policy_id=str(data["policy_id"]),
            version=str(data["version"]),
            patient_visible=frozenset(data["patient_visible"]),
            patient_masked=frozenset(data["patient_masked"]),
            redact_index_diagnosis_terms_in=frozenset(
                data["redact_index_diagnosis_terms_in"]
            ),
            doctor_visible=frozenset(data["doctor_visible"]),
            rationale=str(data.get("rationale", "")),
            deprecated=bool(data.get("deprecated", False)),
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        """Raise unless this policy is complete and internally consistent."""
        addressable = fieldpath.addressable_paths()
        declared = (
            self.patient_visible
            | self.patient_masked
            | self.redact_index_diagnosis_terms_in
            | self.doctor_visible
        )
        unknown = declared - addressable
        if unknown:
            raise PolicyValidationError(
                f"{self.key} names paths that do not exist on the reference: "
                f"{', '.join(sorted(unknown))}"
            )

        overlap = self.patient_visible & self.patient_masked
        if overlap:
            raise PolicyValidationError(
                f"{self.key} lists {', '.join(sorted(overlap))} as both visible "
                "and masked"
            )

        # Redacting a field the patient cannot see is a no-op, and almost
        # always means the author meant to keep the field.
        for path in self.redact_index_diagnosis_terms_in:
            if not any(
                path == v or path.startswith((f"{v}.", f"{v}["))
                for v in self.patient_visible
            ):
                raise PolicyValidationError(
                    f"{self.key} redacts {path}, which is not patient-visible"
                )

        # KNOW-3: every surface field must be classified, so a field added to
        # the reference cannot become visible by default.
        covered = {
            surface
            for path in (self.patient_visible | self.patient_masked)
            if (surface := fieldpath.covering_surface(path)) is not None
        }
        unclassified = fieldpath.policy_surface() - covered
        if unclassified:
            raise PolicyValidationError(
                f"{self.key} does not classify {', '.join(sorted(unclassified))}; "
                "every field must be explicitly visible or masked"
            )

    def mask(
        self, reference: StructuredClinicalReference, role: ParticipantRole
    ) -> Mapping[str, Any]:
        """The payload ``role`` is allowed to see for this reference."""
        if role is ParticipantRole.EVALUATOR:
            # KNOW-7: the evaluator is privileged and sees everything,
            # evidence included.
            return reference.model_dump(mode="json")
        if role is ParticipantRole.DOCTOR:
            return self._project(reference, self.doctor_visible)
        payload, _ = self.mask_with_report(reference)
        return payload

    def mask_with_report(
        self, reference: StructuredClinicalReference
    ) -> tuple[dict[str, Any], RedactionReport]:
        """The patient payload, plus what redaction removed from it."""
        payload = self._project(reference, self.patient_visible)
        for path in sorted(self.patient_masked):
            fieldpath.drop(payload, path)

        terms = index_diagnosis_terms(reference)
        replacements: dict[str, int] = {}
        for path in sorted(self.redact_index_diagnosis_terms_in):
            count = 0
            for container, key in fieldpath.locations(payload, path):
                container[key], removed = redact_value(container[key], terms)
                count += removed
            replacements[path] = count
        return payload, RedactionReport(terms=terms, replacements=replacements)

    def _project(
        self, reference: StructuredClinicalReference, paths: frozenset[str]
    ) -> dict[str, Any]:
        source = fieldpath.strip_evidence(reference.model_dump(mode="json"))
        return fieldpath.project(source, paths)


def _resolve_directory(directory: Path | str | None) -> Path:
    if directory is not None:
        return Path(directory)
    override = os.getenv(POLICY_DIR_ENV)
    return Path(override) if override else DEFAULT_POLICY_DIR


class PolicyRegistry:
    """The policies on disk, keyed by ``policy_id@version``."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = _resolve_directory(directory)
        self._policies: dict[str, KnowledgePolicy] | None = None
        self._hashes: dict[str, str] | None = None

    def _load_hashes(self) -> dict[str, str]:
        if self._hashes is None:
            lock = self.directory / HASH_LOCK_FILENAME
            self._hashes = json.loads(lock.read_text()) if lock.exists() else {}
        return self._hashes

    def _load(self) -> dict[str, KnowledgePolicy]:
        if self._policies is not None:
            return self._policies
        if not self.directory.is_dir():
            raise PolicyValidationError(f"no policy directory at {self.directory}")
        policies: dict[str, KnowledgePolicy] = {}
        for path in sorted(self.directory.glob("*.json")):
            if path.name == HASH_LOCK_FILENAME:
                continue
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                raise PolicyValidationError(
                    f"{path.name} is not valid JSON: {exc}"
                ) from exc
            policy = KnowledgePolicy.from_mapping(data)
            if policy.key in policies:
                raise PolicyValidationError(f"{policy.key} is defined twice")
            policies[policy.key] = policy
        self._verify_hashes(policies)
        self._policies = policies
        return policies

    def _verify_hashes(self, policies: Mapping[str, KnowledgePolicy]) -> None:
        """KNOW-4: a body change without a version bump must fail."""
        recorded = self._load_hashes()
        if not recorded:
            return
        for key, policy in policies.items():
            expected = recorded.get(key)
            if expected is None:
                raise PolicyValidationError(
                    f"{key} has no recorded hash. If this is a new version, add it "
                    f"to {HASH_LOCK_FILENAME}; if you edited an existing policy, "
                    "bump its version instead."
                )
            if expected != policy.content_hash:
                raise PolicyValidationError(
                    f"{key} has changed since its hash was recorded "
                    f"({policy.content_hash[:12]} != {expected[:12]}). Bump the "
                    "version rather than editing an existing one in place."
                )

    def all(self) -> list[KnowledgePolicy]:
        return list(self._load().values())

    def versions(self, policy_id: str) -> list[KnowledgePolicy]:
        return [p for p in self.all() if p.policy_id == policy_id]

    def load(self, policy_id: str, version: str | None = None) -> KnowledgePolicy:
        """A policy by id; without a version, the newest active one."""
        if version is not None:
            try:
                return self._load()[f"{policy_id}@{version}"]
            except KeyError:
                raise UnknownPolicyError(f"no policy {policy_id}@{version}") from None
        candidates = [p for p in self.versions(policy_id) if not p.deprecated]
        if not candidates:
            raise UnknownPolicyError(f"no active policy with id {policy_id}")
        return max(candidates, key=lambda p: _version_key(p.version))

    def for_confirmatory_run(
        self, policy_id: str, version: str | None = None
    ) -> KnowledgePolicy:
        """As :meth:`load`, but refuses a retired arm.

        Exploratory work may replay a deprecated policy; a confirmatory
        result may not be reported under one (EXP-4).
        """
        policy = self.load(policy_id, version)
        if policy.deprecated:
            raise DeprecatedPolicyError(
                f"{policy.key} is deprecated and cannot be used for a "
                "confirmatory run; it is retained only as a comparison arm"
            )
        return policy


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def load_policies(directory: Path | str | None = None) -> Iterable[KnowledgePolicy]:
    """Convenience for callers that just want every registered policy."""
    return PolicyRegistry(directory).all()
