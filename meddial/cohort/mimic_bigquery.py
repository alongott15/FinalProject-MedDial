"""Read the same MIMIC-III extract from BigQuery instead of local CSVs.

PhysioNet publishes MIMIC-III on BigQuery as ``physionet-data``, readable by
any Google account whose PhysioNet credentialing has been linked to it. That
matters for two reasons that have nothing to do with convenience:

* the CSV distribution is tens of gigabytes, which a hosted notebook will not
  hold, so the BigQuery copy is what makes a GPU runtime such as Colab usable
  at all (see ``notebooks/meddial_colab.ipynb``);
* the copy is read-only and shared, so two people building the cohort from it
  are demonstrably building it from the same rows.

**This does not weaken decision D2 / GOV-3.** That decision governs where
restricted text is *sent*: generation and scoring may only reach a loopback
model server, and the provider layer refuses a hosted endpoint before it opens
a socket. Reading from BigQuery is the opposite direction -- MIMIC-III flowing
from the credentialed archive to the operator, exactly as downloading the CSVs
is -- and nothing here sends a note anywhere. On Colab the model server runs
inside the runtime, so the loopback rule holds there unchanged.

Everything this module decides about the data is inherited from
:class:`~meddial.cohort.mimic_source.MimicSource`. What is here is only how to
get MIMIC's columns out of BigQuery: dataset names, the projection, and a
provenance hash for a source whose bytes cannot be read.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from meddial.cohort.mimic_source import (
    COLUMNS,
    DISCHARGE_SUMMARY,
    NOTE_CHUNK_ROWS,
    TABLES,
    MimicSource,
)
from meddial.llm.provider import DISABLE_NETWORK_ENV

DEFAULT_CLINICAL_DATASET = "physionet-data.mimiciii_clinical"
"""Where PhysioNet publishes the structured tables."""

DEFAULT_NOTES_DATASET = "physionet-data.mimiciii_notes"
"""NOTEEVENTS lives in its own dataset, gated by its own PhysioNet agreement."""

DEFAULT_MAX_GIB_BILLED = 64.0
"""Refuse a query that would scan more than this, per query.

Reading the six tables costs a few GB, nearly all of it ``NOTEEVENTS.TEXT``. A
typo in a dataset name, though, can point the same projection at something
enormous, and BigQuery bills the scan whether or not the result was wanted.
This ceiling turns that into a failed query rather than an invoice; raise it
with ``--bq-max-gib`` if a legitimate read ever exceeds it.
"""

PLACEHOLDER_PROJECTS = frozenset(
    {
        "your-gcp-project-id",
        "your-project-id",
        "my-project-id",
        "my-billing-project-id",
        "project-id",
    }
)
"""Values that are documentation, not projects, and must never reach BigQuery."""

_NOTES_TABLE = "NOTEEVENTS"

_PREDICATES: dict[str, str] = {
    # Pushed down because NOTEEVENTS is by far the largest table and most of
    # its rows are not discharge documentation. The identical test runs again
    # in MimicSource._discharge_notes, which is where the definition of
    # "discharge summary" actually lives; this is only an early filter.
    _NOTES_TABLE: f"LOWER(TRIM(CATEGORY)) = '{DISCHARGE_SUMMARY}' AND HADM_ID IS NOT NULL",
}


class MimicBigQueryError(RuntimeError):
    """BigQuery is not usable as a MIMIC-III source right now."""


class MimicBigQuerySource(MimicSource):
    """The ``physionet-data`` MIMIC-III tables, read into cohort candidates.

    ``billing_project`` is the operator's own Google Cloud project: BigQuery
    bills the account that runs the query, not the one that publishes the
    data, so it is required and cannot be defaulted. ``client`` is injectable
    so the query surface can be tested without a network.
    """

    def __init__(
        self,
        billing_project: str,
        *,
        clinical_dataset: str = DEFAULT_CLINICAL_DATASET,
        notes_dataset: str = DEFAULT_NOTES_DATASET,
        client: Any | None = None,
        max_gib_billed: float | None = DEFAULT_MAX_GIB_BILLED,
        cache_dir: str | Path | None = None,
    ) -> None:
        if not billing_project:
            raise MimicBigQueryError(
                "A billing project is required: BigQuery bills the project that runs "
                "the query, not the one that publishes the data."
            )
        if billing_project.strip().strip("<>") in PLACEHOLDER_PROJECTS:
            # Worth its own check because this one gets a long way in before it
            # fails. Reading table metadata needs no billing project, so
            # snapshot_hash() succeeds and prints a plausible
            # ``bigquery-sha256:`` line; the placeholder only bites at the first
            # query, as a raw 400 "ProjectId must be non-empty" from inside the
            # client library, minutes later and nowhere near its cause.
            raise MimicBigQueryError(
                f"{billing_project!r} is the placeholder from the README and the Colab "
                "notebook, not a project id. Set MIMIC_BIGQUERY_PROJECT (or pass "
                "--bq-project) to a Google Cloud project you own; BigQuery bills the "
                "project that runs the query."
            )
        self.billing_project = billing_project
        self.clinical_dataset = clinical_dataset.rstrip(".")
        self.notes_dataset = notes_dataset.rstrip(".")
        self._max_bytes_billed = (
            None if max_gib_billed is None else int(max_gib_billed * 1024**3)
        )
        self._client = client if client is not None else _default_client(billing_project)
        self._snapshot_hash: str | None = None
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None

    # -- provenance --------------------------------------------------------

    def snapshot_hash(self) -> str:
        """SHA-256 over each table's identity, row count and last modification.

        The CSV backend hashes the bytes it reads. That is not available here
        -- the bytes stay in BigQuery -- so this hashes what BigQuery states
        about them: the fully-qualified table id, ``num_rows`` and the
        last-modified timestamp, for all six tables in a fixed order. A table
        that gained a row, lost a row, or was rewritten produces a different
        hash, which is the property the manifest needs.

        It is deliberately prefixed ``bigquery-sha256:`` rather than
        ``sha256:``, because the two backends are not interchangeable once a
        cohort exists. ``select_cohort`` salts its selection key with this
        value, so the same seed over the same eligible pool draws a *different
        sample* from BigQuery than from the CSVs -- the sample is a property of
        an identified artefact, by design. ``meddial-scr`` compares these
        strings exactly, so an operator who selects one way and extracts the
        other is told so rather than quietly allowed. Pick a backend and build
        the whole pipeline on it.
        """
        if self._snapshot_hash is not None:
            return self._snapshot_hash
        _refuse_if_network_disabled()
        digest = hashlib.sha256()
        for name in TABLES:
            table_id = self._table_id(name)
            try:
                meta = self._client.get_table(table_id)
            except Exception as exc:  # reported against the table that failed
                raise MimicBigQueryError(
                    f"Cannot read {table_id}: {type(exc).__name__}: {exc}. Check that the "
                    "account is authenticated, that its PhysioNet credentialing is linked "
                    "to Google BigQuery, and that the dataset names are right."
                ) from exc
            num_rows = getattr(meta, "num_rows", None)
            modified = getattr(meta, "modified", None)
            if num_rows is None or modified is None:
                raise MimicBigQueryError(
                    f"{table_id} reports no row count or modification time, so the "
                    "snapshot it holds cannot be identified. Refusing to build a cohort "
                    "that could not be reproduced."
                )
            digest.update(f"{table_id}\n{int(num_rows)}\n{modified.isoformat()}\n".encode())
        self._snapshot_hash = f"bigquery-sha256:{digest.hexdigest()}"
        return self._snapshot_hash

    # -- tables ------------------------------------------------------------

    def table(self, name: str, *, parse_dates: Sequence[str] = ()) -> pd.DataFrame:
        cached = self._cache_path(name)
        if cached is not None and cached.is_file():
            return _typed(pd.read_parquet(cached), parse_dates)
        frame = self._rows(name).to_dataframe()
        if cached is not None:
            self._write_cache(cached, frame)
        return _typed(frame, parse_dates)

    def iter_table(
        self,
        name: str,
        *,
        parse_dates: Sequence[str] = (),
        chunk_rows: int = NOTE_CHUNK_ROWS,
    ) -> Iterator[pd.DataFrame]:
        cached = self._cache_path(name)
        if cached is not None and cached.is_file():
            yield from self._iter_cache(cached, parse_dates, chunk_rows)
            return

        # Not cached: stream from BigQuery and write each chunk as it arrives.
        # Collecting the table to write it once would defeat the reason this
        # method exists -- NOTEEVENTS does not fit comfortably in memory.
        writer = None
        try:
            for chunk in self._rows(name, page_size=chunk_rows).to_dataframe_iterable():
                if cached is not None:
                    writer = self._append_cache(writer, cached, chunk)
                yield _typed(chunk, parse_dates)
        except BaseException:
            # A partial parquet file would be read back as a complete table on
            # the next run, silently shortening the cohort.
            if writer is not None:
                writer.close()
                writer = None
            if cached is not None:
                cached.unlink(missing_ok=True)
            raise
        finally:
            if writer is not None:
                writer.close()

    # -- the local copy ----------------------------------------------------

    def _cache_path(self, name: str) -> Path | None:
        """Where ``name`` is cached, or ``None`` when caching is off.

        The first call validates the directory against the snapshot it was
        built from. A cache written from a different snapshot describes
        different rows, and reading it while reporting this snapshot's hash
        would produce a cohort that reproduces from neither.
        """
        if self.cache_dir is None:
            return None
        self._ensure_cache_matches_snapshot()
        return self.cache_dir / f"{name}.parquet"

    def _ensure_cache_matches_snapshot(self) -> None:
        assert self.cache_dir is not None
        stamp = self.cache_dir / "_snapshot.json"
        snapshot = self.snapshot_hash()
        if not stamp.is_file():
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            stamp.write_text(
                json.dumps({"source_snapshot_hash": snapshot}, indent=2), encoding="utf-8"
            )
            return
        try:
            recorded = json.loads(stamp.read_text(encoding="utf-8")).get("source_snapshot_hash")
        except (OSError, json.JSONDecodeError) as exc:
            raise MimicBigQueryError(f"Cannot read {stamp}: {exc}") from exc
        if recorded != snapshot:
            raise MimicBigQueryError(
                f"The cache in {self.cache_dir} was downloaded from a different snapshot of "
                f"physionet-data.\n  cache:  {recorded}\n  now:    {snapshot}\n"
                "A table has been rewritten since, so the two describe different rows. Delete "
                "the cache directory to download again, or point --bq-cache-dir somewhere else. "
                "A cohort selected across the two would reproduce from neither."
            )

    def _write_cache(self, path: Path, frame: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".parquet.partial")
        frame.to_parquet(partial, index=False)
        partial.replace(path)

    def _append_cache(self, writer: Any, path: Path, chunk: pd.DataFrame) -> Any:
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.Table.from_pandas(chunk, preserve_index=False)
        if writer is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            writer = pq.ParquetWriter(path, table.schema)
        writer.write_table(table)
        return writer

    def _iter_cache(
        self, path: Path, parse_dates: Sequence[str], chunk_rows: int
    ) -> Iterator[pd.DataFrame]:
        import pyarrow.parquet as pq

        reader = pq.ParquetFile(path)
        for batch in reader.iter_batches(batch_size=chunk_rows):
            yield _typed(batch.to_pandas(), parse_dates)

    # -- the query ---------------------------------------------------------

    def _table_id(self, name: str) -> str:
        dataset = self.notes_dataset if name == _NOTES_TABLE else self.clinical_dataset
        return f"{dataset}.{name.lower()}"

    def sql_for(self, name: str) -> str:
        """The projection for one table.

        Every column is aliased to MIMIC's uppercase name: BigQuery resolves
        identifiers case-insensitively but labels output columns exactly as the
        publisher declared them, which in ``physionet-data`` is lowercase. The
        alias is what lets the shared reader address one set of names whichever
        backend answered.
        """
        columns = ", ".join(f"{column} AS {column}" for column in COLUMNS[name])
        sql = f"SELECT {columns} FROM `{self._table_id(name)}`"
        predicate = _PREDICATES.get(name)
        return sql if predicate is None else f"{sql} WHERE {predicate}"

    def _rows(self, name: str, *, page_size: int | None = None) -> Any:
        _refuse_if_network_disabled()
        sql = self.sql_for(name)
        try:
            job = self._client.query(sql, job_config=self._job_config())
            return job.result(page_size=page_size) if page_size else job.result()
        except Exception as exc:  # reported against the query that failed
            raise MimicBigQueryError(
                f"BigQuery query failed for {self._table_id(name)}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _job_config(self) -> Any | None:
        if self._max_bytes_billed is None:
            return None
        try:
            from google.cloud import bigquery
        except ImportError:
            # No client library means no real BigQuery job and so no bill to
            # cap; this branch is reachable only with an injected client.
            return None
        return bigquery.QueryJobConfig(maximum_bytes_billed=self._max_bytes_billed)


def _default_client(billing_project: str) -> Any:
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise MimicBigQueryError(
            "google-cloud-bigquery is not installed. Install the BigQuery extra: "
            'pip install -e ".[bigquery]"'
        ) from exc
    try:
        return bigquery.Client(project=billing_project)
    except Exception as exc:  # a credentials failure is the common case
        raise MimicBigQueryError(
            f"Cannot open a BigQuery client for project {billing_project!r}: "
            f"{type(exc).__name__}: {exc}. Authenticate first -- "
            "'gcloud auth application-default login' locally, or "
            "google.colab.auth.authenticate_user() on Colab."
        ) from exc


def _refuse_if_network_disabled() -> None:
    """Honour the same kill-switch the provider layer honours.

    CI runs with ``MEDDIAL_DISABLE_EXTERNAL_CALLS=1`` so that a test reaching
    for a real service fails loudly rather than quietly billing a project and
    pulling restricted data onto a build machine.
    """
    if os.environ.get(DISABLE_NETWORK_ENV) not in (None, "", "0"):
        raise MimicBigQueryError(
            f"MimicBigQuerySource attempted a network call while {DISABLE_NETWORK_ENV} "
            "is set. Use a CSV fixture in tests."
        )


def _typed(frame: pd.DataFrame, parse_dates: Sequence[str]) -> pd.DataFrame:
    """Give the frame the dtypes the shared reader expects.

    BigQuery types its own columns, so this is narrower than it looks. Nullable
    integer columns become ``float64`` and a date column that arrived as
    ``object`` (which happens when ``db-dtypes`` is absent) becomes datetime --
    in both cases the dtype ``pandas.read_csv`` would have produced for the
    same data. The point is that the shared reader sees one shape whichever
    backend answered, rather than growing a branch per backend.
    """
    if frame.empty:
        return frame
    updates: dict[str, pd.Series] = {}
    for column, dtype in frame.dtypes.items():
        if isinstance(dtype, pd.api.extensions.ExtensionDtype) and pd.api.types.is_integer_dtype(
            dtype
        ):
            updates[str(column)] = frame[column].astype("float64")
    for column in parse_dates:
        if column in frame.columns and not pd.api.types.is_datetime64_any_dtype(frame[column]):
            updates[column] = pd.to_datetime(frame[column], errors="coerce")
    return frame.assign(**updates) if updates else frame


__all__ = [
    "DEFAULT_CLINICAL_DATASET",
    "DEFAULT_MAX_GIB_BILLED",
    "DEFAULT_NOTES_DATASET",
    "MimicBigQueryError",
    "MimicBigQuerySource",
]
