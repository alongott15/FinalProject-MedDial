"""The BigQuery backend must select the cohort the CSV backend selects.

Adding a second way to read MIMIC-III adds a way for the cohort to depend on
where the extract happened to be stored, which would undo M3: two people with
the same seed would get different cases and neither could show why. So the
central test here is an equivalence -- the same synthetic extract, served once
from files and once through a fake BigQuery client, must produce identical
:class:`AdmissionRecord`s.

The fake reproduces the parts of BigQuery that could break that equivalence:
``physionet-data`` declares MIMIC's columns in lowercase, hands back nullable
integers and typed dates rather than the strings a CSV holds, and applies
whatever predicate the query pushed down. It answers by *parsing the SQL the
source actually generated*, so a projection that asked for the wrong columns,
the wrong table or the wrong filter fails these tests rather than passing them.

Every row here is synthetic and MIMIC-shaped. No real record is involved.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from test_cohort_mimic_csv import _write_extract

from meddial.cohort.mimic_bigquery import (
    DEFAULT_CLINICAL_DATASET,
    DEFAULT_NOTES_DATASET,
    MimicBigQueryError,
    MimicBigQuerySource,
)
from meddial.cohort.mimic_csv import MimicCsvSource
from meddial.cohort.mimic_source import COLUMNS, TABLES
from meddial.llm.provider import DISABLE_NETWORK_ENV

_INTEGER_COLUMNS = frozenset({"ROW_ID", "SUBJECT_ID", "HADM_ID", "HOSPITAL_EXPIRE_FLAG"})
_TIMESTAMP_COLUMNS = frozenset({"ADMITTIME", "DISCHTIME", "DEATHTIME", "DOB"})
_DATE_COLUMNS = frozenset({"CHARTDATE"})

_MODIFIED = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


# -- a BigQuery that is only as helpful as the real one ---------------------


class _FakeTable:
    def __init__(self, table_id: str, num_rows: int | None, modified: datetime | None) -> None:
        self.full_table_id = table_id
        self.num_rows = num_rows
        self.modified = modified


class _FakeRows:
    def __init__(self, frame: pd.DataFrame, page_size: int | None) -> None:
        self._frame = frame
        self._page_size = page_size

    def to_dataframe(self) -> pd.DataFrame:
        return self._frame

    def to_dataframe_iterable(self):
        size = self._page_size or max(len(self._frame), 1)
        for start in range(0, max(len(self._frame), 1), size):
            yield self._frame.iloc[start : start + size]


class _FakeJob:
    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def result(self, page_size: int | None = None) -> _FakeRows:
        return _FakeRows(self._frame, page_size)


class FakeBigQueryClient:
    """Serves a CSV extract the way ``physionet-data`` would serve it."""

    def __init__(self, extract: Path, *, modified: datetime = _MODIFIED) -> None:
        self.extract = extract
        self.modified = modified
        self.queries: list[str] = []
        self.job_configs: list[object] = []

    # -- the surface MimicBigQuerySource uses -------------------------------

    def get_table(self, table_id: str) -> _FakeTable:
        return _FakeTable(table_id, len(self._stored(_table_name(table_id))), self.modified)

    def query(self, sql: str, job_config: object = None) -> _FakeJob:
        self.queries.append(sql)
        self.job_configs.append(job_config)
        frame = self._stored(_table_name(_from_clause(sql)))
        if " WHERE " in sql:
            # The only predicate the source pushes down.
            frame = frame[
                frame["category"].str.strip().str.lower().eq("discharge summary")
                & frame["hadm_id"].notna()
            ]
        selected = _selected_columns(sql)
        frame = frame[[column.lower() for column in selected]]
        frame.columns = list(selected)
        return _FakeJob(_bigquery_dtypes(frame))

    # -- storage ------------------------------------------------------------

    def _stored(self, table: str) -> pd.DataFrame:
        """The table as BigQuery holds it: MIMIC's columns, lowercased."""
        frame = pd.read_csv(self.extract / f"{table}.csv", low_memory=False)
        frame.columns = [column.lower() for column in frame.columns]
        return frame


def _from_clause(sql: str) -> str:
    match = re.search(r"FROM `([^`]+)`", sql)
    assert match, f"no FROM clause in {sql!r}"
    return match.group(1)


def _selected_columns(sql: str) -> list[str]:
    match = re.search(r"SELECT (.+?) FROM ", sql)
    assert match, f"no projection in {sql!r}"
    return [part.split(" AS ")[-1].strip() for part in match.group(1).split(",")]


def _table_name(table_id: str) -> str:
    return table_id.rsplit(".", 1)[-1].upper()


def _bigquery_dtypes(frame: pd.DataFrame) -> pd.DataFrame:
    """The dtypes a BigQuery read produces, which a CSV read does not."""
    out = frame.copy()
    for column in out.columns:
        if column in _INTEGER_COLUMNS:
            out[column] = out[column].astype("Int64")
        elif column in _TIMESTAMP_COLUMNS:
            out[column] = pd.to_datetime(out[column])
        elif column in _DATE_COLUMNS:
            # A DATE column arrives as objects when db-dtypes is absent.
            out[column] = [
                None if pd.isna(value) else pd.Timestamp(value).date() for value in out[column]
            ]
    return out


@pytest.fixture
def extract(tmp_path: Path) -> Path:
    return _write_extract(tmp_path / "mimic")


def _source(extract: Path, **kwargs) -> MimicBigQuerySource:
    return MimicBigQuerySource(
        "my-billing-project", client=FakeBigQueryClient(extract), **kwargs
    )


# -- the equivalence --------------------------------------------------------


def test_bigquery_yields_the_candidates_the_csv_files_yield(extract: Path) -> None:
    """The cohort must not depend on where the extract was stored."""
    from_files = list(MimicCsvSource(extract).admission_records())
    from_bigquery = list(_source(extract).admission_records())

    assert from_bigquery == from_files


def test_bigquery_yields_the_demographics_the_csv_files_yield(extract: Path) -> None:
    assert _source(extract).demographics() == MimicCsvSource(extract).demographics()


def test_the_addendum_is_joined_to_its_summary_here_too(extract: Path) -> None:
    """The one decision most likely to drift between two readers."""
    records = {r.hadm_id: r for r in _source(extract).admission_records()}

    assert "older" in records[100].note_text and "NEWEST" in records[100].note_text
    assert records[100].row_id == 12


# -- the query --------------------------------------------------------------


def test_the_query_reads_only_the_columns_the_criteria_need(extract: Path) -> None:
    """Every column read is a column billed, and TEXT is the expensive one."""
    source = _source(extract)
    list(source.admission_records())

    assert source._client.queries, "no query was issued"
    for sql in source._client.queries:
        table = _table_name(_from_clause(sql))
        assert _selected_columns(sql) == list(COLUMNS[table]), table


def test_the_note_query_filters_to_discharge_summaries_in_bigquery(extract: Path) -> None:
    """NOTEEVENTS is the largest table; scanning its other categories is waste."""
    sql = _source(extract).sql_for("NOTEEVENTS")

    assert "LOWER(TRIM(CATEGORY)) = 'discharge summary'" in sql
    assert "HADM_ID IS NOT NULL" in sql


def test_notes_come_from_the_notes_dataset_and_the_rest_from_the_clinical_one(
    extract: Path,
) -> None:
    """They are separate PhysioNet agreements, and separate datasets."""
    source = _source(extract)

    assert _from_clause(source.sql_for("NOTEEVENTS")) == f"{DEFAULT_NOTES_DATASET}.noteevents"
    assert (
        _from_clause(source.sql_for("ADMISSIONS")) == f"{DEFAULT_CLINICAL_DATASET}.admissions"
    )


def test_lowercase_columns_are_aliased_to_the_names_the_reader_expects(
    extract: Path,
) -> None:
    """physionet-data declares MIMIC's columns in lowercase; the reader does not."""
    frame = _source(extract).table("ICUSTAYS")

    assert list(frame.columns) == ["HADM_ID", "LOS"]


# -- provenance -------------------------------------------------------------


def test_the_snapshot_hash_covers_every_table(extract: Path) -> None:
    before = _source(extract).snapshot_hash()

    for table in TABLES:
        grown = MimicBigQuerySource(
            "my-billing-project", client=_client_with_extra_row(extract, table)
        )
        assert grown.snapshot_hash() != before, f"a change to {table} went unnoticed"


def test_the_same_tables_hash_the_same_way(extract: Path) -> None:
    assert _source(extract).snapshot_hash() == _source(extract).snapshot_hash()


def test_a_rewritten_table_changes_the_hash_at_the_same_row_count(extract: Path) -> None:
    """Row counts alone would let a corrected extract masquerade as the old one."""
    rewritten = MimicBigQuerySource(
        "my-billing-project",
        client=FakeBigQueryClient(extract, modified=datetime(2025, 6, 1, tzinfo=timezone.utc)),
    )

    assert rewritten.snapshot_hash() != _source(extract).snapshot_hash()


def test_a_bigquery_snapshot_can_never_be_mistaken_for_a_csv_one(extract: Path) -> None:
    """meddial-scr compares these strings exactly, so the schemes must not collide."""
    bigquery_hash = _source(extract).snapshot_hash()

    assert bigquery_hash.startswith("bigquery-sha256:")
    assert MimicCsvSource(extract).snapshot_hash().startswith("sha256:")


def test_a_table_that_cannot_state_its_size_is_refused(extract: Path) -> None:
    """An unidentifiable snapshot is an unreproducible cohort."""

    class Silent(FakeBigQueryClient):
        def get_table(self, table_id: str) -> _FakeTable:
            return _FakeTable(table_id, None, None)

    source = MimicBigQuerySource("my-billing-project", client=Silent(extract))

    with pytest.raises(MimicBigQueryError, match="cannot be identified"):
        source.snapshot_hash()


# -- refusals ---------------------------------------------------------------


def test_a_missing_billing_project_is_refused(extract: Path) -> None:
    """BigQuery bills the reader, so there is no sensible default."""
    with pytest.raises(MimicBigQueryError, match="billing project"):
        MimicBigQuerySource("", client=FakeBigQueryClient(extract))


def test_the_test_kill_switch_stops_a_bigquery_read(
    extract: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test reaching a real warehouse bills a project and pulls restricted data."""
    source = _source(extract)
    monkeypatch.setenv(DISABLE_NETWORK_ENV, "1")

    with pytest.raises(MimicBigQueryError, match=DISABLE_NETWORK_ENV):
        source.table("ICUSTAYS")
    with pytest.raises(MimicBigQueryError, match=DISABLE_NETWORK_ENV):
        source.snapshot_hash()


def test_a_failing_query_names_the_table_it_failed_on(extract: Path) -> None:
    class Broken(FakeBigQueryClient):
        def query(self, sql: str, job_config: object = None) -> _FakeJob:
            raise RuntimeError("Access Denied: physionet-data:mimiciii_notes")

    source = MimicBigQuerySource("my-billing-project", client=Broken(extract))

    with pytest.raises(MimicBigQueryError, match="noteevents"):
        source.table("NOTEEVENTS")


def test_the_scan_ceiling_is_expressed_in_bytes(extract: Path) -> None:
    """``--bq-max-gib`` is what stands between a mistyped dataset and an invoice."""
    assert _source(extract, max_gib_billed=2.0)._max_bytes_billed == 2 * 1024**3
    assert _source(extract, max_gib_billed=None)._max_bytes_billed is None


def _client_with_extra_row(extract: Path, table: str) -> FakeBigQueryClient:
    """A client whose ``table`` holds one more row than the extract does."""

    class Grown(FakeBigQueryClient):
        def get_table(self, table_id: str) -> _FakeTable:
            found = super().get_table(table_id)
            if _table_name(table_id) == table:
                found.num_rows += 1
            return found

    return Grown(extract)


# -- the command line -------------------------------------------------------


def test_meddial_cohort_builds_a_manifest_from_bigquery(
    extract: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: a cohort built without the CSV distribution on disk."""
    from meddial.cli import cohort_main

    monkeypatch.setattr(
        "meddial.cohort.mimic_bigquery._default_client",
        lambda project: FakeBigQueryClient(extract),
    )
    out = tmp_path / "cohort"

    assert cohort_main(["--bigquery", "--bq-project", "p", "--out", str(out), "--n", "1"]) == 0

    manifest = json.loads((out / "cohort_private_manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_snapshot_hash"].startswith("bigquery-sha256:")
    assert [(c["subject_id"], c["hadm_id"]) for c in manifest["selected"]] == [(1, 100)]


def test_both_backends_report_the_same_exclusion_flow(
    extract: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eligibility is the part that must not depend on storage.

    The *sample* is a different matter, and the next test says so:
    ``select_cohort`` salts its selection key with the source snapshot hash, so
    a pool larger than ``--n`` yields different cases from the two backends
    even at the same seed. The exclusion flow -- who was eligible at all, and
    which criterion removed everyone else -- is what has to agree, and it is
    what the manuscript reports.
    """
    from meddial.cli import cohort_main

    monkeypatch.setattr(
        "meddial.cohort.mimic_bigquery._default_client",
        lambda project: FakeBigQueryClient(extract),
    )
    from_files = tmp_path / "csv"
    from_bigquery = tmp_path / "bq"
    cohort_main(["--csv-dir", str(extract), "--out", str(from_files), "--n", "1"])
    cohort_main(["--bigquery", "--bq-project", "p", "--out", str(from_bigquery), "--n", "1"])

    csv_manifest = _manifest(from_files)
    bigquery_manifest = _manifest(from_bigquery)
    assert bigquery_manifest["exclusion_flow"] == csv_manifest["exclusion_flow"]
    assert bigquery_manifest["selected"] == csv_manifest["selected"], "the pool holds one case"


def test_the_cohort_hash_records_which_artefact_it_came_from(
    extract: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two hashes for the same cases is the intended answer, not a defect.

    ``_cohort_hash`` mixes in ``source_snapshot_hash`` deliberately: a cohort
    is a claim about a specific extract, and a claim about the CSVs is not a
    claim about the BigQuery tables even when the case list matches. The
    practical consequence is the one ``meddial-scr`` enforces -- pick a backend
    and build the whole pipeline on it.
    """
    from meddial.cli import cohort_main

    monkeypatch.setattr(
        "meddial.cohort.mimic_bigquery._default_client",
        lambda project: FakeBigQueryClient(extract),
    )
    from_files = tmp_path / "csv"
    from_bigquery = tmp_path / "bq"
    cohort_main(["--csv-dir", str(extract), "--out", str(from_files), "--n", "1"])
    cohort_main(["--bigquery", "--bq-project", "p", "--out", str(from_bigquery), "--n", "1"])

    assert _manifest(from_bigquery)["cohort_hash"] != _manifest(from_files)["cohort_hash"]


def _manifest(root: Path) -> dict:
    return json.loads((root / "cohort_private_manifest.json").read_text(encoding="utf-8"))


def test_bigquery_without_a_billing_project_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from meddial.cli import MIMIC_BIGQUERY_PROJECT_ENV, cohort_main

    monkeypatch.setattr("meddial.cli._load_env", lambda: None)
    monkeypatch.delenv(MIMIC_BIGQUERY_PROJECT_ENV, raising=False)

    with pytest.raises(SystemExit, match=MIMIC_BIGQUERY_PROJECT_ENV):
        cohort_main(["--bigquery", "--out", str(tmp_path / "out")])


def test_naming_no_source_at_all_names_both_ways_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An operator on Colab has no CSV directory to be told about."""
    from meddial.cli import MIMIC_CSV_DIR_ENV, cohort_main

    monkeypatch.setattr("meddial.cli._load_env", lambda: None)
    monkeypatch.delenv(MIMIC_CSV_DIR_ENV, raising=False)

    with pytest.raises(SystemExit, match="--bigquery"):
        cohort_main(["--out", str(tmp_path / "out")])


# -- the local copy ---------------------------------------------------------


def test_a_cache_answers_the_second_read_without_querying(extract: Path, tmp_path: Path) -> None:
    """meddial-cohort and meddial-scr each read all six tables.

    Without a cache the scan is paid twice, and BigQuery bills the reader.
    """
    cache = tmp_path / "cache"

    first = _source(extract, cache_dir=cache)
    records = list(first.admission_records())
    queries_to_fill_it = len(first._client.queries)
    assert queries_to_fill_it > 0

    second = _source(extract, cache_dir=cache)
    assert list(second.admission_records()) == records
    assert second._client.queries == [], "a filled cache must not re-query BigQuery"


def test_the_cache_yields_exactly_what_bigquery_yielded(extract: Path, tmp_path: Path) -> None:
    """Round-tripping through parquet must not change a candidate."""
    direct = list(_source(extract).admission_records())

    cache = tmp_path / "cache"
    list(_source(extract, cache_dir=cache).admission_records())
    from_cache = list(_source(extract, cache_dir=cache).admission_records())

    assert from_cache == direct


def test_a_cached_run_keeps_the_bigquery_snapshot(extract: Path, tmp_path: Path) -> None:
    """The cohort is salted with the warehouse's hash, not the local copy's.

    Hashing the downloaded bytes instead would make the cohort a property of
    one machine's dump: re-download, and the same seed draws a different sample.
    """
    cache = tmp_path / "cache"
    source = _source(extract, cache_dir=cache)
    list(source.admission_records())

    assert source.snapshot_hash().startswith("bigquery-sha256:")
    assert source.snapshot_hash() == _source(extract).snapshot_hash()
    stamp = json.loads((cache / "_snapshot.json").read_text(encoding="utf-8"))
    assert stamp["source_snapshot_hash"] == source.snapshot_hash()


def test_a_cache_from_another_snapshot_is_refused(extract: Path, tmp_path: Path) -> None:
    """A rewritten table means the cache and the warehouse describe different rows."""
    cache = tmp_path / "cache"
    list(_source(extract, cache_dir=cache).admission_records())

    moved_on = MimicBigQuerySource(
        "my-billing-project",
        client=FakeBigQueryClient(extract, modified=datetime(2025, 6, 7, 8, 9, tzinfo=timezone.utc)),
        cache_dir=cache,
    )

    with pytest.raises(MimicBigQueryError, match="different snapshot"):
        list(moved_on.admission_records())
