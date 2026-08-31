"""Private and safely pseudonymised cohort manifests (COH-3, GOV-6)."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from meddial.cohort.select import CohortSelection

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CRITERIA_SQL = _REPO_ROOT / "configs" / "cohort" / "criteria_v1.sql"

PRIVATE_MANIFEST_VERSION = "1.0-private"
RELEASE_MANIFEST_VERSION = "1.0-release"


class ManifestValidationError(ValueError):
    """A manifest is inconsistent, modified, or unsafe to publish."""


def create_private_manifest(
    selection: CohortSelection,
    *,
    criteria_sql_path: str | Path = DEFAULT_CRITERIA_SQL,
) -> dict[str, Any]:
    """Serialise the full auditable selection for credentialed storage only."""
    sql_path = Path(criteria_sql_path)
    if not sql_path.is_file():
        raise ManifestValidationError(f"criteria SQL does not exist: {sql_path}")

    payload: dict[str, Any] = {
        "manifest_version": PRIVATE_MANIFEST_VERSION,
        "data_classification": "restricted_clinical",
        "publishable": False,
        "criteria": {
            "key": selection.criteria.key,
            "content_hash": selection.criteria.content_hash,
            "sql_file": sql_path.name,
            "sql_sha256": _file_hash(sql_path),
        },
        "source_snapshot_hash": selection.source_snapshot_hash,
        "sampling_seed": selection.seed,
        "requested_n": selection.requested_n,
        "candidate_pool_size": selection.candidate_pool_size,
        "eligible_pool_size": selection.eligible_pool_size,
        "n_cases": selection.n_cases,
        "cohort_hash": selection.cohort_hash,
        "exclusion_flow": [row.to_dict() for row in selection.stage_counts],
        "selected": [
            {
                "subject_id": row.subject_id,
                "hadm_id": row.hadm_id,
                "row_id": row.row_id,
                "selection_order": order,
            }
            for order, row in enumerate(selection.selected, start=1)
        ],
        "audit": [row.to_private_dict() for row in selection.audit],
    }
    payload["manifest_hash"] = manifest_hash(payload)
    return payload


def create_release_manifest(
    private_manifest: Mapping[str, Any],
    *,
    publication_salt: str,
) -> dict[str, Any]:
    """Create a public manifest containing no raw or reversibly hashed IDs.

    The private cohort hash and source snapshot hash are omitted: both are
    unsalted commitments over restricted material.  A salted HMAC commitment
    lets the authors later demonstrate that a release came from a particular
    private manifest without giving readers a dictionary-attack target.
    """
    _verify_private_manifest(private_manifest)
    if len(publication_salt) < 16:
        raise ManifestValidationError("publication_salt must contain at least 16 characters")

    selected: list[dict[str, Any]] = []
    for entry in private_manifest.get("selected", []):
        identity = f"{entry['subject_id']}:{entry['hadm_id']}"
        pseudonym = hmac.new(
            publication_salt.encode("utf-8"),
            identity.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:24]
        selected.append(
            {
                "study_id": f"MEDDIAL-{pseudonym}",
                "selection_order": int(entry["selection_order"]),
            }
        )

    private_hash = str(private_manifest["manifest_hash"])
    commitment = hmac.new(
        publication_salt.encode("utf-8"),
        private_hash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    payload: dict[str, Any] = {
        "manifest_version": RELEASE_MANIFEST_VERSION,
        "data_classification": "public_metadata_only",
        "publishable": True,
        "criteria": dict(private_manifest["criteria"]),
        "sampling_seed": int(private_manifest["sampling_seed"]),
        "requested_n": int(private_manifest["requested_n"]),
        "candidate_pool_size": int(private_manifest["candidate_pool_size"]),
        "eligible_pool_size": int(private_manifest["eligible_pool_size"]),
        "n_cases": int(private_manifest["n_cases"]),
        "exclusion_flow": list(private_manifest["exclusion_flow"]),
        "private_manifest_commitment": commitment,
        "selected": selected,
    }
    payload["manifest_hash"] = manifest_hash(payload)
    return payload


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    """Hash a manifest independently of mapping insertion order."""
    body = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def verify_manifest(manifest: Mapping[str, Any]) -> None:
    """Raise if the recorded manifest hash does not cover the current body."""
    recorded = manifest.get("manifest_hash")
    if not isinstance(recorded, str) or not recorded:
        raise ManifestValidationError("manifest has no manifest_hash")
    expected = manifest_hash(manifest)
    if not hmac.compare_digest(recorded, expected):
        raise ManifestValidationError(
            f"manifest body has changed ({expected[:12]} != {recorded[:12]})"
        )


def _verify_private_manifest(manifest: Mapping[str, Any]) -> None:
    verify_manifest(manifest)
    if manifest.get("manifest_version") != PRIVATE_MANIFEST_VERSION:
        raise ManifestValidationError("release source is not a private v1 manifest")
    if manifest.get("publishable") is not False:
        raise ManifestValidationError("release source is not marked private")
    if len(manifest.get("selected", [])) != int(manifest.get("n_cases", -1)):
        raise ManifestValidationError("private manifest selected count is inconsistent")


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
