"""Pure aggregation functions kept separate from generation orchestration."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def aggregate_attempt_records(
    attempts: Iterable[Mapping[str, Any]], profile_types: Sequence[str]
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        grouped[(str(attempt["profile_id"]), str(attempt["profile_type"]))].append(attempt)

    outcomes: list[dict[str, Any]] = []
    for (profile_id, profile_type), records in sorted(grouped.items()):
        ordered = sorted(records, key=lambda record: int(record.get("attempt", 0)))
        accepted = [record for record in ordered if record.get("accepted")]
        scored = [
            record
            for record in ordered
            if record.get("evaluation", {}).get("composite_score") is not None
        ]
        best = (
            accepted[0]
            if accepted
            else max(
                scored,
                key=lambda record: float(record["evaluation"]["composite_score"]),
                default=ordered[-1],
            )
        )
        evaluation = best.get("evaluation", {})
        metrics = evaluation.get("metrics", {})
        outcomes.append(
            {
                "profile_id": profile_id,
                "profile_type": profile_type,
                "success": bool(accepted),
                "is_realistic": bool(accepted),
                "attempts": len(ordered),
                "best_attempt": best.get("attempt"),
                "judge_score": evaluation.get("composite_score"),
                "naturalness_score": metrics.get("naturalness", {}).get("score"),
                "profile_compliance_score": metrics.get("knowledge_boundary", {}).get("score"),
                "claim_faithfulness_score": metrics.get(
                    "role_aware_clinical_faithfulness", {}
                ).get("score"),
                "processing_time": sum(float(r.get("duration_seconds", 0.0)) for r in ordered),
                "evaluation_status": evaluation.get("evaluation_status"),
            }
        )
    return outcomes


def build_global_stats(
    per_profile_stats: Sequence[Mapping[str, Any]],
    total_profiles: int,
    profile_types: Sequence[str],
) -> dict[str, Any]:
    unique: dict[tuple[str, str], Mapping[str, Any]] = {}
    for record in per_profile_stats:
        key = (str(record.get("profile_id")), str(record.get("profile_type")))
        unique[key] = record
    records = list(unique.values())
    judge_scores = [float(r["judge_score"]) for r in records if r.get("judge_score") is not None]
    processing_times = [
        float(r["processing_time"]) for r in records if r.get("processing_time") is not None
    ]
    by_type: dict[str, dict[str, Any]] = {
        profile_type: {
            "success": 0,
            "fail": 0,
            "realistic": 0,
            "non_realistic": 0,
            "judge_scores": [],
        }
        for profile_type in profile_types
    }
    for record in records:
        profile_type = str(record.get("profile_type", "UNKNOWN"))
        bucket = by_type.setdefault(
            profile_type,
            {"success": 0, "fail": 0, "realistic": 0, "non_realistic": 0, "judge_scores": []},
        )
        if record.get("success"):
            bucket["success"] += 1
            bucket["realistic"] += int(bool(record.get("is_realistic")))
            bucket["non_realistic"] += int(not bool(record.get("is_realistic")))
            if record.get("judge_score") is not None:
                bucket["judge_scores"].append(float(record["judge_score"]))
        else:
            bucket["fail"] += 1
    for bucket in by_type.values():
        scores = bucket.pop("judge_scores")
        bucket["avg_judge_score"] = sum(scores) / len(scores) if scores else None
    return {
        "total_profiles": total_profiles,
        "expected_records": total_profiles * len(profile_types),
        "completed_records": len(records),
        "successful_dialogues": sum(bool(r.get("success")) for r in records),
        "failed_dialogues": sum(not bool(r.get("success")) for r in records),
        "realistic_dialogues": sum(bool(r.get("is_realistic")) for r in records),
        "non_realistic_dialogues": sum(
            bool(r.get("success")) and not bool(r.get("is_realistic")) for r in records
        ),
        "avg_judge_score": sum(judge_scores) / len(judge_scores) if judge_scores else None,
        "avg_processing_time": (
            sum(processing_times) / len(processing_times) if processing_times else None
        ),
        "by_profile_type": by_type,
    }
