"""SCR creation is gated by the structured cohort, not by reading note text.

The pipeline used to pick cases with ``is_light_common_case``, a keyword scan
over the note body. ``configs/cohort/criteria_v1.sql`` forbids exactly that --
"No clinical eligibility decision is made from note vocabulary" -- because a
cohort chosen by reading the text is a function of the same text the study then
measures extraction against, and it cannot reproduce by hash (M3).

These tests pin the replacement: a CSV reader that reproduces the SQL, a
selection that reports its own exclusion flow, and an extraction step that
refuses to run against an extract the cohort did not come from.

Every CSV here is synthetic and MIMIC-shaped. No real record is involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meddial.cohort import evaluate_admission
from meddial.cohort.mimic_csv import MimicCsvError, MimicCsvSource

LONG_NOTE = "Chief complaint: sore throat. " + ("Patient reports gradual onset. " * 30)


def _write_extract(root: Path, *, note_category_200: str = "Nursing") -> Path:
    """A four-admission extract: one eligible case and three distinct exclusions."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "ADMISSIONS.csv").write_text(
        "SUBJECT_ID,HADM_ID,ADMITTIME,DISCHTIME,DEATHTIME,ADMISSION_TYPE,"
        "HOSPITAL_EXPIRE_FLAG,RELIGION,MARITAL_STATUS,ETHNICITY,INSURANCE\n"
        "1,100,2150-03-01 10:00:00,2150-03-04 12:00:00,,ELECTIVE,0,CATHOLIC,SINGLE,WHITE,Private\n"
        "2,200,2150-05-01 10:00:00,2150-05-03 12:00:00,,EMERGENCY,0,,MARRIED,WHITE,Medicare\n"
        "3,300,2150-06-01 10:00:00,2150-06-02 12:00:00,,ELECTIVE,0,,,,\n"
        "4,400,2150-07-01 10:00:00,2150-07-30 12:00:00,,ELECTIVE,0,,,,\n",
        encoding="utf-8",
    )
    # Subject 3's date of birth is shifted the way MIMIC shifts ages over 89.
    (root / "PATIENTS.csv").write_text(
        "SUBJECT_ID,DOB,GENDER\n"
        "1,2100-06-15,F\n2,2110-01-01,M\n3,1850-01-01,F\n4,2100-01-01,M\n",
        encoding="utf-8",
    )
    (root / "NOTEEVENTS.csv").write_text(
        "ROW_ID,SUBJECT_ID,HADM_ID,CHARTDATE,CATEGORY,TEXT\n"
        f'11,1,100,2150-03-04,Discharge summary,"older {LONG_NOTE}"\n'
        f'12,1,100,2150-03-05,Discharge summary,"NEWEST {LONG_NOTE}"\n'
        f'13,2,200,2150-05-03,{note_category_200},"{LONG_NOTE}"\n'
        f'14,3,300,2150-06-02,Discharge summary,"icu {LONG_NOTE}"\n'
        f'15,4,400,2150-07-30,Discharge summary,"long stay {LONG_NOTE}"\n',
        encoding="utf-8",
    )
    (root / "ICUSTAYS.csv").write_text("HADM_ID\n300\n", encoding="utf-8")
    (root / "PROCEDURES_ICD.csv").write_text(
        "HADM_ID,ICD9_CODE\n100,38.93\n", encoding="utf-8"
    )
    (root / "DIAGNOSES_ICD.csv").write_text(
        "HADM_ID,ICD9_CODE\n100,401.9\n100,401.9\n200,250.00\n", encoding="utf-8"
    )
    return root


def _by_hadm(source: MimicCsvSource) -> dict[int, object]:
    return {record.hadm_id: record for record in source.admission_records()}


# -- the loader reproduces the SQL ------------------------------------------


def test_a_directory_missing_a_required_table_says_which_one(tmp_path: Path) -> None:
    extract = _write_extract(tmp_path / "mimic")
    (extract / "ICUSTAYS.csv").unlink()

    with pytest.raises(MimicCsvError, match="ICUSTAYS.csv"):
        MimicCsvSource(extract)


def test_the_latest_discharge_summary_wins(tmp_path: Path) -> None:
    """``chartdate DESC, row_id DESC`` cut to rank 1, as the SQL does."""
    records = _by_hadm(MimicCsvSource(_write_extract(tmp_path / "mimic")))

    assert records[100].row_id == 12
    assert records[100].note_text.startswith("NEWEST")


def test_icd9_codes_are_normalised_and_deduplicated(tmp_path: Path) -> None:
    records = _by_hadm(MimicCsvSource(_write_extract(tmp_path / "mimic")))

    assert records[100].diagnosis_icd9_codes == ("4019",)
    assert records[100].procedure_icd9_codes == ("3893",)


def test_an_admission_without_a_discharge_summary_is_still_a_candidate(
    tmp_path: Path,
) -> None:
    """The SQL LEFT JOINs notes: E9 excludes it, the loader does not."""
    records = _by_hadm(MimicCsvSource(_write_extract(tmp_path / "mimic")))

    assert 200 in records
    assert records[200].note_text == ""
    assert [c.value for c in evaluate_admission(records[200]).fired] == ["E9"]


def test_the_shifted_date_of_birth_is_left_alone_for_E5_to_catch(tmp_path: Path) -> None:
    """MIMIC shifts over-89 ages to ~300; E5 excludes them as it would any other."""
    records = _by_hadm(MimicCsvSource(_write_extract(tmp_path / "mimic")))

    assert records[300].age_years > 200
    assert "E5" in [c.value for c in evaluate_admission(records[300]).fired]


def test_each_exclusion_criterion_fires_on_its_own_case(tmp_path: Path) -> None:
    records = _by_hadm(MimicCsvSource(_write_extract(tmp_path / "mimic")))
    fired = {h: [c.value for c in evaluate_admission(r).fired] for h, r in records.items()}

    assert fired[100] == []                  # eligible
    assert fired[200] == ["E9"]              # no discharge summary
    assert set(fired[300]) == {"E1", "E5"}   # ICU stay, shifted age
    assert fired[400] == ["E7"]              # 29-day stay


def test_demographics_carry_no_date_of_birth(tmp_path: Path) -> None:
    """Age is what a policy can act on; a birth date is only a re-identifier."""
    demographics = MimicCsvSource(_write_extract(tmp_path / "mimic")).demographics()

    fields = demographics[(1, 100)]
    assert fields["Sex"] == "F"
    assert not any("dob" in key.lower() or "birth" in key.lower() for key in fields)


def test_the_snapshot_hash_changes_when_the_extract_changes(tmp_path: Path) -> None:
    """A cohort is only reproducible if its source is identified."""
    extract = _write_extract(tmp_path / "mimic")
    before = MimicCsvSource(extract).snapshot_hash()
    (extract / "ICUSTAYS.csv").write_text("HADM_ID\n300\n400\n", encoding="utf-8")

    assert MimicCsvSource(extract).snapshot_hash() != before


# -- meddial-cohort ---------------------------------------------------------


def test_cohort_command_writes_a_manifest_with_the_exclusion_flow(
    tmp_path: Path, capsys
) -> None:
    """M3 needs the flow counts, and they must be a by-product of selection."""
    from meddial.cli import cohort_main

    extract = _write_extract(tmp_path / "mimic")
    out = tmp_path / "cohort"

    assert cohort_main(["--csv-dir", str(extract), "--out", str(out), "--n", "1"]) == 0

    manifest = json.loads((out / "cohort_private_manifest.json").read_text(encoding="utf-8"))
    assert [c["subject_id"] for c in manifest["selected"]] == [1]
    assert manifest["cohort_hash"]
    flow = {stage["criterion"]: stage["excluded"] for stage in manifest["exclusion_flow"]}
    assert flow["E1"] == 1 and flow["E7"] == 1 and flow["E9"] == 1
    assert "Exclusion flow" in capsys.readouterr().out


def test_the_same_seed_and_extract_reproduce_the_same_cohort(tmp_path: Path) -> None:
    from meddial.cli import cohort_main

    extract = _write_extract(tmp_path / "mimic")
    hashes = []
    for name in ("a", "b"):
        out = tmp_path / name
        cohort_main(
            ["--csv-dir", str(extract), "--out", str(out), "--n", "1", "--seed", "7"]
        )
        manifest = json.loads(
            (out / "cohort_private_manifest.json").read_text(encoding="utf-8")
        )
        hashes.append(manifest["cohort_hash"])

    assert hashes[0] == hashes[1]


def test_cohort_output_inside_the_repository_is_refused(tmp_path: Path) -> None:
    from meddial.cli import cohort_main

    extract = _write_extract(tmp_path / "mimic")
    with pytest.raises(SystemExit, match="inside the repository"):
        cohort_main(["--csv-dir", str(extract), "--out", str(Path(__file__).parent)])


# -- meddial-scr ------------------------------------------------------------


def test_scr_refuses_an_extract_the_cohort_did_not_come_from(tmp_path: Path) -> None:
    """Otherwise the manifest and the references silently describe different data."""
    from meddial.cli import cohort_main, scr_main

    extract = _write_extract(tmp_path / "mimic")
    out = tmp_path / "cohort"
    cohort_main(["--csv-dir", str(extract), "--out", str(out), "--n", "1"])

    (extract / "ICUSTAYS.csv").write_text("HADM_ID\n300\n400\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="does not match"):
        scr_main(
            [
                "--csv-dir", str(extract),
                "--cohort", str(out / "cohort_private_manifest.json"),
                "--out", str(tmp_path / "scr"),
            ]
        )


def test_scr_refuses_a_manifest_naming_no_cases(tmp_path: Path) -> None:
    from meddial.cli import scr_main

    extract = _write_extract(tmp_path / "mimic")
    manifest = tmp_path / "empty.json"
    manifest.write_text(json.dumps({"selected": []}), encoding="utf-8")

    with pytest.raises(SystemExit, match="no selected cases"):
        scr_main(
            [
                "--csv-dir", str(extract),
                "--cohort", str(manifest),
                "--out", str(tmp_path / "scr"),
            ]
        )


# -- the old path is closed -------------------------------------------------


def test_the_text_selecting_entry_point_refuses_and_names_its_replacement() -> None:
    """Leaving it runnable would let a non-reproducible cohort be built by habit."""
    import gtmf_creation

    with pytest.raises(SystemExit, match="meddial-cohort"):
        gtmf_creation.main()


# -- unevaluable records ----------------------------------------------------


def test_an_admission_discharged_before_it_began_is_reported_not_raised(
    tmp_path: Path,
) -> None:
    """MIMIC-III holds 98 such rows; one must not abandon a 59k-record build.

    Their discharge precedes their admission by under a day -- back-entered
    timestamps, not a parsing artefact. Length of stay is undefined for them,
    so E7 cannot be evaluated and they are excluded; but they never reach a
    criterion, so they appear in no E-stage and would vanish from the counts
    M3 asks to be reported if they were merely dropped.
    """
    from meddial.cohort import select_cohort
    from meddial.cohort.mimic_csv import MimicCsvSource

    extract = _write_extract(tmp_path / "mimic")
    # Subject 1 is the only eligible case; invert subject 4's stay instead.
    text = (extract / "ADMISSIONS.csv").read_text(encoding="utf-8")
    text = text.replace(
        "4,400,2150-07-01 10:00:00,2150-07-30 12:00:00",
        "4,400,2150-07-01 10:00:00,2150-07-01 02:00:00",
    )
    (extract / "ADMISSIONS.csv").write_text(text, encoding="utf-8")

    source = MimicCsvSource(extract)
    selection = select_cohort(
        list(source.admission_records()),
        source_snapshot_hash=source.snapshot_hash(),
        n_cases=1,
    )

    assert [(m.subject_id, m.hadm_id) for m in selection.malformed] == [(4, 400)]
    assert "dischtime precedes admittime" in selection.malformed[0].reason
    assert len(selection.selected) == 1, "the build still completes"


def test_the_manifest_reports_unevaluable_candidates(tmp_path: Path) -> None:
    """A silent drop would leave a hole in the reported flow."""
    from meddial.cli import cohort_main

    extract = _write_extract(tmp_path / "mimic")
    text = (extract / "ADMISSIONS.csv").read_text(encoding="utf-8")
    text = text.replace(
        "4,400,2150-07-01 10:00:00,2150-07-30 12:00:00",
        "4,400,2150-07-01 10:00:00,2150-07-01 02:00:00",
    )
    (extract / "ADMISSIONS.csv").write_text(text, encoding="utf-8")
    out = tmp_path / "cohort"

    cohort_main(["--csv-dir", str(extract), "--out", str(out), "--n", "1"])

    manifest = json.loads((out / "cohort_private_manifest.json").read_text(encoding="utf-8"))
    assert manifest["malformed_candidate_count"] == 1
    assert manifest["malformed_candidates"][0]["hadm_id"] == 400
