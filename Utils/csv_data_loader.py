import logging
import pandas as pd
from pathlib import Path
from Utils.markdown_gtmf import save_gtmf_markdown
import os
from meddial.cohort import (
    classify_lower_acuity_candidate,
    create_cohort_manifest,
    deterministic_sample,
    load_manifest_selection,
    save_cohort_manifest,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CSVDataLoader:
    def __init__(self, csv_dir: str):
        self.csv_dir = Path(csv_dir)
        if not self.csv_dir.exists():
            raise ValueError(f"CSV directory not found: {csv_dir}")

        self._noteevents = None
        self._patients = None
        self._admissions = None

    @property
    def noteevents(self):
        if self._noteevents is None:
            csv_path = self.csv_dir / "NOTEEVENTS.csv"
            self._noteevents = pd.read_csv(csv_path, low_memory=False)
        return self._noteevents

    @property
    def patients(self):
        if self._patients is None:
            csv_path = self.csv_dir / "PATIENTS.csv"
            self._patients = pd.read_csv(csv_path)
        return self._patients

    @property
    def admissions(self):
        if self._admissions is None:
            csv_path = self.csv_dir / "ADMISSIONS.csv"
            self._admissions = pd.read_csv(csv_path)
        return self._admissions

    def fetch_notes(self, category_filter="Discharge summary", text_filter="Chief Complaint", limit=100):
        notes = self.noteevents.copy()

        if category_filter:
            notes = notes[notes['CATEGORY'].str.contains(category_filter, case=False, na=False)]

        if text_filter:
            notes = notes[notes['TEXT'].str.contains(text_filter, case=False, na=False)]

        # Stable ordering makes the candidate pool reproducible across pandas versions.
        notes = notes.sort_values(["SUBJECT_ID", "HADM_ID", "ROW_ID"], kind="stable").head(limit)

        results = []
        for _, note_row in notes.iterrows():
            subject_id = note_row['SUBJECT_ID']
            hadm_id = note_row['HADM_ID']

            patient_row = self.patients[self.patients['SUBJECT_ID'] == subject_id]
            if patient_row.empty:
                continue
            patient_row = patient_row.iloc[0]

            admission_row = self.admissions[
                (self.admissions['SUBJECT_ID'] == subject_id) &
                (self.admissions['HADM_ID'] == hadm_id)
            ]
            if admission_row.empty:
                continue
            admission_row = admission_row.iloc[0]

            result = {
                'row_id': note_row.get('ROW_ID'),
                'subject_id': int(subject_id),
                'hadm_id': int(hadm_id),
                'text': note_row.get('TEXT', ''),
                'category': note_row.get('CATEGORY', ''),
                'gender': patient_row.get('GENDER', ''),
                'dob': patient_row.get('DOB', ''),
                'admittime': admission_row.get('ADMITTIME', ''),
                'dischtime': admission_row.get('DISCHTIME', ''),
                'religion': admission_row.get('RELIGION', ''),
                'marital_status': admission_row.get('MARITAL_STATUS', ''),
                'ethnicity': admission_row.get('ETHNICITY', ''),
                'insurance': admission_row.get('INSURANCE', ''),
                'admission_type': admission_row.get('ADMISSION_TYPE', '')
            }

            results.append(result)

        return results

    def fetch_note_by_ids(self, subject_id, hadm_id):
        notes = self.noteevents

        matching_notes = notes[
            (notes['SUBJECT_ID'] == subject_id) &
            (notes['HADM_ID'] == hadm_id)
        ]

        if matching_notes.empty:
            logger.warning(f"No notes found for subject_id={subject_id}, hadm_id={hadm_id}")
            return ""

        discharge_summaries = matching_notes[
            matching_notes['CATEGORY'].str.contains('Discharge summary', case=False, na=False)
        ]

        if not discharge_summaries.empty:
            return discharge_summaries.iloc[0]['TEXT']

        return matching_notes.iloc[0]['TEXT']

    def fetch_notes_with_light_case_filter(
        self,
        category_filter: str = "Discharge summary",
        limit: int = 100,
        light_case_include_terms: list[str] = None,
        light_case_exclude_terms: list[str] = None,
        seed: int = 42,
        manifest_path: str | None = None,
        reuse_manifest: bool = False,
    ) -> list[dict]:
        # Term arguments are retained for source compatibility. The versioned,
        # audited policy in meddial.cohort is authoritative.
        del light_case_include_terms, light_case_exclude_terms

        all_notes = self.fetch_notes(
            category_filter=category_filter,
            text_filter="",
            limit=limit * 10
        )

        light_case_notes = []

        for note in all_notes:
            filter_result = classify_lower_acuity_candidate(note['text']).to_dict()
            note['light_case_filter'] = filter_result
            if filter_result['passed']:
                light_case_notes.append(note)

        if manifest_path and reuse_manifest and Path(manifest_path).exists():
            selected = load_manifest_selection(manifest_path, light_case_notes)
        else:
            selected = deterministic_sample(light_case_notes, limit=limit, seed=seed)
            if manifest_path:
                manifest = create_cohort_manifest(
                    selected, seed=seed, source=str(self.csv_dir)
                )
                save_cohort_manifest(manifest_path, manifest)

        if len(selected) < limit:
            logger.warning(f"Only {len(selected)} eligible notes available, requested {limit}")

        # This method now returns eligible notes only; rejected notes are never appended.
        return selected


def csv_to_gtmf_workflow(csv_dir: str, output_path: str, limit: int = 50):
    from gtmf_creation import AzureAIClient, process_notes
    import json

    loader = CSVDataLoader(csv_dir)

    notes = loader.fetch_notes_with_light_case_filter(
        category_filter="Discharge summary",
        limit=limit
    )

    if not notes:
        logger.error("No light case notes found")
        return {}

    azure_client = AzureAIClient()

    output_dir = os.path.dirname(output_path) if os.path.dirname(output_path) else 'gtmf'
    quality_summary = process_notes(notes, azure_client, output_dir)

    summary_path = os.path.join(output_dir, 'processing_summary.json')
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(quality_summary, f, indent=2)

    return quality_summary
