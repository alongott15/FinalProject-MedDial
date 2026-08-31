"""Build :class:`AdmissionRecord`s from the MIMIC-III CSV distribution.

``configs/cohort/criteria_v1.sql`` is the authoritative candidate query, but it
needs a Postgres instance loaded with MIMIC-III. Most work starts from the CSV
files as PhysioNet ships them, and there was no path from those files to
:func:`~meddial.cohort.select.select_cohort` -- which is why the only runnable
extraction path selected notes by *reading their text*, the one thing the
cohort design forbids.

This module is that path. It reproduces the SQL, and the mapping is written out
here so the two can be diffed by eye:

===========================  ==============================================
``criteria_v1.sql``          here
===========================  ==============================================
``discharge_notes`` CTE      :meth:`MimicCsvSource._discharge_notes`
``icu_flags`` CTE            :meth:`MimicCsvSource._icu_hadm_ids`
``procedure_codes`` CTE      :meth:`MimicCsvSource._codes_by_hadm`
``diagnosis_codes`` CTE      :meth:`MimicCsvSource._codes_by_hadm`
final ``SELECT``             :meth:`MimicCsvSource.admission_records`
===========================  ==============================================

No clinical eligibility decision is made here. This module only assembles the
structured facts; :func:`~meddial.cohort.criteria.evaluate_admission` decides.
Note text is carried because E9 measures its length and category, not its
vocabulary.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from meddial.cohort.criteria import AdmissionRecord, normalise_icd9

REQUIRED_FILES: tuple[str, ...] = (
    "ADMISSIONS.csv",
    "PATIENTS.csv",
    "NOTEEVENTS.csv",
    "ICUSTAYS.csv",
    "PROCEDURES_ICD.csv",
    "DIAGNOSES_ICD.csv",
)

_COLUMNS: dict[str, tuple[str, ...]] = {
    "ADMISSIONS.csv": (
        "SUBJECT_ID",
        "HADM_ID",
        "ADMITTIME",
        "DISCHTIME",
        "DEATHTIME",
        "ADMISSION_TYPE",
        "HOSPITAL_EXPIRE_FLAG",
        "RELIGION",
        "MARITAL_STATUS",
        "ETHNICITY",
        "INSURANCE",
    ),
    "PATIENTS.csv": ("SUBJECT_ID", "DOB", "GENDER"),
    "NOTEEVENTS.csv": ("ROW_ID", "SUBJECT_ID", "HADM_ID", "CHARTDATE", "CATEGORY", "TEXT"),
    "ICUSTAYS.csv": ("HADM_ID",),
    "PROCEDURES_ICD.csv": ("HADM_ID", "ICD9_CODE"),
    "DIAGNOSES_ICD.csv": ("HADM_ID", "ICD9_CODE"),
}

_NOTE_CHUNK_ROWS = 50_000
"""NOTEEVENTS is several GB; it is streamed rather than loaded whole."""

DISCHARGE_SUMMARY = "discharge summary"


class MimicCsvError(RuntimeError):
    """The CSV directory is not a usable MIMIC-III extract."""


@dataclass(frozen=True)
class _Note:
    row_id: int
    category: str
    text: str


class MimicCsvSource:
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

    # -- the SQL's CTEs ----------------------------------------------------

    def _read(self, name: str, **kwargs) -> pd.DataFrame:
        return pd.read_csv(
            self.csv_dir / name,
            usecols=list(_COLUMNS[name]),
            low_memory=False,
            **kwargs,
        )

    def _icu_hadm_ids(self) -> set[int]:
        """``icu_flags``: any ICU stay at all marks the admission (E1)."""
        icu = self._read("ICUSTAYS.csv")
        return {int(value) for value in icu["HADM_ID"].dropna().unique()}

    def _codes_by_hadm(self, name: str) -> dict[int, tuple[str, ...]]:
        """``procedure_codes`` / ``diagnosis_codes``: distinct normalised codes."""
        frame = self._read(name).dropna(subset=["HADM_ID", "ICD9_CODE"])
        grouped: dict[int, set[str]] = {}
        for hadm_id, code in zip(frame["HADM_ID"], frame["ICD9_CODE"], strict=False):
            grouped.setdefault(int(hadm_id), set()).add(normalise_icd9(code))
        return {hadm_id: tuple(sorted(codes)) for hadm_id, codes in grouped.items()}

    def _discharge_notes(self) -> dict[int, _Note]:
        """``discharge_notes``: the latest discharge summary per admission.

        Ranked by ``chartdate DESC NULLS LAST, row_id DESC`` and cut to rank 1,
        matching the SQL. Notes are streamed in chunks and only the current best
        per admission is kept, so peak memory stays independent of the size of
        NOTEEVENTS.
        """
        best: dict[int, tuple[tuple[int, int, int], _Note]] = {}
        reader = self._read(
            "NOTEEVENTS.csv", chunksize=_NOTE_CHUNK_ROWS, parse_dates=["CHARTDATE"]
        )
        for chunk in reader:
            summaries = chunk[
                chunk["CATEGORY"].astype("string").str.strip().str.lower() == DISCHARGE_SUMMARY
            ].dropna(subset=["HADM_ID"])
            for row in summaries.itertuples(index=False):
                hadm_id = int(row.HADM_ID)
                chartdate = row.CHARTDATE
                # NULLS LAST under DESC: a dated note outranks an undated one.
                dated = 0 if pd.isna(chartdate) else 1
                stamp = 0 if pd.isna(chartdate) else int(pd.Timestamp(chartdate).value)
                row_id = 0 if pd.isna(row.ROW_ID) else int(row.ROW_ID)
                rank_key = (dated, stamp, row_id)
                current = best.get(hadm_id)
                if current is None or rank_key > current[0]:
                    best[hadm_id] = (
                        rank_key,
                        _Note(
                            row_id=row_id,
                            category="" if pd.isna(row.CATEGORY) else str(row.CATEGORY),
                            text="" if pd.isna(row.TEXT) else str(row.TEXT),
                        ),
                    )
        return {hadm_id: note for hadm_id, (_, note) in best.items()}

    # -- the final SELECT --------------------------------------------------

    def admission_records(self) -> Iterator[AdmissionRecord]:
        """Yield every admission as a candidate, eligible or not.

        The SQL deliberately returns excluded admissions too, because the
        exclusion counts are themselves a reported result. Filtering here would
        destroy the flow diagram before it could be produced.
        """
        icu_hadm_ids = self._icu_hadm_ids()
        procedures = self._codes_by_hadm("PROCEDURES_ICD.csv")
        diagnoses = self._codes_by_hadm("DIAGNOSES_ICD.csv")
        notes = self._discharge_notes()

        patients = self._read("PATIENTS.csv", parse_dates=["DOB"])
        dob_by_subject = {
            int(row.SUBJECT_ID): row.DOB
            for row in patients.itertuples(index=False)
            if not pd.isna(row.SUBJECT_ID)
        }

        admissions = self._read(
            "ADMISSIONS.csv", parse_dates=["ADMITTIME", "DISCHTIME", "DEATHTIME"]
        )
        admissions = admissions.dropna(
            subset=["SUBJECT_ID", "HADM_ID", "ADMITTIME", "DISCHTIME"]
        ).sort_values(["SUBJECT_ID", "ADMITTIME", "HADM_ID"])

        for row in admissions.itertuples(index=False):
            subject_id = int(row.SUBJECT_ID)
            hadm_id = int(row.HADM_ID)
            dob = dob_by_subject.get(subject_id)
            if dob is None or pd.isna(dob):
                # The SQL inner-JOINs patients: no date of birth, no candidate,
                # because E4/E5 could not be evaluated.
                continue
            note = notes.get(hadm_id)
            yield AdmissionRecord(
                subject_id=subject_id,
                hadm_id=hadm_id,
                admittime=_to_datetime(row.ADMITTIME),
                dischtime=_to_datetime(row.DISCHTIME),
                age_years=float(_full_years(dob, row.ADMITTIME)),
                admission_type=_text(row.ADMISSION_TYPE),
                has_icu_stay=hadm_id in icu_hadm_ids,
                hospital_expire_flag=bool(int(row.HOSPITAL_EXPIRE_FLAG or 0)),
                procedure_icd9_codes=procedures.get(hadm_id, ()),
                diagnosis_icd9_codes=diagnoses.get(hadm_id, ()),
                note_text=note.text if note else "",
                note_category=note.category if note else "",
                deathtime=None if pd.isna(row.DEATHTIME) else _to_datetime(row.DEATHTIME),
                row_id=note.row_id if note else None,
            )


    def demographics(self) -> dict[tuple[int, int], dict[str, str]]:
        """Presentational demographics per admission, keyed by (subject, hadm).

        Deliberately excludes ``DOB``. The reference records an age, which is
        what the disclosure policies address; a date of birth adds nothing a
        policy can act on and is a re-identification vector in a document that
        outlives the extract. ``AdmissionRecord.age_years`` already carries the
        only form of it this project needs.
        """
        patients = self._read("PATIENTS.csv", parse_dates=["DOB"])
        sex_by_subject = {
            int(row.SUBJECT_ID): _text(row.GENDER)
            for row in patients.itertuples(index=False)
            if not pd.isna(row.SUBJECT_ID)
        }
        admissions = self._read(
            "ADMISSIONS.csv", parse_dates=["ADMITTIME", "DISCHTIME", "DEATHTIME"]
        ).dropna(subset=["SUBJECT_ID", "HADM_ID"])

        out: dict[tuple[int, int], dict[str, str]] = {}
        for row in admissions.itertuples(index=False):
            subject_id = int(row.SUBJECT_ID)
            hadm_id = int(row.HADM_ID)
            out[(subject_id, hadm_id)] = {
                "Sex": sex_by_subject.get(subject_id, ""),
                "Religion": _text(row.RELIGION),
                "Marital_Status": _text(row.MARITAL_STATUS),
                "Ethnicity": _text(row.ETHNICITY),
                "Insurance": _text(row.INSURANCE),
                "Admission_Type": _text(row.ADMISSION_TYPE),
            }
        return out


def _text(value: object) -> str:
    return "" if value is None or pd.isna(value) else str(value)


def _to_datetime(value: object) -> datetime:
    return pd.Timestamp(value).to_pydatetime()


def _full_years(dob: object, admittime: object) -> int:
    """``DATE_PART('year', AGE(admittime, dob))`` -- whole years elapsed.

    MIMIC-III shifts the date of birth of patients over 89 so the computed age
    lands near 300. That is deliberate de-identification and is left alone:
    criterion E5 excludes ages at or above 90, so the shifted cohort is excluded
    by the same rule that excludes any other over-89 patient, rather than by a
    special case here.
    """
    birth = pd.Timestamp(dob)
    admit = pd.Timestamp(admittime)
    years = admit.year - birth.year
    if (admit.month, admit.day) < (birth.month, birth.day):
        years -= 1
    return years


__all__ = [
    "DISCHARGE_SUMMARY",
    "REQUIRED_FILES",
    "MimicCsvError",
    "MimicCsvSource",
]
