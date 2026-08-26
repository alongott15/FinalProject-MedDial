"""Private, auditable two-reviewer cohort validation workflow."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class ClinicalReviewDecision(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"


class ClinicalReviewStatus(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    NEEDS_ADJUDICATION = "needs_adjudication"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class ClinicalReview:
    subject_id: Any
    hadm_id: Any
    row_id: Any
    reviewer_id: str
    decision: ClinicalReviewDecision
    index_complaint: str
    acuity_label: str
    exclusion_reasons: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""
    guideline_version: str = "clinical-review-v1"
    reviewed_at: str = ""
    adjudication: bool = False

    @property
    def source_key(self) -> tuple[Any, Any, Any]:
        return self.subject_id, self.hadm_id, self.row_id

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["decision"] = self.decision.value
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ClinicalReview:
        return cls(
            subject_id=value.get("subject_id"),
            hadm_id=value.get("hadm_id"),
            row_id=value.get("row_id"),
            reviewer_id=str(value["reviewer_id"]),
            decision=ClinicalReviewDecision(str(value["decision"])),
            index_complaint=str(value.get("index_complaint", "")),
            acuity_label=str(value.get("acuity_label", "")),
            exclusion_reasons=tuple(str(item) for item in value.get("exclusion_reasons", [])),
            notes=str(value.get("notes", "")),
            guideline_version=str(value.get("guideline_version", "clinical-review-v1")),
            reviewed_at=str(value.get("reviewed_at", "")),
            adjudication=bool(value.get("adjudication", False)),
        )


@dataclass(frozen=True)
class ClinicalReviewOutcome:
    source_key: tuple[Any, Any, Any]
    status: ClinicalReviewStatus
    reviewer_count: int
    reason: str


def _source_key(note: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return note.get("subject_id"), note.get("hadm_id"), note.get("row_id")


def create_review_template(notes: Sequence[Mapping[str, Any]], path: str | Path) -> Path:
    """Write an ignored/private template; it contains restricted source identifiers."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "clinical-review-v1",
        "data_classification": "restricted_clinical",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "instructions": (
            "Two independent clinicians review each record. Add separate review objects and "
            "an adjudication object when their decisions disagree."
        ),
        "candidates": [
            {
                "subject_id": note.get("subject_id"),
                "hadm_id": note.get("hadm_id"),
                "row_id": note.get("row_id"),
                "reviews": [],
            }
            for note in notes
        ],
    }
    with target.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    return target


def load_reviews(path: str | Path) -> list[ClinicalReview]:
    with Path(path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    reviews: list[ClinicalReview] = []
    for candidate in payload.get("candidates", []):
        for value in candidate.get("reviews", []):
            merged = {
                "subject_id": candidate.get("subject_id"),
                "hadm_id": candidate.get("hadm_id"),
                "row_id": candidate.get("row_id"),
                **value,
            }
            reviews.append(ClinicalReview.from_dict(merged))
    return reviews


def resolve_reviews(
    notes: Sequence[Mapping[str, Any]],
    reviews: Sequence[ClinicalReview],
    required_independent_reviewers: int = 2,
) -> tuple[list[dict[str, Any]], list[ClinicalReviewOutcome]]:
    grouped: dict[tuple[Any, Any, Any], list[ClinicalReview]] = defaultdict(list)
    for review in reviews:
        grouped[review.source_key].append(review)
    eligible: list[dict[str, Any]] = []
    outcomes: list[ClinicalReviewOutcome] = []
    for note in notes:
        key = _source_key(note)
        case_reviews = grouped.get(key, [])
        adjudications = [review for review in case_reviews if review.adjudication]
        independent = [review for review in case_reviews if not review.adjudication]
        reviewer_ids = {review.reviewer_id for review in independent}
        if len(reviewer_ids) < required_independent_reviewers:
            status = ClinicalReviewStatus.INCOMPLETE
            reason = f"Only {len(reviewer_ids)} independent reviewers"
        elif adjudications:
            final = adjudications[-1].decision
            status = (
                ClinicalReviewStatus.ELIGIBLE
                if final is ClinicalReviewDecision.ELIGIBLE
                else ClinicalReviewStatus.INELIGIBLE
            )
            reason = "Resolved by recorded adjudication"
        else:
            decisions = {review.decision for review in independent}
            if len(decisions) > 1:
                status = ClinicalReviewStatus.NEEDS_ADJUDICATION
                reason = "Independent reviewers disagreed"
            else:
                final = next(iter(decisions))
                status = (
                    ClinicalReviewStatus.ELIGIBLE
                    if final is ClinicalReviewDecision.ELIGIBLE
                    else ClinicalReviewStatus.INELIGIBLE
                )
                reason = "Independent reviewers agreed"
        outcomes.append(ClinicalReviewOutcome(key, status, len(reviewer_ids), reason))
        if status is ClinicalReviewStatus.ELIGIBLE:
            selected = dict(note)
            selected["clinical_review_status"] = status.value
            eligible.append(selected)
    return eligible, outcomes


def review_agreement(reviews: Sequence[ClinicalReview]) -> dict[str, float | int | None]:
    """Calculate observed agreement and Cohen's kappa for two-reviewer cases."""

    grouped: dict[tuple[Any, Any, Any], list[ClinicalReview]] = defaultdict(list)
    for review in reviews:
        if not review.adjudication:
            grouped[review.source_key].append(review)
    paired: list[tuple[ClinicalReviewDecision, ClinicalReviewDecision]] = []
    for case_reviews in grouped.values():
        by_reviewer = {review.reviewer_id: review for review in case_reviews}
        if len(by_reviewer) == 2:
            ordered = [by_reviewer[key] for key in sorted(by_reviewer)]
            paired.append((ordered[0].decision, ordered[1].decision))
    if not paired:
        return {"paired_case_count": 0, "observed_agreement": None, "cohens_kappa": None}
    observed = sum(first is second for first, second in paired) / len(paired)
    first_eligible = sum(first is ClinicalReviewDecision.ELIGIBLE for first, _ in paired) / len(
        paired
    )
    second_eligible = sum(second is ClinicalReviewDecision.ELIGIBLE for _, second in paired) / len(
        paired
    )
    expected = first_eligible * second_eligible + (1.0 - first_eligible) * (1.0 - second_eligible)
    kappa = (observed - expected) / (1.0 - expected) if expected < 1.0 else None
    return {
        "paired_case_count": len(paired),
        "observed_agreement": observed,
        "cohens_kappa": kappa,
    }
