"""Reproducible run orchestration and immutable attempt records (EXP-2/3/7).

The runner separates three identities that were previously conflated:

* the canonical run configuration;
* the input manifest; and
* the prompt set.

All three hashes, plus the source commit, form a run's full identity.  Resume
checks every component before doing work.  Attempt records are newline-delimited
JSON opened only with ``O_APPEND``; a duplicate record id raises, and every line
carries its own content hash so external mutation is detected on read.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

from meddial.knowledge import PolicyRegistry

from .config import RunConfig
from .repair import repair_from_evaluation
from .variants import (
    VariantBackend,
    VariantRegistry,
    VariantRequest,
    default_variant_registry,
)

RUN_MANIFEST_VERSION = "2.0"
ATTEMPT_RECORD_VERSION = "2.0"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ExperimentRunError(RuntimeError):
    """Base class for experiment-record and orchestration failures."""


class ResumeHashMismatch(ExperimentRunError):
    """An existing run directory belongs to a different experimental identity."""


class ImmutableAttemptError(ExperimentRunError):
    """Appending would replace or duplicate an existing attempt record."""


class AttemptRecordError(ExperimentRunError):
    """An attempt line is malformed, contaminated, or has been modified."""


class VariantExecutionError(ExperimentRunError):
    """The selected architecture failed after the failure record was persisted."""


# Compatibility with the terminology used by the earlier reference branch.
ResumeConfigurationMismatch = ResumeHashMismatch


def _jsonable(value: Any) -> Any:
    if hasattr(value, "as_record") and callable(value.as_record):
        return _jsonable(value.as_record())
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, (str, int, float, bool)):
        return value.value
    if isinstance(value, Path):
        return str(value)
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _current_git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _write_json_once(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        payload = json.dumps(_jsonable(value), indent=2, sort_keys=True, ensure_ascii=False)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-.")
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:72] or 'case'}-{digest}"


@dataclass(frozen=True)
class RunHashes:
    config_hash: str
    input_manifest_hash: str
    prompt_set_hash: str
    git_commit: str
    full_hash: str

    @classmethod
    def from_config(cls, config: RunConfig, git_commit: str) -> RunHashes:
        parts = {
            "config_hash": config.config_hash(),
            "input_manifest_hash": config.input_manifest_hash,
            "prompt_set_hash": config.prompt_set_hash(),
            "git_commit": git_commit,
        }
        return cls(**parts, full_hash=_sha256(parts))

    def as_record(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class RunContext:
    run_id: str
    run_dir: Path
    config: RunConfig
    hashes: RunHashes
    manifest: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest", MappingProxyType(dict(self.manifest)))

    @property
    def config_hash(self) -> str:
        return self.hashes.config_hash

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "run_manifest.json"

    @property
    def attempts_path(self) -> Path:
        return self.run_dir / "attempts" / "attempts.jsonl"

    @property
    def outcomes_dir(self) -> Path:
        return self.run_dir / "outcomes"


class RunManager:
    """Create a run or resume it only when every identity hash matches."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        git_commit: str | None = None,
        policy_registry: PolicyRegistry | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.git_commit = git_commit or _current_git_commit()
        self.policy_registry = policy_registry

    def resolve(
        self,
        config: RunConfig,
        *,
        requested_run_id: str | None = None,
        confirmatory: bool = False,
        provider: Mapping[str, Any] | None = None,
        environment: Mapping[str, Any] | None = None,
        cohort: Mapping[str, Any] | None = None,
    ) -> RunContext:
        policy = config.validate(
            policy_registry=self.policy_registry,
            confirmatory=confirmatory,
        )
        hashes = RunHashes.from_config(config, self.git_commit)
        run_id = requested_run_id or self._default_run_id(config, hashes)
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError(
                "run_id must contain only letters, digits, dot, underscore, and hyphen"
            )

        run_dir = self.output_root / "runs" / run_id
        manifest_path = run_dir / "run_manifest.json"
        if manifest_path.exists():
            manifest = self._read_manifest(manifest_path)
            self._assert_resume_identity(run_id, manifest, hashes)
            return RunContext(run_id, run_dir, config, hashes, manifest)
        if run_dir.exists() and any(run_dir.iterdir()):
            raise ResumeHashMismatch(
                f"run directory {run_dir} exists without an immutable run_manifest.json"
            )

        manifest = {
            "manifest_version": RUN_MANIFEST_VERSION,
            "run_id": run_id,
            "created_utc": _utc_now(),
            **hashes.as_record(),
            "frozen_at": config.frozen_at,
            "config": config.as_record(),
            "models": {
                role: model.as_record() for role, model in sorted(config.models.items())
            },
            "provider": dict(provider or {}),
            "environment": {
                "python": platform.python_version(),
                "implementation": platform.python_implementation(),
                "platform": platform.platform(),
                **dict(environment or {}),
            },
            "cohort": {
                "input_manifest_hash": config.input_manifest_hash,
                **dict(cohort or {}),
            },
            "resolved_policy": {
                "id": policy.policy_id,
                "version": policy.version,
                "content_hash": policy.content_hash,
                "deprecated": policy.deprecated,
            },
            "confirmatory": confirmatory,
        }
        _write_json_once(manifest_path, manifest)
        return RunContext(run_id, run_dir, config, hashes, manifest)

    @staticmethod
    def _default_run_id(config: RunConfig, hashes: RunHashes) -> str:
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", config.name).strip("-.")[:36]
        return f"run_{name or 'experiment'}_{hashes.full_hash[:16]}"

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResumeHashMismatch(f"cannot read existing manifest {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ResumeHashMismatch(f"existing manifest {path} is not a JSON object")
        return value

    @staticmethod
    def _assert_resume_identity(
        run_id: str, manifest: Mapping[str, Any], expected: RunHashes
    ) -> None:
        differences = [
            name
            for name, value in expected.as_record().items()
            if manifest.get(name) != value
        ]
        if differences:
            detail = ", ".join(
                f"{name}={manifest.get(name)!r} (requested {getattr(expected, name)!r})"
                for name in differences
            )
            raise ResumeHashMismatch(f"run {run_id!r} hash mismatch: {detail}")


def derive_attempt_seed(
    base_seed: int,
    case_id: str,
    patient_policy_ref: str,
    attempt_index: int,
    *,
    stream: str = "generation",
) -> int:
    """Derive a stable 31-bit seed without depending on Python's salted hash."""
    if attempt_index < 1:
        raise ValueError("attempt_index must be >= 1")
    payload = {
        "base_seed": base_seed,
        "case_id": str(case_id),
        "patient_policy_ref": str(patient_policy_ref),
        "attempt_index": attempt_index,
        "stream": stream,
    }
    return int(_sha256(payload)[:16], 16) % (2**31)


@dataclass(frozen=True)
class AttemptRecord:
    dialogue_id: str
    run_id: str
    config_hash: str
    input_manifest_hash: str
    prompt_set_hash: str
    case_id: str
    variant: str
    attempt_index: int
    inputs: Mapping[str, Any]
    dialogue: tuple[Mapping[str, Any], ...]
    evaluation: Mapping[str, Any]
    repair: Mapping[str, Any] | None
    model_calls: tuple[Mapping[str, Any], ...]
    cost: Mapping[str, Any]
    record_version: str = ATTEMPT_RECORD_VERSION

    def __post_init__(self) -> None:
        for name in (
            "dialogue_id",
            "run_id",
            "config_hash",
            "input_manifest_hash",
            "prompt_set_hash",
            "case_id",
            "variant",
        ):
            if not str(getattr(self, name)).strip():
                raise AttemptRecordError(f"{name} is required")
        if self.attempt_index < 1:
            raise AttemptRecordError("attempt_index must be >= 1")
        object.__setattr__(self, "inputs", MappingProxyType(dict(_jsonable(self.inputs))))
        object.__setattr__(
            self,
            "dialogue",
            tuple(MappingProxyType(dict(_jsonable(turn))) for turn in self.dialogue),
        )
        object.__setattr__(
            self, "evaluation", MappingProxyType(dict(_jsonable(self.evaluation)))
        )
        if self.repair is not None:
            object.__setattr__(
                self, "repair", MappingProxyType(dict(_jsonable(self.repair)))
            )
        object.__setattr__(
            self,
            "model_calls",
            tuple(MappingProxyType(dict(_jsonable(call))) for call in self.model_calls),
        )
        object.__setattr__(self, "cost", MappingProxyType(dict(_jsonable(self.cost))))

    @property
    def record_id(self) -> str:
        return f"{self.dialogue_id}::{self.attempt_index}"

    def _body(self) -> dict[str, Any]:
        return {
            "record_version": self.record_version,
            "dialogue_id": self.dialogue_id,
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "input_manifest_hash": self.input_manifest_hash,
            "prompt_set_hash": self.prompt_set_hash,
            "case_id": self.case_id,
            "variant": self.variant,
            "attempt_index": self.attempt_index,
            "inputs": _jsonable(self.inputs),
            "dialogue": _jsonable(self.dialogue),
            "evaluation": _jsonable(self.evaluation),
            "repair": _jsonable(self.repair),
            "model_calls": _jsonable(self.model_calls),
            "cost": _jsonable(self.cost),
        }

    @property
    def record_hash(self) -> str:
        return _sha256(self._body())

    def as_record(self) -> dict[str, Any]:
        return {**self._body(), "record_hash": self.record_hash}

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> AttemptRecord:
        data = dict(value)
        recorded_hash = data.pop("record_hash", None)
        try:
            record = cls(
                dialogue_id=str(data.pop("dialogue_id")),
                run_id=str(data.pop("run_id")),
                config_hash=str(data.pop("config_hash")),
                input_manifest_hash=str(data.pop("input_manifest_hash")),
                prompt_set_hash=str(data.pop("prompt_set_hash")),
                case_id=str(data.pop("case_id")),
                variant=str(data.pop("variant")),
                attempt_index=int(data.pop("attempt_index")),
                inputs=data.pop("inputs"),
                dialogue=tuple(data.pop("dialogue")),
                evaluation=data.pop("evaluation"),
                repair=data.pop("repair", None),
                model_calls=tuple(data.pop("model_calls", ())),
                cost=data.pop("cost"),
                record_version=str(data.pop("record_version", ATTEMPT_RECORD_VERSION)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AttemptRecordError(f"invalid attempt record: {exc}") from exc
        if data:
            raise AttemptRecordError(
                f"attempt record has unknown fields: {', '.join(sorted(data))}"
            )
        if recorded_hash is None or recorded_hash != record.record_hash:
            raise AttemptRecordError(
                f"attempt {record.record_id!r} content hash does not match"
            )
        return record

    @classmethod
    def synthetic_for_test(cls, context: RunContext, *, case_id: str) -> AttemptRecord:
        """Small public fixture constructor; never contains clinical data."""
        seed = derive_attempt_seed(
            context.config.seed, case_id, context.config.patient_policy_ref, 1
        )
        return cls(
            dialogue_id=(
                f"{case_id}_{context.config.patient_policy_ref}_"
                f"{context.config.variant}_a1"
            ),
            run_id=context.run_id,
            config_hash=context.hashes.config_hash,
            input_manifest_hash=context.hashes.input_manifest_hash,
            prompt_set_hash=context.hashes.prompt_set_hash,
            case_id=case_id,
            variant=context.config.variant,
            attempt_index=1,
            inputs={
                "scr_hash": f"sha256:{_sha256({'case_id': case_id})}",
                "policy": {
                    "id": context.config.patient_policy_id,
                    "version": context.config.patient_policy_version,
                },
                "doctor_guidance_id": context.config.doctor_guidance_id,
                "seed": seed,
                "prompt_versions": dict(context.config.prompt_versions),
            },
            dialogue=(
                {"index": 0, "role": "Doctor", "text": "How are you feeling?"},
                {"index": 1, "role": "Patient", "text": "I feel tired."},
            ),
            evaluation={
                "scores": {"naturalness": {"value": 0.9, "status": "pass"}},
                "acceptance": {"overall": "ACCEPT", "per_dimension": {}},
            },
            repair=None,
            model_calls=(),
            cost={
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "wall_ms": 0,
                "estimated_cost": 0.0,
            },
        )


class AttemptLog:
    """Append-only JSONL storage bound to one run's full identity."""

    def __init__(self, context: RunContext) -> None:
        self.context = context

    def read(self) -> list[AttemptRecord]:
        path = self.context.attempts_path
        if not path.exists():
            return []
        records: list[AttemptRecord] = []
        seen: set[str] = set()
        with path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    value = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise AttemptRecordError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
                if not isinstance(value, Mapping):
                    raise AttemptRecordError(f"{path}:{line_no}: expected a JSON object")
                record = AttemptRecord.from_record(value)
                self._assert_belongs(record)
                if record.record_id in seen:
                    raise AttemptRecordError(
                        f"{path}:{line_no}: duplicate immutable record {record.record_id!r}"
                    )
                seen.add(record.record_id)
                records.append(record)
        return records

    def append(self, record: AttemptRecord) -> None:
        self._assert_belongs(record)
        if any(existing.record_id == record.record_id for existing in self.read()):
            raise ImmutableAttemptError(
                f"attempt {record.record_id!r} already exists and cannot be rewritten"
            )

        path = self.context.attempts_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(record.as_record(), sort_keys=True, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise AttemptRecordError(
                    f"short append to {path}: wrote {written} of {len(payload)} bytes"
                )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _assert_belongs(self, record: AttemptRecord) -> None:
        mismatches = []
        expected = {
            "run_id": self.context.run_id,
            "config_hash": self.context.hashes.config_hash,
            "input_manifest_hash": self.context.hashes.input_manifest_hash,
            "prompt_set_hash": self.context.hashes.prompt_set_hash,
        }
        for field_name, expected_value in expected.items():
            if getattr(record, field_name) != expected_value:
                mismatches.append(field_name)
        if mismatches:
            raise ResumeHashMismatch(
                f"attempt record does not belong to this run: {', '.join(mismatches)}"
            )


@dataclass(frozen=True)
class ExperimentRunResult:
    context: RunContext
    attempts: tuple[AttemptRecord, ...]
    outcomes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcomes", MappingProxyType(dict(self.outcomes)))


class ExperimentRunner:
    """Run cases through one registered architecture with resumable attempts."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        variants: VariantRegistry | None = None,
        policy_registry: PolicyRegistry | None = None,
        git_commit: str | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.variants = variants if variants is not None else default_variant_registry()
        self.manager = RunManager(
            self.output_root,
            git_commit=git_commit,
            policy_registry=policy_registry,
        )

    def run(
        self,
        config: RunConfig,
        cases: Iterable[Mapping[str, Any]],
        backend: VariantBackend,
        *,
        requested_run_id: str | None = None,
        confirmatory: bool = False,
        provider: Mapping[str, Any] | None = None,
        environment: Mapping[str, Any] | None = None,
        cohort: Mapping[str, Any] | None = None,
    ) -> ExperimentRunResult:
        # Resolve before touching disk: an advertised but absent implementation
        # must not leave behind a run that appears to have started.
        implementation = self.variants.resolve(config.variant)
        context = self.manager.resolve(
            config,
            requested_run_id=requested_run_id,
            confirmatory=confirmatory,
            provider=provider,
            environment=environment,
            cohort=cohort,
        )
        log = AttemptLog(context)
        existing = log.read()
        by_case: dict[str, list[AttemptRecord]] = {}
        for record in existing:
            by_case.setdefault(record.case_id, []).append(record)

        normalized_cases = self._normalise_cases(cases)
        outcomes = self._read_outcomes(context)
        for case_id, case in normalized_cases:
            if case_id in outcomes:
                continue

            prior = sorted(by_case.get(case_id, []), key=lambda record: record.attempt_index)
            accepted_prior = next((record for record in prior if _accepted(record.evaluation)), None)
            if accepted_prior is not None:
                outcomes[case_id] = self._finalize_outcome(context, prior, accepted_prior)
                continue
            if len(prior) >= config.max_attempts:
                outcomes[case_id] = self._finalize_outcome(context, prior, prior[-1])
                continue

            for attempt_index in range(len(prior) + 1, config.max_attempts + 1):
                repair = None
                if prior:
                    repair_plan = repair_from_evaluation(prior[-1].evaluation)
                    if repair_plan.actions:
                        repair = repair_plan.as_record()
                seed = derive_attempt_seed(
                    config.seed,
                    case_id,
                    config.patient_policy_ref,
                    attempt_index,
                )
                request = VariantRequest(
                    case_id=case_id,
                    case=case,
                    config=config,
                    attempt_index=attempt_index,
                    seed=seed,
                    repair=repair,
                    prior_attempts=tuple(record.as_record() for record in prior),
                )

                failure: Exception | None = None
                try:
                    output = implementation.execute(backend, request)
                except Exception as exc:  # record the failed attempt, then fail closed
                    failure = exc
                    output = {
                        "dialogue": [],
                        "evaluation": {
                            "scores": {},
                            "acceptance": {"overall": "INCOMPLETE", "per_dimension": {}},
                            "execution_error": {
                                "class": type(exc).__name__,
                                "message": str(exc),
                            },
                        },
                        "accepted": False,
                        "model_calls": [],
                    }

                record = self._attempt_record(context, case_id, case, request, output)
                log.append(record)
                prior.append(record)
                by_case.setdefault(case_id, []).append(record)
                if failure is not None:
                    raise VariantExecutionError(
                        f"{implementation.implementation_id} failed for {case_id} attempt "
                        f"{attempt_index}; the immutable failure record was written"
                    ) from failure
                if _accepted(record.evaluation):
                    break

            final = next((record for record in prior if _accepted(record.evaluation)), prior[-1])
            outcomes[case_id] = self._finalize_outcome(context, prior, final)

        return ExperimentRunResult(context, tuple(log.read()), outcomes)

    @staticmethod
    def _normalise_cases(
        cases: Iterable[Mapping[str, Any]],
    ) -> list[tuple[str, dict[str, Any]]]:
        normalized: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for raw in cases:
            if not isinstance(raw, Mapping):
                raise TypeError("each case must be a mapping")
            case = dict(raw)
            case_id = str(case.get("case_id") or case.get("profile_id") or "").strip()
            if not case_id:
                raise ValueError("each case requires case_id")
            if case_id in seen:
                raise ValueError(f"duplicate case_id {case_id!r}")
            seen.add(case_id)
            normalized.append((case_id, case))
        return sorted(normalized, key=lambda pair: pair[0])

    @staticmethod
    def _attempt_record(
        context: RunContext,
        case_id: str,
        case: Mapping[str, Any],
        request: VariantRequest,
        output: Mapping[str, Any],
    ) -> AttemptRecord:
        evaluation = output.get("evaluation", {})
        if not isinstance(evaluation, Mapping):
            raise AttemptRecordError("variant output evaluation must be a mapping")
        model_calls = output.get("model_calls", ())
        if not isinstance(model_calls, Sequence) or isinstance(model_calls, (str, bytes)):
            raise AttemptRecordError("variant output model_calls must be a sequence")
        calls = tuple(_jsonable(call) for call in model_calls)
        cost = _summarize_cost(calls, output.get("cost"))
        dialogue = _normalise_dialogue(output.get("dialogue", ()))
        scr_hash = str(case.get("scr_hash") or f"sha256:{_sha256(case)}")
        dialogue_id = (
            f"{case_id}_{context.config.patient_policy_ref}_"
            f"{context.config.variant}_a{request.attempt_index}"
        )
        inputs: dict[str, Any] = {
            "case_id": case_id,
            "scr_hash": scr_hash,
            "policy": {
                "id": context.config.patient_policy_id,
                "version": context.config.patient_policy_version,
            },
            "doctor_guidance_id": context.config.doctor_guidance_id,
            "seed": request.seed,
            "prompt_versions": dict(context.config.prompt_versions),
        }
        for optional in ("contexts_hash", "reference_hash"):
            if optional in case:
                inputs[optional] = case[optional]
        return AttemptRecord(
            dialogue_id=dialogue_id,
            run_id=context.run_id,
            config_hash=context.hashes.config_hash,
            input_manifest_hash=context.hashes.input_manifest_hash,
            prompt_set_hash=context.hashes.prompt_set_hash,
            case_id=case_id,
            variant=context.config.variant,
            attempt_index=request.attempt_index,
            inputs=inputs,
            dialogue=dialogue,
            evaluation=evaluation,
            repair=request.repair,
            model_calls=calls,
            cost=cost,
        )

    @staticmethod
    def _outcome_path(context: RunContext, case_id: str) -> Path:
        return context.outcomes_dir / f"{_safe_component(case_id)}.json"

    @classmethod
    def _finalize_outcome(
        cls,
        context: RunContext,
        attempts: Sequence[AttemptRecord],
        final: AttemptRecord,
    ) -> dict[str, Any]:
        path = cls._outcome_path(context, final.case_id)
        outcome = {
            "run_id": context.run_id,
            "config_hash": context.hashes.config_hash,
            "case_id": final.case_id,
            "patient_policy": context.config.patient_policy_ref,
            "variant": context.config.variant,
            "status": "accepted" if _accepted(final.evaluation) else "terminal_failure",
            "attempts": len(attempts),
            "final_record_id": final.record_id,
            "final_record_hash": final.record_hash,
        }
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != outcome:
                raise ImmutableAttemptError(f"outcome {path} already exists with different content")
            return existing
        _write_json_once(path, outcome)
        return outcome

    @classmethod
    def _read_outcomes(cls, context: RunContext) -> dict[str, dict[str, Any]]:
        outcomes: dict[str, dict[str, Any]] = {}
        if not context.outcomes_dir.exists():
            return outcomes
        for path in sorted(context.outcomes_dir.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise AttemptRecordError(f"invalid outcome {path}: {exc}") from exc
            if value.get("run_id") != context.run_id or value.get("config_hash") != context.config_hash:
                raise ResumeHashMismatch(f"outcome {path} belongs to another run/config")
            outcomes[str(value["case_id"])] = value
        return outcomes


def _accepted(evaluation: Mapping[str, Any]) -> bool:
    acceptance = evaluation.get("acceptance", {})
    if isinstance(acceptance, Mapping):
        overall = acceptance.get("overall")
        return str(getattr(overall, "value", overall)).upper() == "ACCEPT"
    return False


def _normalise_dialogue(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AttemptRecordError("variant output dialogue must be a sequence")
    turns: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise AttemptRecordError(f"dialogue turn {index} must be a mapping")
        role = str(raw.get("role", "")).strip()
        text = str(raw.get("text", raw.get("content", "")))
        if not role:
            raise AttemptRecordError(f"dialogue turn {index} has no role")
        turns.append({"index": index, "role": role, "text": text})
    return tuple(turns)


def _summarize_cost(
    calls: Sequence[Mapping[str, Any]], supplied: Any = None
) -> dict[str, Any]:
    summary = {
        "calls": len(calls),
        "prompt_tokens": sum(
            int(call.get("prompt_tokens", call.get("input_tokens", 0)) or 0) for call in calls
        ),
        "completion_tokens": sum(
            int(call.get("completion_tokens", call.get("output_tokens", 0)) or 0)
            for call in calls
        ),
        "wall_ms": sum(int(call.get("latency_ms", 0) or 0) for call in calls),
        "estimated_cost": sum(float(call.get("estimated_cost", 0.0) or 0.0) for call in calls),
    }
    if supplied is not None:
        if not isinstance(supplied, Mapping):
            raise AttemptRecordError("variant output cost must be a mapping")
        summary.update(_jsonable(supplied))
    return summary


__all__ = [
    "ATTEMPT_RECORD_VERSION",
    "RUN_MANIFEST_VERSION",
    "AttemptLog",
    "AttemptRecord",
    "AttemptRecordError",
    "ExperimentRunError",
    "ExperimentRunResult",
    "ExperimentRunner",
    "ImmutableAttemptError",
    "ResumeConfigurationMismatch",
    "ResumeHashMismatch",
    "RunContext",
    "RunHashes",
    "RunManager",
    "VariantExecutionError",
    "derive_attempt_seed",
]
