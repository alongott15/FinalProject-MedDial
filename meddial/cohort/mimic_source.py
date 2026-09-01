"""One MIMIC-III extract, whatever it is stored in.

``configs/cohort/criteria_v1.sql`` is the authoritative candidate query. This
module is the runnable reproduction of it, and the mapping is written out here
so the two can be diffed by eye:

===========================  ==============================================
``criteria_v1.sql``          here
===========================  ==============================================
``discharge_notes`` CTE      :meth:`MimicSource._discharge_notes`
``icu_flags`` CTE            :meth:`MimicSource._icu_days`
``procedure_codes`` CTE      :meth:`MimicSource._codes_by_hadm`
``diagnosis_codes`` CTE      :meth:`MimicSource._codes_by_hadm`
final ``SELECT``             :meth:`MimicSource.admission_records`
===========================  ==============================================

The extract reaches this project two ways -- the CSV files as PhysioNet ships
them, and the ``physionet-data`` copy on BigQuery -- and a cohort built from
one must be the cohort built from the other. So a subclass supplies only
*tables*: :meth:`MimicSource.table` and :meth:`MimicSource.iter_table` hand
back MIMIC's columns under MIMIC's names, and every decision made from them
lives here, once. A storage backend cannot quietly select differently, because
it cannot select at all.

No clinical eligibility decision is made here either. This module only
assembles the structured facts;
:func:`~meddial.cohort.criteria.evaluate_admission` decides. Note text is
carried because E9 measures its length and category, not its vocabulary.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from meddial.cohort.criteria import AdmissionRecord, normalise_icd9

TABLES: tuple[str, ...] = (
    "ADMISSIONS",
    "PATIENTS",
    "NOTEEVENTS",
    "ICUSTAYS",
    "PROCEDURES_ICD",
    "DIAGNOSES_ICD",
)
"""The tables the candidate query reads, in the order a snapshot hashes them."""

COLUMNS: dict[str, tuple[str, ...]] = {
    "ADMISSIONS": (
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
    "PATIENTS": ("SUBJECT_ID", "DOB", "GENDER"),
    "NOTEEVENTS": ("ROW_ID", "SUBJECT_ID", "HADM_ID", "CHARTDATE", "CATEGORY", "TEXT"),
    "ICUSTAYS": ("HADM_ID", "LOS"),
    "PROCEDURES_ICD": ("HADM_ID", "ICD9_CODE"),
    "DIAGNOSES_ICD": ("HADM_ID", "ICD9_CODE"),
}
"""Exactly the columns the query needs. Nothing else is read, by any backend."""

NOTE_CHUNK_ROWS = 50_000
"""NOTEEVENTS is several GB; it is streamed rather than loaded whole."""

DISCHARGE_SUMMARY = "discharge summary"

NOTE_SEPARATOR = "\n\n"
"""Joins an admission's discharge summary to its addenda, in filing order."""


@dataclass(frozen=True)
class _Note:
    row_id: int
    category: str
    text: str


class MimicSource(ABC):
    """A MIMIC-III extract, read into cohort candidates.

    Subclasses implement the three storage-facing methods below. Everything
    else is the SQL, and is shared.
    """

    # -- what a subclass supplies -----------------------------------------

    @abstractmethod
    def snapshot_hash(self) -> str:
        """A digest identifying the exact extract these records came from.

        The cohort is only reproducible if the extract it came from is
        identified, so the manifest records this and ``meddial-scr`` refuses to
        extract against a source that hashes differently. Each backend prefixes
        its own scheme: a hash over CSV bytes and a hash over a BigQuery
        snapshot describe different things, and must never compare equal by
        accident.
        """

    @abstractmethod
    def table(self, name: str, *, parse_dates: Sequence[str] = ()) -> pd.DataFrame:
        """One whole MIMIC table, restricted to ``COLUMNS[name]``.

        Columns carry MIMIC's own uppercase names whatever the backend calls
        them, and the columns named in ``parse_dates`` arrive as datetimes.
        """

    @abstractmethod
    def iter_table(
        self,
        name: str,
        *,
        parse_dates: Sequence[str] = (),
        chunk_rows: int = NOTE_CHUNK_ROWS,
    ) -> Iterator[pd.DataFrame]:
        """The same, streamed, so peak memory does not follow NOTEEVENTS."""

    # -- the SQL's CTEs ----------------------------------------------------

    def _icu_days(self) -> dict[int, float]:
        """``icu_flags``, but graded: total ICU days per admission (E1).

        An admission can have several ICU stays; their sum is the duration the
        criterion bounds. A stay whose LOS the source left null (10 rows in
        v1.4) becomes ``inf``: an unknown duration cannot be shown to be under
        the threshold, so it fails E1 rather than slipping under it.
        """
        icu = self.table("ICUSTAYS").dropna(subset=["HADM_ID"])
        totals: dict[int, float] = {}
        for hadm_id, los in zip(icu["HADM_ID"], icu["LOS"], strict=False):
            key = int(hadm_id)
            days = math.inf if pd.isna(los) else float(los)
            previous = totals.get(key, 0.0)
            totals[key] = math.inf if math.isinf(days) or math.isinf(previous) else previous + days
        return totals

    def _codes_by_hadm(self, name: str) -> dict[int, tuple[str, ...]]:
        """``procedure_codes`` / ``diagnosis_codes``: distinct normalised codes."""
        frame = self.table(name).dropna(subset=["HADM_ID", "ICD9_CODE"])
        grouped: dict[int, set[str]] = {}
        for hadm_id, code in zip(frame["HADM_ID"], frame["ICD9_CODE"], strict=False):
            grouped.setdefault(int(hadm_id), set()).add(normalise_icd9(code))
        return {hadm_id: tuple(sorted(codes)) for hadm_id, codes in grouped.items()}

    def _discharge_notes(self) -> dict[int, _Note]:
        """The admission's whole discharge documentation, in filing order.

        MIMIC files addenda as further rows of category "Discharge summary",
        dated *after* the summary they amend and typically a few hundred
        characters long. Taking rank 1 by ``chartdate DESC, row_id DESC`` --
        which is what the SQL does and what this method used to do -- therefore
        selects an addendum rather than the note it amends whenever one exists.
        Measured on a 1,000-case cohort: 65 admissions (6.5%) took a note that
        was not the longest available, losing a median of 6,132 characters, and
        43 of those read a note under 2,000 characters while one over 5,000
        sat beside it. One such case extracted a single entity from a 547-byte
        addendum whose 9,024-byte summary was never seen.

        The addenda are not noise -- one of them carries the DISCHARGE
        DIAGNOSES -- so the fix is to concatenate rather than to pick better.
        All discharge summaries for the admission are joined in ascending
        (chartdate, row_id) order, which is filing order, and that text is both
        what E9 measures and what extraction reads. ``row_id`` reports the
        longest constituent note, the one a reader would call the summary.

        Notes are streamed in chunks, so peak memory stays independent of the
        size of NOTEEVENTS. The category test runs here even for a backend that
        already pushed it down into its own query: testing twice costs nothing,
        and it keeps the definition of "discharge summary" in one place rather
        than one place per backend.
        """
        parts: dict[int, list[tuple[tuple[int, int, int], int, str, str]]] = {}
        for chunk in self.iter_table("NOTEEVENTS", parse_dates=["CHARTDATE"]):
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
                parts.setdefault(hadm_id, []).append(
                    (
                        (dated, stamp, row_id),
                        row_id,
                        "" if pd.isna(row.CATEGORY) else str(row.CATEGORY),
                        "" if pd.isna(row.TEXT) else str(row.TEXT),
                    )
                )

        notes: dict[int, _Note] = {}
        for hadm_id, rows in parts.items():
            rows.sort(key=lambda item: item[0])
            primary = max(rows, key=lambda item: len(item[3]))
            notes[hadm_id] = _Note(
                row_id=primary[1],
                category=primary[2],
                text=NOTE_SEPARATOR.join(text for _, _, _, text in rows if text),
            )
        return notes

    # -- the final SELECT --------------------------------------------------

    def admission_records(self) -> Iterator[AdmissionRecord]:
        """Yield every admission as a candidate, eligible or not.

        The SQL deliberately returns excluded admissions too, because the
        exclusion counts are themselves a reported result. Filtering here would
        destroy the flow diagram before it could be produced.
        """
        icu_days = self._icu_days()
        procedures = self._codes_by_hadm("PROCEDURES_ICD")
        diagnoses = self._codes_by_hadm("DIAGNOSES_ICD")
        notes = self._discharge_notes()

        patients = self.table("PATIENTS", parse_dates=["DOB"])
        dob_by_subject = {
            int(row.SUBJECT_ID): row.DOB
            for row in patients.itertuples(index=False)
            if not pd.isna(row.SUBJECT_ID)
        }

        admissions = self.table(
            "ADMISSIONS", parse_dates=["ADMITTIME", "DISCHTIME", "DEATHTIME"]
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
                has_icu_stay=hadm_id in icu_days,
                hospital_expire_flag=bool(int(row.HOSPITAL_EXPIRE_FLAG or 0)),
                procedure_icd9_codes=procedures.get(hadm_id, ()),
                diagnosis_icd9_codes=diagnoses.get(hadm_id, ()),
                note_text=note.text if note else "",
                note_category=note.category if note else "",
                deathtime=None if pd.isna(row.DEATHTIME) else _to_datetime(row.DEATHTIME),
                row_id=note.row_id if note else None,
                icu_days=icu_days.get(hadm_id, 0.0),
            )

    def demographics(self) -> dict[tuple[int, int], dict[str, str]]:
        """Presentational demographics per admission, keyed by (subject, hadm).

        Deliberately excludes ``DOB``. The reference records an age, which is
        what the disclosure policies address; a date of birth adds nothing a
        policy can act on and is a re-identification vector in a document that
        outlives the extract. ``AdmissionRecord.age_years`` already carries the
        only form of it this project needs.
        """
        patients = self.table("PATIENTS", parse_dates=["DOB"])
        sex_by_subject = {
            int(row.SUBJECT_ID): _text(row.GENDER)
            for row in patients.itertuples(index=False)
            if not pd.isna(row.SUBJECT_ID)
        }
        admissions = self.table(
            "ADMISSIONS", parse_dates=["ADMITTIME", "DISCHTIME", "DEATHTIME"]
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
    "COLUMNS",
    "DISCHARGE_SUMMARY",
    "NOTE_CHUNK_ROWS",
    "NOTE_SEPARATOR",
    "TABLES",
    "MimicSource",
]
