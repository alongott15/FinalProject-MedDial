from __future__ import annotations

from datetime import datetime, timezone

import pytest

from meddial.analysis import (
    AdjustmentMethod,
    PowerError,
    adjust_pvalues,
    calculate_paired_power,
    paired_randomisation_test,
    regenerate_tables,
    write_power_record,
)


def test_paired_comparison_uses_case_pairing():
    result = paired_randomisation_test(
        {"a": 0.9, "b": 0.8, "c": None},
        {"a": 0.2, "b": 0.1, "c": 1.0},
    )
    assert result.mean_difference == pytest.approx(0.7)
    assert result.n_cases == 2
    assert result.n_dropped == 1


def test_multiplicity_adjustments_match_reference_values():
    raw = {"a": 0.01, "b": 0.04, "c": 0.03}
    assert adjust_pvalues(raw, AdjustmentMethod.HOLM) == pytest.approx(
        {"a": 0.03, "b": 0.06, "c": 0.06}
    )
    assert adjust_pvalues(raw, AdjustmentMethod.BENJAMINI_HOCHBERG) == pytest.approx(
        {"a": 0.03, "b": 0.04, "c": 0.04}
    )


def test_power_calculation_records_pilot_and_incomplete_allowance(tmp_path):
    pilot = [-0.08, -0.02, 0.01, 0.04, 0.07, 0.11, 0.14, 0.18]
    result = calculate_paired_power(
        pilot, smallest_difference=0.04, incomplete_rate=0.1
    )
    assert result.required_total_90 >= result.required_total_80
    assert result.required_total_80 > result.required_complete_80

    path = write_power_record(
        tmp_path / "power.md",
        result,
        planned_total=200,
        frozen_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    body = path.read_text()
    assert result.pilot_hash in body
    assert "INCOMPLETE" in body
    assert "Planned total: 200" in body


def test_power_record_requires_zoned_freeze_time(tmp_path):
    result = calculate_paired_power([0.1, 0.2], smallest_difference=0.05)
    with pytest.raises(PowerError, match="timezone"):
        write_power_record(
            tmp_path / "power.md",
            result,
            planned_total=200,
            frozen_at=datetime(2026, 9, 1),  # noqa: DTZ001 - exercising rejection
        )


def _record(case, policy, value, *, accepted=True, attempt=0):
    return {
        "run_id": "run_test",
        "dialogue_id": f"{case}_{policy}",
        "attempt_index": attempt,
        "inputs": {
            "case_id": case,
            "variant": "baseline",
            "policy": {"id": policy, "version": "2.0"},
        },
        "evaluation": {
            "scores": {
                "patient_factuality": {
                    "value": value,
                    "status": "pass" if value is not None else "incomplete",
                }
            },
            "acceptance": {"overall": "ACCEPT" if accepted else "REJECT"},
        },
        "cost": {
            "calls": 2,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "wall_ms": 20,
        },
    }


def test_tables_regenerate_deterministically(tmp_path):
    records = [
        _record("c1", "FULL", 0.8),
        _record("c2", "FULL", 0.9),
        _record("c1", "NO_DIAGNOSIS", None, accepted=False),
        _record("c2", "NO_DIAGNOSIS", 0.7),
    ]
    first = regenerate_tables(records, output_root=tmp_path / "one", resamples=50)
    second = regenerate_tables(
        list(reversed(records)), output_root=tmp_path / "two", resamples=50
    )
    assert set(first.files) == set(second.files)
    for name in first.files:
        assert first.files[name].read_bytes() == second.files[name].read_bytes()
    assert "INCOMPLETE" not in first.files["summary.json"].read_text()
    assert "n_incomplete" in first.files["scores.csv"].read_text()


def test_tables_use_last_attempt_for_quality_but_all_attempts_for_cost(tmp_path):
    failed = _record("c1", "FULL", 0.2, accepted=False, attempt=0)
    passed = _record("c1", "FULL", 0.9, accepted=True, attempt=1)
    outputs = regenerate_tables(
        [failed, passed], output_root=tmp_path, resamples=10
    )
    summary = outputs.files["summary.json"].read_text()
    cost = outputs.files["cost.csv"].read_text()
    assert '"estimate": 0.9' in summary
    assert "baseline,2,4,20,10,40" in cost
