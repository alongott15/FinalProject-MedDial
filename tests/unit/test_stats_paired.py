"""Appendix E.1-E.3: case-clustered bootstrap, paired differences, Wilson.

The property that matters most here is defect D-11: resampling dialogues
instead of cases understates every interval, because a case's three policy
arms are correlated. ``test_clustering_widens_the_interval`` is the test that
would fail if someone "simplified" the bootstrap back to the defect.

No fixture here is derived from any real record.
"""

from __future__ import annotations

import pytest

from meddial.stats import (
    StatsError,
    case_clustered_bootstrap,
    mean,
    paired_difference,
    wilson_interval,
)

# Ten cases, each observed three times. Within a case the three observations
# are identical — the extreme of the correlation the clustering exists for.
CASE_MEANS = {f"c{i}": 0.5 + 0.04 * i for i in range(10)}
CLUSTERED = {case: [value] * 3 for case, value in CASE_MEANS.items()}
FLATTENED = {f"{case}-{i}": [value] for case, value in CASE_MEANS.items() for i in range(3)}


def test_clustering_widens_the_interval():
    """D-11: three correlated observations are not three independent ones."""
    clustered = case_clustered_bootstrap(CLUSTERED, resamples=500, seed=7)
    as_if_independent = case_clustered_bootstrap(FLATTENED, resamples=500, seed=7)

    assert clustered.estimate == pytest.approx(as_if_independent.estimate)
    clustered_width = clustered.high - clustered.low
    naive_width = as_if_independent.high - as_if_independent.low
    assert clustered_width > naive_width * 1.4


def test_a_case_drawn_twice_contributes_its_whole_cluster():
    """Otherwise a resample would silently break the cluster apart."""
    lopsided = {"a": [0.0, 0.0, 0.0, 0.0], "b": [1.0]}
    interval = case_clustered_bootstrap(lopsided, resamples=400, seed=3)

    # Pooled mean is 0.2 over all five observations; it would be 0.5 if the
    # bootstrap took one observation per case.
    assert interval.estimate == pytest.approx(0.2)


def test_bootstrap_is_reproducible_from_the_seed():
    first = case_clustered_bootstrap(CLUSTERED, resamples=300, seed=11)
    second = case_clustered_bootstrap(CLUSTERED, resamples=300, seed=11)
    third = case_clustered_bootstrap(CLUSTERED, resamples=300, seed=12)

    assert (first.low, first.high) == (second.low, second.high)
    assert (first.low, first.high) != (third.low, third.high)


def test_incomplete_observations_are_dropped_not_zeroed():
    """EVAL-5: an unmeasured dimension must not enter an aggregate as 0."""
    with_gaps = {"a": [0.8, None], "b": [0.8, None, 0.8]}
    interval = case_clustered_bootstrap(with_gaps, resamples=200, seed=1)

    assert interval.estimate == pytest.approx(0.8)


def test_a_case_with_no_measured_observation_drops_out():
    partial = {"a": [0.6], "b": [None, None]}
    interval = case_clustered_bootstrap(partial, resamples=200, seed=1)

    assert interval.estimate == pytest.approx(0.6)


def test_an_empty_sample_raises_rather_than_returning_zero():
    with pytest.raises(StatsError):
        case_clustered_bootstrap({"a": [None]}, resamples=100)
    with pytest.raises(StatsError):
        mean([])


def test_paired_difference_uses_only_cases_measured_in_both_arms():
    arm_a = {"a": 0.9, "b": 0.8, "c": None, "d": 0.7}
    arm_b = {"a": 0.6, "b": 0.5, "c": 0.4}

    result = paired_difference(arm_a, arm_b, label_a="policy_context", label_b="full_reference")

    assert result.n_cases == 2
    # c is INCOMPLETE in one arm; d appears in only one arm.
    assert result.n_dropped == 2
    assert result.difference.estimate == pytest.approx(0.3)
    assert result.arm_a == "policy_context"


def test_a_constant_within_case_offset_excludes_zero():
    arm_a = {f"c{i}": 0.5 + 0.01 * i for i in range(12)}
    arm_b = {case: value - 0.2 for case, value in arm_a.items()}

    result = paired_difference(arm_a, arm_b, resamples=500, seed=5)

    assert result.excludes_zero
    assert result.difference.low > 0.0


def test_arms_with_no_shared_case_raise():
    with pytest.raises(StatsError):
        paired_difference({"a": 0.5}, {"b": 0.5})


def test_wilson_stays_inside_the_unit_interval_at_the_ceiling():
    """E.3: the normal approximation puts the upper bound above 1 here."""
    interval = wilson_interval(100, 100)

    assert interval.estimate == pytest.approx(1.0)
    assert interval.high <= 1.0
    assert interval.low < 1.0


def test_wilson_refuses_an_impossible_proportion():
    with pytest.raises(StatsError):
        wilson_interval(0, 0)
    with pytest.raises(StatsError):
        wilson_interval(5, 3)


def test_an_untabulated_confidence_is_refused_not_approximated():
    with pytest.raises(StatsError):
        wilson_interval(50, 100, confidence=0.975)


def test_intervals_serialise_with_their_method():
    """E.6: a point estimate never travels without the interval that made it."""
    record = wilson_interval(80, 100).as_record()

    assert set(record) == {"estimate", "low", "high", "method", "confidence"}
    assert record["method"] == "Wilson score"
    assert record["confidence"] == 0.95
