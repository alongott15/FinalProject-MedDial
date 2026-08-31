"""Pure aggregation over immutable attempt records (EXP-4, EXP-7).

This module imports no provider and accepts no generation callback.  It reads
already-materialized records, selects one final attempt per case/condition,
and summarizes quality and cost.  ``INCOMPLETE`` values are counted and
excluded from means rather than silently converted to zero.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .runner import AttemptRecord, AttemptRecordError


def _mapping(record: AttemptRecord | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(record, AttemptRecord):
        return record.as_record()
    return dict(record)


def _condition_key(record: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    inputs = record.get("inputs", {})
    policy = inputs.get("policy", {}) if isinstance(inputs, Mapping) else {}
    policy_id = str(policy.get("id", "unknown")) if isinstance(policy, Mapping) else "unknown"
    policy_version = (
        str(policy.get("version", "active")) if isinstance(policy, Mapping) else "active"
    )
    return (
        str(record.get("run_id", "unknown")),
        str(record.get("case_id", "unknown")),
        policy_id,
        policy_version,
        str(record.get("variant", "unknown")),
    )


def _accepted(record: Mapping[str, Any]) -> bool:
    evaluation = record.get("evaluation", {})
    acceptance = evaluation.get("acceptance", {}) if isinstance(evaluation, Mapping) else {}
    overall = acceptance.get("overall") if isinstance(acceptance, Mapping) else None
    return str(getattr(overall, "value", overall)).upper() == "ACCEPT"


def _final_attempts(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    grouped: dict[tuple[str, str, str, str, str], list[Mapping[str, Any]]] = {}
    for record in records:
        grouped.setdefault(_condition_key(record), []).append(record)

    final: list[Mapping[str, Any]] = []
    for key in sorted(grouped):
        ordered = sorted(grouped[key], key=lambda item: int(item.get("attempt_index", 0)))
        accepted = next((item for item in ordered if _accepted(item)), None)
        final.append(accepted or ordered[-1])
    return final


def _dimension_summary(final: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    cells: dict[str, dict[str, Any]] = {}
    for record in final:
        evaluation = record.get("evaluation", {})
        scores = evaluation.get("scores", {}) if isinstance(evaluation, Mapping) else {}
        if not isinstance(scores, Mapping):
            continue
        for dimension, score in scores.items():
            if not isinstance(score, Mapping):
                continue
            cell = cells.setdefault(
                str(dimension),
                {"values": [], "pass": 0, "fail": 0, "incomplete": 0},
            )
            status = str(getattr(score.get("status"), "value", score.get("status"))).lower()
            value = score.get("value")
            if status == "incomplete" or value is None:
                cell["incomplete"] += 1
                continue
            if status in {"pass", "fail"}:
                cell[status] += 1
            cell["values"].append(float(value))

    result: dict[str, dict[str, Any]] = {}
    for dimension, cell in sorted(cells.items()):
        values = cell.pop("values")
        result[dimension] = {
            "measured": len(values),
            "incomplete": cell["incomplete"],
            "pass": cell["pass"],
            "fail": cell["fail"],
            "mean": sum(values) / len(values) if values else None,
        }
    return result


def _cost_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    keys = ("calls", "prompt_tokens", "completion_tokens", "wall_ms")
    summary: dict[str, Any] = {
        key: sum(
            int(record.get("cost", {}).get(key, 0) or 0)
            for record in records
            if isinstance(record.get("cost", {}), Mapping)
        )
        for key in keys
    }
    summary["estimated_cost"] = sum(
        float(record.get("cost", {}).get("estimated_cost", 0.0) or 0.0)
        for record in records
        if isinstance(record.get("cost", {}), Mapping)
    )
    return summary


def _summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    final = _final_attempts(records) if records else []
    accepted = sum(_accepted(record) for record in final)
    cost = _cost_summary(records)
    return {
        "attempts": len(records),
        "cases": len(final),
        "accepted_cases": accepted,
        "terminal_failures": len(final) - accepted,
        "acceptance_rate": accepted / len(final) if final else None,
        "attempts_per_case": len(records) / len(final) if final else None,
        "dimensions": _dimension_summary(final),
        "cost": cost,
        "cost_per_case": (
            cost["estimated_cost"] / len(final) if final else None
        ),
    }


def aggregate_attempts(
    attempts: Iterable[AttemptRecord | Mapping[str, Any]],
) -> dict[str, Any]:
    """Deterministically summarize records with no model/provider access."""
    records = [_mapping(record) for record in attempts]
    records.sort(
        key=lambda item: (
            _condition_key(item),
            int(item.get("attempt_index", 0)),
        )
    )
    report = _summary(records)

    by_variant: dict[str, dict[str, Any]] = {}
    variants = sorted({str(record.get("variant", "unknown")) for record in records})
    for variant in variants:
        by_variant[variant] = _summary(
            [record for record in records if str(record.get("variant", "unknown")) == variant]
        )

    by_policy: dict[str, dict[str, Any]] = {}
    policies = sorted(
        {
            f"{key[2]}@{key[3]}"
            for record in records
            if (key := _condition_key(record))
        }
    )
    for policy in policies:
        by_policy[policy] = _summary(
            [
                record
                for record in records
                if f"{_condition_key(record)[2]}@{_condition_key(record)[3]}" == policy
            ]
        )

    return {**report, "by_variant": by_variant, "by_policy": by_policy}


# The name used in the PRD and by the earlier design-reference branch.
aggregate_attempt_records = aggregate_attempts


def read_attempts(path: str | Path) -> list[AttemptRecord]:
    """Read and content-hash-verify an attempts JSONL file."""
    source = Path(path)
    records: list[AttemptRecord] = []
    with source.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise AttemptRecordError(f"{source}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(value, Mapping):
                raise AttemptRecordError(f"{source}:{line_no}: expected a JSON object")
            records.append(AttemptRecord.from_record(value))
    return records


def aggregate_run(run_dir: str | Path) -> dict[str, Any]:
    """Separate aggregation entry point over ``<run>/attempts/attempts.jsonl``."""
    path = Path(run_dir) / "attempts" / "attempts.jsonl"
    return aggregate_attempts(read_attempts(path))


__all__ = [
    "aggregate_attempt_records",
    "aggregate_attempts",
    "aggregate_run",
    "read_attempts",
]
