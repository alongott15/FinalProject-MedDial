"""Immutable attempt records and run/config isolation."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meddial.experiments.config import ExperimentConfig


class ResumeConfigurationMismatch(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_once(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


@dataclass(frozen=True)
class AttemptRecord:
    run_id: str
    config_hash: str
    profile_id: str
    profile_type: str
    attempt: int
    status: str
    accepted: bool
    started_at: str
    duration_seconds: float
    dialogue: tuple[Mapping[str, str], ...] = field(default_factory=tuple)
    transcript: str | None = None
    evaluation: Mapping[str, Any] = field(default_factory=dict)
    failure_class: str | None = None
    error: str | None = None
    model_calls: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    record_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def record_id(self) -> str:
        safe_profile = self.profile_id.replace("/", "_")
        return f"{safe_profile}_{self.profile_type}_attempt-{self.attempt:02d}"


class AttemptStore:
    def __init__(self, run_dir: Path, run_id: str, config_hash: str) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.config_hash = config_hash
        self.records_dir = run_dir / "attempt_records"
        self.outcomes_dir = run_dir / "outcomes"
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.outcomes_dir.mkdir(parents=True, exist_ok=True)

    def append(self, record: AttemptRecord) -> Path:
        if record.run_id != self.run_id or record.config_hash != self.config_hash:
            raise ResumeConfigurationMismatch("Attempt record does not belong to this run/config")
        path = self.records_dir / f"{record.record_id}.json"
        _write_json_once(path, record.to_dict())
        return path

    def finalize(self, profile_id: str, profile_type: str, outcome: Mapping[str, Any]) -> Path:
        safe_profile = profile_id.replace("/", "_")
        path = self.outcomes_dir / f"{safe_profile}_{profile_type}.json"
        payload = {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "profile_id": profile_id,
            "profile_type": profile_type,
            "completed_at": _utc_now(),
            **dict(outcome),
        }
        _write_json_once(path, payload)
        return path

    def is_complete(self, profile_id: str, profile_type: str) -> bool:
        safe_profile = profile_id.replace("/", "_")
        return (self.outcomes_dir / f"{safe_profile}_{profile_type}.json").exists()

    def load_attempts(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.records_dir.glob("*.json")):
            with path.open(encoding="utf-8") as handle:
                record = json.load(handle)
            if record.get("run_id") != self.run_id or record.get("config_hash") != self.config_hash:
                raise ResumeConfigurationMismatch(f"Contaminated record: {path}")
            records.append(record)
        return records

    def attempts_for(self, profile_id: str, profile_type: str) -> list[dict[str, Any]]:
        return [
            record
            for record in self.load_attempts()
            if record.get("profile_id") == profile_id and record.get("profile_type") == profile_type
        ]


@dataclass(frozen=True)
class RunContext:
    run_id: str
    config_hash: str
    run_dir: Path
    config: ExperimentConfig


class RunManager:
    def __init__(self, output_root: str | Path) -> None:
        self.output_root = Path(output_root)
        self.runs_root = self.output_root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def resolve(
        self,
        config: ExperimentConfig,
        requested_run_id: str | None = None,
        resume: bool = True,
    ) -> RunContext:
        run_id = requested_run_id
        latest_path = self.output_root / "latest_run.json"
        if run_id is None and resume and latest_path.exists():
            with latest_path.open(encoding="utf-8") as handle:
                latest = json.load(handle)
            if latest.get("config_hash") == config.config_hash:
                run_id = str(latest["run_id"])
        if run_id is None:
            run_id = (
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
            )

        run_dir = self.runs_root / run_id
        manifest_path = run_dir / "run_manifest.json"
        if manifest_path.exists():
            with manifest_path.open(encoding="utf-8") as handle:
                manifest = json.load(handle)
            if manifest.get("config_hash") != config.config_hash:
                raise ResumeConfigurationMismatch(
                    f"Run {run_id} has config hash {manifest.get('config_hash')}; "
                    f"requested {config.config_hash}"
                )
        else:
            run_dir.mkdir(parents=True, exist_ok=False)
            _write_json_once(
                manifest_path,
                {
                    "run_id": run_id,
                    "config_hash": config.config_hash,
                    "created_at": _utc_now(),
                    "config": config.to_dict(),
                },
            )
        _atomic_json(
            latest_path,
            {"run_id": run_id, "config_hash": config.config_hash, "updated_at": _utc_now()},
        )
        return RunContext(run_id, config.config_hash, run_dir, config)
