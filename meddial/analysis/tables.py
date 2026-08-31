"""Deterministically regenerate standard study tables and figures (STAT-4)."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meddial.stats import case_clustered_bootstrap, wilson_interval


class AnalysisError(ValueError):
    """Attempt records are incomplete or mix incompatible runs."""


@dataclass(frozen=True)
class AnalysisOutputs:
    run_id: str
    output_dir: Path
    files: Mapping[str, Path]


def read_attempt_records(paths: Iterable[Path | str]) -> list[dict[str, Any]]:
    """Read immutable attempt JSON or JSONL files in deterministic path order."""

    records: list[dict[str, Any]] = []
    for path in sorted((Path(path) for path in paths), key=lambda item: str(item)):
        text = path.read_text().strip()
        if not text:
            continue
        if text.startswith("["):
            payload = json.loads(text)
            if not isinstance(payload, list):
                raise AnalysisError(f"{path} does not contain a JSON record list")
            records.extend(_require_record(item, path) for item in payload)
        else:
            for line_number, line in enumerate(text.splitlines(), start=1):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AnalysisError(f"{path}:{line_number}: {exc}") from exc
                records.append(_require_record(item, path))
    return records


def regenerate_tables(
    records: Sequence[Mapping[str, Any]],
    *,
    output_root: Path | str,
    run_id: str | None = None,
    resamples: int = 2000,
    seed: int = 0,
    primary_metric: str = "patient_factuality",
) -> AnalysisOutputs:
    """Write all standard machine-readable tables and the primary SVG figure."""

    if not records:
        raise AnalysisError("no attempt records were supplied")
    run_ids = {str(record.get("run_id", "")) for record in records}
    if "" in run_ids:
        raise AnalysisError("every attempt record must carry run_id")
    selected_run_id = run_id or (next(iter(run_ids)) if len(run_ids) == 1 else None)
    if selected_run_id is None or run_ids != {selected_run_id}:
        raise AnalysisError(
            f"records belong to {sorted(run_ids)}; analysis requires exactly one run"
        )
    if resamples < 1:
        raise AnalysisError("resamples must be positive")

    final_records = _final_attempts(records)
    score_rows = _score_rows(final_records, resamples=resamples, seed=seed)
    acceptance_rows = _acceptance_rows(final_records)
    cost_rows = _cost_rows(records)
    incomplete_rows = _incomplete_rows(final_records)
    summary = {
        "run_id": selected_run_id,
        "attempt_records": len(records),
        "final_dialogues": len(final_records),
        "resamples": resamples,
        "bootstrap_seed": seed,
        "primary_metric": primary_metric,
        "scores": score_rows,
        "acceptance": acceptance_rows,
        "incomplete": incomplete_rows,
        "cost": cost_rows,
    }

    output_dir = Path(output_root) / selected_run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    content: dict[str, str] = {
        "summary.json": json.dumps(summary, sort_keys=True, indent=2) + "\n",
        "scores.csv": _to_csv(
            score_rows,
            [
                "variant",
                "policy",
                "metric",
                "estimate",
                "low",
                "high",
                "confidence",
                "method",
                "n_cases",
                "n_scores",
                "n_incomplete",
            ],
        ),
        "acceptance.csv": _to_csv(
            acceptance_rows,
            [
                "variant",
                "policy",
                "accepted",
                "total",
                "rate",
                "low",
                "high",
                "method",
            ],
        ),
        "incomplete.csv": _to_csv(
            incomplete_rows, ["variant", "policy", "metric", "count", "total", "rate"]
        ),
        "cost.csv": _to_csv(
            cost_rows,
            [
                "variant",
                "attempts",
                "calls",
                "prompt_tokens",
                "completion_tokens",
                "wall_ms",
            ],
        ),
        "primary_metric.svg": _primary_svg(score_rows, primary_metric),
    }
    files: dict[str, Path] = {}
    for name, body in content.items():
        path = output_dir / name
        path.write_text(body)
        files[name] = path

    manifest = {
        "run_id": selected_run_id,
        "files": {
            name: hashlib.sha256(content[name].encode("utf-8")).hexdigest()
            for name in sorted(content)
        },
    }
    manifest_path = output_dir / "analysis_manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    files[manifest_path.name] = manifest_path
    return AnalysisOutputs(run_id=selected_run_id, output_dir=output_dir, files=files)


def _final_attempts(
    records: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    by_dialogue: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        dialogue_id = str(record.get("dialogue_id", ""))
        if not dialogue_id:
            raise AnalysisError("every attempt record must carry dialogue_id")
        by_dialogue[dialogue_id].append(record)
    final = []
    for dialogue_id in sorted(by_dialogue):
        attempts = by_dialogue[dialogue_id]
        indices = [int(record.get("attempt_index", -1)) for record in attempts]
        if len(indices) != len(set(indices)):
            raise AnalysisError(f"dialogue {dialogue_id!r} repeats an attempt index")
        final.append(max(attempts, key=lambda record: int(record.get("attempt_index", -1))))
    return final


def _score_rows(
    records: Sequence[Mapping[str, Any]], *, resamples: int, seed: int
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, list[float | None]]] = defaultdict(
        lambda: defaultdict(list)
    )
    totals: dict[tuple[str, str, str], int] = defaultdict(int)
    incomplete: dict[tuple[str, str, str], int] = defaultdict(int)
    for record in records:
        variant, policy, case_id = _coordinates(record)
        scores = _evaluation(record).get("scores", {})
        if not isinstance(scores, Mapping):
            raise AnalysisError("evaluation.scores must be an object")
        for metric, raw_score in sorted(scores.items()):
            if not isinstance(raw_score, Mapping):
                raise AnalysisError(f"score {metric!r} must be an object")
            key = (variant, policy, str(metric))
            totals[key] += 1
            status = str(raw_score.get("status", "")).lower()
            value = raw_score.get("value")
            measured = None if status == "incomplete" or value is None else float(value)
            if measured is None:
                incomplete[key] += 1
            grouped[key][case_id].append(measured)

    rows = []
    for offset, key in enumerate(sorted(grouped)):
        variant, policy, metric = key
        measured_count = totals[key] - incomplete[key]
        base = {
            "variant": variant,
            "policy": policy,
            "metric": metric,
            "n_cases": sum(any(value is not None for value in values) for values in grouped[key].values()),
            "n_scores": measured_count,
            "n_incomplete": incomplete[key],
        }
        if measured_count:
            interval = case_clustered_bootstrap(
                grouped[key], resamples=resamples, seed=seed + offset
            )
            base.update(interval.as_record())
        else:
            base.update(
                {
                    "estimate": None,
                    "low": None,
                    "high": None,
                    "method": "all scores INCOMPLETE",
                    "confidence": 0.95,
                }
            )
        rows.append(base)
    return rows


def _acceptance_rows(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for record in records:
        variant, policy, _ = _coordinates(record)
        acceptance = _evaluation(record).get("acceptance", {})
        overall = acceptance.get("overall", "") if isinstance(acceptance, Mapping) else ""
        grouped[(variant, policy)].append(str(overall).upper() in {"ACCEPT", "PASS"})
    rows = []
    for (variant, policy), values in sorted(grouped.items()):
        accepted = sum(values)
        interval = wilson_interval(accepted, len(values))
        rows.append(
            {
                "variant": variant,
                "policy": policy,
                "accepted": accepted,
                "total": len(values),
                "rate": interval.estimate,
                "low": interval.low,
                "high": interval.high,
                "method": interval.method,
            }
        )
    return rows


def _incomplete_rows(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[bool]] = defaultdict(list)
    for record in records:
        variant, policy, _ = _coordinates(record)
        scores = _evaluation(record).get("scores", {})
        for metric, raw_score in sorted(scores.items()):
            status = str(raw_score.get("status", "")).lower()
            grouped[(variant, policy, str(metric))].append(
                status == "incomplete" or raw_score.get("value") is None
            )
    return [
        {
            "variant": variant,
            "policy": policy,
            "metric": metric,
            "count": sum(values),
            "total": len(values),
            "rate": sum(values) / len(values),
        }
        for (variant, policy, metric), values in sorted(grouped.items())
    ]


def _cost_rows(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "attempts": 0,
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "wall_ms": 0,
        }
    )
    for record in records:
        variant, _, _ = _coordinates(record)
        target = grouped[variant]
        target["attempts"] += 1
        cost = record.get("cost", {})
        if not isinstance(cost, Mapping):
            raise AnalysisError("cost must be an object")
        for key in ("calls", "prompt_tokens", "completion_tokens", "wall_ms"):
            target[key] += int(cost.get(key, 0))
    return [{"variant": variant, **grouped[variant]} for variant in sorted(grouped)]


def _coordinates(record: Mapping[str, Any]) -> tuple[str, str, str]:
    inputs = record.get("inputs", {})
    if not isinstance(inputs, Mapping):
        raise AnalysisError("inputs must be an object")
    variant = str(inputs.get("variant") or record.get("variant") or "")
    policy_raw = inputs.get("policy", inputs.get("patient_policy_id", ""))
    if isinstance(policy_raw, Mapping):
        policy = "@".join(
            part
            for part in (str(policy_raw.get("id", "")), str(policy_raw.get("version", "")))
            if part
        )
    else:
        policy = str(policy_raw)
    case_id = str(inputs.get("case_id") or record.get("case_id") or "")
    if not variant or not policy or not case_id:
        raise AnalysisError("every attempt requires variant, policy, and case_id")
    return variant, policy, case_id


def _evaluation(record: Mapping[str, Any]) -> Mapping[str, Any]:
    evaluation = record.get("evaluation", {})
    if not isinstance(evaluation, Mapping):
        raise AnalysisError("evaluation must be an object")
    return evaluation


def _to_csv(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column) for column in columns})
    return output.getvalue()


def _primary_svg(rows: Sequence[Mapping[str, Any]], metric: str) -> str:
    selected = [row for row in rows if row["metric"] == metric and row["estimate"] is not None]
    selected.sort(key=lambda row: (row["variant"], row["policy"]))
    width = max(480, 120 * len(selected) + 100)
    height = 340
    plot_height = 240
    bars = []
    for index, row in enumerate(selected):
        value = max(0.0, min(1.0, float(row["estimate"])))
        bar_height = value * plot_height
        x = 70 + index * 120
        y = 270 - bar_height
        label = html.escape(f"{row['variant']} / {row['policy']}")
        bars.extend(
            [
                f'<rect x="{x}" y="{y:.3f}" width="70" height="{bar_height:.3f}" fill="#3465a4"/>',
                f'<text x="{x + 35}" y="{y - 6:.3f}" text-anchor="middle" font-size="12">{value:.3f}</text>',
                f'<text x="{x + 35}" y="290" text-anchor="middle" font-size="10">{label}</text>',
            ]
        )
    body = "\n  ".join(bars)
    title = html.escape(metric)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        f'  <title>{title} by condition</title>\n'
        '  <line x1="60" y1="30" x2="60" y2="270" stroke="black"/>\n'
        '  <line x1="60" y1="270" x2="95%" y2="270" stroke="black"/>\n'
        f'  {body}\n'
        '</svg>\n'
    )


def _require_record(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisError(f"{path} contains a non-object attempt record")
    return value


__all__ = [
    "AnalysisError",
    "AnalysisOutputs",
    "read_attempt_records",
    "regenerate_tables",
]
