"""Pure aggregation functions kept separate from generation orchestration."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def _model_call_tokens(call: Mapping[str, Any]) -> int:
    usage = call.get("usage", {})
    if not isinstance(usage, Mapping):
        return 0
    if usage.get("total_tokens") is not None:
        return int(usage["total_tokens"])
    return sum(
        int(usage.get(key, 0) or 0)
        for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens")
    )


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
        first_evaluation = ordered[0].get("evaluation", {})
        metrics = evaluation.get("metrics", {})
        boundary_details = metrics.get("knowledge_boundary", {}).get("details", {})
        ensemble_dimensions = (
            metrics.get("independent_ensemble", {}).get("details", {}).get("dimensions", {})
        )
        model_calls = [call for record in ordered for call in record.get("model_calls", [])]
        outcomes.append(
            {
                "profile_id": profile_id,
                "profile_type": profile_type,
                "success": bool(accepted),
                "is_realistic": bool(accepted),
                "first_attempt_success": bool(ordered[0].get("accepted")),
                "first_attempt_evaluation_status": first_evaluation.get("evaluation_status"),
                "attempts": len(ordered),
                "best_attempt": best.get("attempt"),
                "judge_score": evaluation.get("composite_score"),
                "naturalness_score": metrics.get("naturalness", {}).get("score"),
                "profile_compliance_score": metrics.get("knowledge_boundary", {}).get("score"),
                "claim_faithfulness_score": metrics.get("role_aware_clinical_faithfulness", {}).get(
                    "score"
                ),
                "structural_validity_score": metrics.get("structural_validity", {}).get("score"),
                "leakage_event_count": boundary_details.get("leakage_event_count"),
                "leakage_rate": boundary_details.get("leakage_rate"),
                "zero_leakage": boundary_details.get("leakage_event_count") == 0
                if boundary_details.get("leakage_event_count") is not None
                else None,
                "patient_factuality_score": ensemble_dimensions.get("patient_factuality"),
                "doctor_factuality_score": ensemble_dimensions.get("doctor_factuality"),
                "clinical_plausibility_score": ensemble_dimensions.get("clinical_plausibility"),
                "model_call_count": len(model_calls),
                "total_tokens": sum(_model_call_tokens(call) for call in model_calls),
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

    def _values(name: str) -> list[float]:
        return [float(record[name]) for record in records if record.get(name) is not None]

    dimension_scores = {
        name: _values(name)
        for name in (
            "patient_factuality_score",
            "doctor_factuality_score",
            "clinical_plausibility_score",
            "structural_validity_score",
            "leakage_rate",
        )
    }
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
        "first_attempt_successes": sum(bool(r.get("first_attempt_success")) for r in records),
        "first_attempt_success_rate": (
            sum(bool(r.get("first_attempt_success")) for r in records) / len(records)
            if records
            else None
        ),
        "failed_dialogues": sum(not bool(r.get("success")) for r in records),
        "realistic_dialogues": sum(bool(r.get("is_realistic")) for r in records),
        "non_realistic_dialogues": sum(
            bool(r.get("success")) and not bool(r.get("is_realistic")) for r in records
        ),
        "avg_judge_score": sum(judge_scores) / len(judge_scores) if judge_scores else None,
        "avg_processing_time": (
            sum(processing_times) / len(processing_times) if processing_times else None
        ),
        "avg_patient_factuality_score": (
            sum(dimension_scores["patient_factuality_score"])
            / len(dimension_scores["patient_factuality_score"])
            if dimension_scores["patient_factuality_score"]
            else None
        ),
        "avg_doctor_factuality_score": (
            sum(dimension_scores["doctor_factuality_score"])
            / len(dimension_scores["doctor_factuality_score"])
            if dimension_scores["doctor_factuality_score"]
            else None
        ),
        "avg_clinical_plausibility_score": (
            sum(dimension_scores["clinical_plausibility_score"])
            / len(dimension_scores["clinical_plausibility_score"])
            if dimension_scores["clinical_plausibility_score"]
            else None
        ),
        "avg_structural_validity_score": (
            sum(dimension_scores["structural_validity_score"])
            / len(dimension_scores["structural_validity_score"])
            if dimension_scores["structural_validity_score"]
            else None
        ),
        "avg_leakage_rate": (
            sum(dimension_scores["leakage_rate"]) / len(dimension_scores["leakage_rate"])
            if dimension_scores["leakage_rate"]
            else None
        ),
        "zero_leakage_rate": (
            sum(record.get("zero_leakage") is True for record in records)
            / sum(record.get("zero_leakage") is not None for record in records)
            if any(record.get("zero_leakage") is not None for record in records)
            else None
        ),
        "total_model_calls": sum(int(record.get("model_call_count", 0)) for record in records),
        "total_tokens": sum(int(record.get("total_tokens", 0)) for record in records),
        "by_profile_type": by_type,
    }
