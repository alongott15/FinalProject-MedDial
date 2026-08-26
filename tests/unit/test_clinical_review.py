from meddial.clinical_review import (
    ClinicalReview,
    ClinicalReviewDecision,
    ClinicalReviewStatus,
    resolve_reviews,
    review_agreement,
)


def review(reviewer: str, decision: str, *, adjudication: bool = False):
    return ClinicalReview(
        subject_id=1,
        hadm_id=2,
        row_id=3,
        reviewer_id=reviewer,
        decision=ClinicalReviewDecision(decision),
        index_complaint="sore throat",
        acuity_label="lower-acuity candidate",
        adjudication=adjudication,
    )


def test_two_independent_clinicians_are_required():
    notes = [{"subject_id": 1, "hadm_id": 2, "row_id": 3}]
    eligible, outcomes = resolve_reviews(notes, [review("one", "eligible")])
    assert eligible == []
    assert outcomes[0].status is ClinicalReviewStatus.INCOMPLETE


def test_agreement_selects_case_and_disagreement_requires_adjudication():
    notes = [{"subject_id": 1, "hadm_id": 2, "row_id": 3}]
    eligible, outcomes = resolve_reviews(
        notes, [review("one", "eligible"), review("two", "eligible")]
    )
    assert eligible[0]["clinical_review_status"] == "eligible"
    assert outcomes[0].status is ClinicalReviewStatus.ELIGIBLE

    eligible, outcomes = resolve_reviews(
        notes, [review("one", "eligible"), review("two", "ineligible")]
    )
    assert eligible == []
    assert outcomes[0].status is ClinicalReviewStatus.NEEDS_ADJUDICATION


def test_recorded_adjudication_is_final():
    notes = [{"subject_id": 1, "hadm_id": 2, "row_id": 3}]
    eligible, outcomes = resolve_reviews(
        notes,
        [
            review("one", "eligible"),
            review("two", "ineligible"),
            review("adjudicator", "eligible", adjudication=True),
        ],
    )
    assert eligible
    assert outcomes[0].reason == "Resolved by recorded adjudication"


def test_review_agreement_reports_kappa_for_two_reviewers():
    reviews = [
        review("one", "eligible"),
        review("two", "eligible"),
        ClinicalReview(
            subject_id=4,
            hadm_id=5,
            row_id=6,
            reviewer_id="one",
            decision=ClinicalReviewDecision.INELIGIBLE,
            index_complaint="",
            acuity_label="",
        ),
        ClinicalReview(
            subject_id=4,
            hadm_id=5,
            row_id=6,
            reviewer_id="two",
            decision=ClinicalReviewDecision.INELIGIBLE,
            index_complaint="",
            acuity_label="",
        ),
    ]
    agreement = review_agreement(reviews)
    assert agreement["paired_case_count"] == 2
    assert agreement["observed_agreement"] == 1.0
    assert agreement["cohens_kappa"] == 1.0
