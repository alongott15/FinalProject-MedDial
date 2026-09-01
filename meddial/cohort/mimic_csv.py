"""Read a MIMIC-III extract from the CSV files as PhysioNet ships them.

``configs/cohort/criteria_v1.sql`` is the authoritative candidate query, but it
needs a Postgres instance loaded with MIMIC-III. Most work starts from the CSV
files, and there was no path from those files to
:func:`~meddial.cohort.select.select_cohort` -- which is why the only runnable
extraction path selected notes by *reading their text*, the one thing the
cohort design forbids.

This module is that path for local files. Everything it decides -- which note
is the discharge documentation, how ICU days total, what a candidate looks like
-- lives in :class:`~meddial.cohort.mimic_source.MimicSource` and is shared with
:class:`~meddial.cohort.mimic_bigquery.MimicBigQuerySource`, so the two backends
cannot answer differently. What is left here is only how to get MIMIC's columns
out of a directory of CSVs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from pathlib import Path

import pandas as pd

from meddial.cohort.mimic_source import (
    COLUMNS,
    DISCHARGE_SUMMARY,
    NOTE_CHUNK_ROWS,
    NOTE_SEPARATOR,
    TABLES,
    MimicSource,
)

REQUIRED_FILES: tuple[str, ...] = tuple(f"{table}.csv" for table in TABLES)


class MimicCsvError(RuntimeError):
    """The CSV directory is not a usable MIMIC-III extract."""


class MimicCsvSource(MimicSource):
    """A MIMIC-III CSV directory, read into cohort candidates."""

    def __init__(self, csv_dir: str | Path) -> None:
        self.csv_dir = Path(csv_dir)
        if not self.csv_dir.is_dir():
            raise MimicCsvError(f"CSV directory not found: {self.csv_dir}")
        missing = [name for name in REQUIRED_FILES if not (self.csv_dir / name).is_file()]
        if missing:
            raise MimicCsvError(
                "MIMIC-III CSV directory is missing required file(s): "
                + ", ".join(missing)
                + f". Expected all of {', '.join(REQUIRED_FILES)} in {self.csv_dir}."
            )
        self._snapshot_hash: str | None = None

    # -- provenance --------------------------------------------------------

    def snapshot_hash(self) -> str:
        """SHA-256 over the exact bytes of every source file, in a fixed order.

        The cohort is only reproducible if the extract it came from is
        identified. Hashing sizes or modification times would let a different
        extract masquerade as the same one, so the content is hashed even
        though NOTEEVENTS is large -- it is paid once per cohort build.
        """
        if self._snapshot_hash is not None:
            return self._snapshot_hash
        digest = hashlib.sha256()
        for name in REQUIRED_FILES:
            digest.update(name.encode("utf-8"))
            with (self.csv_dir / name).open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        self._snapshot_hash = f"sha256:{digest.hexdigest()}"
        return self._snapshot_hash

    # -- tables ------------------------------------------------------------

    def table(self, name: str, *, parse_dates: Sequence[str] = ()) -> pd.DataFrame:
        return self._read(name, **_date_kwargs(parse_dates))

    def iter_table(
        self,
        name: str,
        *,
        parse_dates: Sequence[str] = (),
        chunk_rows: int = NOTE_CHUNK_ROWS,
    ) -> Iterator[pd.DataFrame]:
        return iter(self._read(name, chunksize=chunk_rows, **_date_kwargs(parse_dates)))

    def _read(self, name: str, **kwargs) -> pd.DataFrame:
        return pd.read_csv(
            self.csv_dir / f"{name}.csv",
            usecols=list(COLUMNS[name]),
            low_memory=False,
            **kwargs,
        )


def _date_kwargs(parse_dates: Sequence[str]) -> dict[str, list[str]]:
    return {"parse_dates": list(parse_dates)} if parse_dates else {}


__all__ = [
    "DISCHARGE_SUMMARY",
    "NOTE_SEPARATOR",
    "REQUIRED_FILES",
    "MimicCsvError",
    "MimicCsvSource",
]
