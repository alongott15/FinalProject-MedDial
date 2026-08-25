import json
import logging
import copy
from typing import Any, Mapping
from Models.classes import EvidenceProvenance, GTMF, SCR
from Utils.utils import format_date, calculate_age
from Utils.bias_aware_prompts import GTMF_CREATION_PROMPT
from Utils.markdown_gtmf import save_gtmf_markdown
import re
import os
from meddial.cohort import (
    EXCLUDE_PATTERNS,
    INCLUDE_PATTERNS,
    classify_lower_acuity_candidate,
)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LIGHT_CASE_INCLUDE_TERMS = list(INCLUDE_PATTERNS)
LIGHT_CASE_EXCLUDE_TERMS = list(EXCLUDE_PATTERNS)


class ClinicalReferenceExtractionError(RuntimeError):
    """The reference could not be extracted or validated; it is not an empty SCR."""


def is_light_common_case(note_text: str, chief_complaint: str = "") -> dict:
    """Compatibility wrapper for the versioned lower-acuity lexical filter."""
    return classify_lower_acuity_candidate(note_text, chief_complaint).to_dict()

def get_existing_gtmf_ids(output_dir: str = 'gtmf') -> set:
    """
    Get set of (subject_id, hadm_id) tuples for existing GTMFs.

    Returns:
        Set of tuples (subject_id, hadm_id) that already exist
    """
    existing_ids = set()

    if not os.path.exists(output_dir):
        return existing_ids

    for filename in os.listdir(output_dir):
        if filename.startswith('gtmf_') and filename.endswith('.md'):
            # Parse filename: gtmf_10145_135661.md
            parts = filename.replace('gtmf_', '').replace('.md', '').split('_')
            if len(parts) == 2:
                try:
                    subject_id = int(parts[0])
                    hadm_id = int(parts[1])
                    existing_ids.add((subject_id, hadm_id))
                except ValueError:
                    continue

    logger.info(f"Found {len(existing_ids)} existing GTMF profiles")
    return existing_ids


def aggressive_json_clean(text: str) -> str:
    text = re.sub(r'```[a-z]*\n?', '', text)
    text = re.sub(r'```', '', text)

    prefixes = ['Here is the JSON:', 'JSON:', 'Response:', 'Output:', 'Result:']
    for prefix in prefixes:
        if text.strip().startswith(prefix):
            text = text.strip()[len(prefix):].strip()

    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\t+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r',(\s*[}\]])', r'\1', text)

    return text.strip()

def safe_json_parse_object(json_str: str, field_name: str = "") -> dict:
    if not json_str or json_str.strip() in ['', '{}']:
        raise ClinicalReferenceExtractionError(
            f"{field_name or 'extraction'} returned empty JSON"
        )

    try:
        result = json.loads(json_str)
        if isinstance(result, dict):
            return result
        raise ClinicalReferenceExtractionError(
            f"{field_name or 'extraction'} was JSON but not an object"
        )
    except json.JSONDecodeError:
        try:
            cleaned = json_str.strip()
            if not cleaned.startswith('{'):
                cleaned = '{' + cleaned
            if not cleaned.endswith('}'):
                cleaned = cleaned + '}'
            cleaned = re.sub(r'(?<!\\)"(?=[^",\]\}]*")', '\\"', cleaned)
            cleaned = re.sub(r',(\s*[}\]])', r'\1', cleaned)
            result = json.loads(cleaned)
            if isinstance(result, dict):
                return result
        except Exception as exc:
            raise ClinicalReferenceExtractionError(
                f"Could not parse {field_name or 'extraction'} JSON: {exc}"
            ) from exc
        raise ClinicalReferenceExtractionError(
            f"Could not parse {field_name or 'extraction'} JSON"
        )

class AzureAIClient:
    def __init__(self, endpoint: str = None, api_key: str = None, model_name: str = "gpt-4.1"):
        self.endpoint = endpoint or os.getenv("AZURE_AI_ENDPOINT")
        self.api_key = api_key or os.getenv("AZURE_AI_API_KEY")
        self.model_name = model_name

        if not self.endpoint or not self.api_key:
            raise ValueError("Azure AI endpoint and API key must be provided")

        try:
            from azure.ai.inference import ChatCompletionsClient
            from azure.core.credentials import AzureKeyCredential
        except ImportError as exc:
            raise RuntimeError("Install MedDial with the 'azure' extra") from exc
        self.client = ChatCompletionsClient(
            endpoint=self.endpoint,
            credential=AzureKeyCredential(self.api_key)
        )

    def chat_completion(self, system_message: str, user_message: str, temperature: float = 0.0) -> str:
        try:
            from azure.ai.inference.models import SystemMessage, UserMessage
            response = self.client.complete(
                messages=[
                    SystemMessage(content=system_message),
                    UserMessage(content=user_message)
                ],
                model=self.model_name,
                temperature=temperature,
                max_tokens=2048
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"Azure AI completion failed: {e}")
            raise

def chunk_medical_text(text: str, max_chunk_size: int = 3000, overlap: int = 200) -> list[str]:
    if len(text) <= max_chunk_size:
        return [text]

    chunks = []
    start = 0

    while start < len(text):
        end = start + max_chunk_size

        if end < len(text):
            last_period = text.rfind('.', end - 200, end)
            last_newline = text.rfind('\n', end - 200, end)

            if last_period > start:
                end = last_period + 1
            elif last_newline > start:
                end = last_newline + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        start = max(start + max_chunk_size - overlap, end)

        if start >= len(text):
            break

    return chunks

def _annotate_chunk_evidence(
    extraction: dict[str, Any],
    chunk: str,
    chunk_index: int,
    model: str,
    source_note_id: str,
    character_start: int | None,
) -> dict[str, Any]:
    """Attach source provenance to clinical entities extracted from one chunk."""
    annotated = copy.deepcopy(extraction)
    evidence = EvidenceProvenance(
        source_note_id=source_note_id,
        chunk_index=chunk_index,
        character_start=character_start,
        character_end=(character_start + len(chunk) if character_start is not None else None),
        excerpt=chunk[:500],
        extractor="llm_structured_extraction",
        model=model,
    ).model_dump()
    core = annotated.setdefault("Core_Fields", {})
    context = annotated.setdefault("Context_Fields", {})
    for key in ("Symptoms", "Diagnoses", "Treatment_Options"):
        for entity in core.setdefault(key, []):
            if isinstance(entity, dict):
                entity.setdefault("evidence", []).append(evidence)
                for medication in entity.get("medications", []):
                    if isinstance(medication, dict):
                        medication.setdefault("evidence", []).append(evidence)
    for key in ("Current_Medications", "Discharge_Medications"):
        for entity in context.setdefault(key, []):
            if isinstance(entity, dict):
                entity.setdefault("evidence", []).append(evidence)
    history = context.setdefault("Medical_History", {})
    if isinstance(history, dict) and history.get("Past_Medical_History") not in (None, "", "not provided"):
        history.setdefault("evidence", []).append(evidence)
    additional = annotated.setdefault("Additional_Context", {})
    if isinstance(additional, dict) and additional.get("Chief_Complaint") not in (None, "", "not provided"):
        additional.setdefault("evidence", []).append(evidence)
    annotated.setdefault("reference_evidence", []).append(evidence)
    return annotated


def extract_scr_chunked(
    medical_text: str,
    azure_client: AzureAIClient,
    source_note_id: str = "current_note",
) -> SCR:
    if not medical_text or not medical_text.strip():
        raise ClinicalReferenceExtractionError("Cannot extract an SCR from empty note text")
    schema_json = SCR.model_json_schema()
    chunks = chunk_medical_text(medical_text, max_chunk_size=3000, overlap=200)

    system_message = GTMF_CREATION_PROMPT + """

    CRITICAL: Output ONLY valid JSON - no explanations, no markdown, no code blocks.
    Always start your response directly with the opening brace { and end with closing brace }"""

    all_extractions = []

    for i, chunk in enumerate(chunks):
        character_start = medical_text.find(chunk)
        user_message = f"""
        Extract medical information from this clinical note chunk and format it according to the JSON schema below.

        IMPORTANT: Respond with ONLY the JSON object, no other text.

        JSON Schema:
        {json.dumps(schema_json, indent=2)}

        Medical Note Chunk:
        {chunk}

        JSON Output:
        """

        try:
            result = azure_client.chat_completion(system_message, user_message, temperature=0.0)
            cleaned_result = aggressive_json_clean(result)

            json_start = -1
            json_end = -1
            brace_count = 0

            for idx, char in enumerate(cleaned_result):
                if char == '{':
                    if json_start == -1:
                        json_start = idx
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0 and json_start != -1:
                        json_end = idx + 1
                        break

            if json_start >= 0 and json_end > json_start:
                json_str = cleaned_result[json_start:json_end]
                data = safe_json_parse_object(json_str, f"chunk_{i+1}")

                if data and data != {}:
                    all_extractions.append(
                        _annotate_chunk_evidence(
                            data,
                            chunk=chunk,
                            chunk_index=i,
                            model=getattr(azure_client, "model_name", "unknown"),
                            source_note_id=source_note_id,
                            character_start=character_start if character_start >= 0 else None,
                        )
                    )

        except Exception as e:
            logger.error(f"Error processing chunk {i+1}: {e}")
            continue

    if not all_extractions:
        raise ClinicalReferenceExtractionError(
            f"No valid chunk extractions obtained from {len(chunks)} chunk(s)"
        )

    merged_extraction = merge_scr_extractions(all_extractions)

    try:
        return SCR(**merged_extraction)
    except Exception as e:
        raise ClinicalReferenceExtractionError(f"Merged SCR validation failed: {e}") from e


_EMPTY_VALUES = (None, "", "not provided", "Not provided", 0)


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value).strip().lower())


def _merge_unique_scalars(values: list[Any]) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = _normalized(value)
        if key and key not in seen:
            seen.add(key)
            merged.append(copy.deepcopy(value))
    return merged


def _entity_key(entity: Any, keys: tuple[str, ...]) -> str:
    if not isinstance(entity, Mapping):
        return _normalized(entity)
    parts = [_normalized(entity.get(key, "")) for key in keys]
    populated = [part for part in parts if part and part != "not provided"]
    return "|".join(populated) or _normalized(json.dumps(entity, sort_keys=True, default=str))


def _deep_merge(first: Any, second: Any) -> Any:
    if first in _EMPTY_VALUES and second not in _EMPTY_VALUES:
        return copy.deepcopy(second)
    if isinstance(first, Mapping) and isinstance(second, Mapping):
        merged = copy.deepcopy(dict(first))
        for key, value in second.items():
            merged[key] = _deep_merge(merged[key], value) if key in merged else copy.deepcopy(value)
        return merged
    if isinstance(first, list) and isinstance(second, list):
        return _merge_unique_scalars(first + second)
    return copy.deepcopy(first)


def _merge_entity_lists(
    lists: list[list[Any]], key_fields: tuple[str, ...]
) -> list[Any]:
    merged: dict[str, Any] = {}
    order: list[str] = []
    for entities in lists:
        for entity in entities:
            key = _entity_key(entity, key_fields)
            if not key:
                continue
            if key not in merged:
                merged[key] = copy.deepcopy(entity)
                order.append(key)
            else:
                merged[key] = _deep_merge(merged[key], entity)
    return [merged[key] for key in order]


def merge_scr_extractions(extractions: list[dict]) -> dict:
    """Merge every SCR field across chunks with entity-aware de-duplication."""
    if not extractions:
        raise ClinicalReferenceExtractionError("No SCR extractions to merge")
    merged: dict[str, Any] = {}
    for extraction in extractions:
        merged = _deep_merge(merged, extraction)

    core_sections = [item.get("Core_Fields", {}) for item in extractions]
    context_sections = [item.get("Context_Fields", {}) for item in extractions]
    merged_core = merged.setdefault("Core_Fields", {})
    merged_context = merged.setdefault("Context_Fields", {})
    merged_core["Symptoms"] = _merge_entity_lists(
        [section.get("Symptoms", []) for section in core_sections], ("description",)
    )
    merged_core["Diagnoses"] = _merge_entity_lists(
        [section.get("Diagnoses", []) for section in core_sections], ("primary",)
    )
    merged_core["Treatment_Options"] = _merge_entity_lists(
        [section.get("Treatment_Options", []) for section in core_sections],
        ("procedure", "treatment"),
    )
    merged_context["Allergies"] = _merge_unique_scalars(
        [value for section in context_sections for value in section.get("Allergies", [])]
    )
    for key in ("Current_Medications", "Discharge_Medications"):
        merged_context[key] = _merge_entity_lists(
            [section.get(key, []) for section in context_sections], ("name",)
        )
    for key, fields in (
        ("structured_diagnoses", ("icd9_code", "description")),
        ("structured_procedures", ("icd9_code", "description")),
        ("structured_prescriptions", ("drug", "dose_val_rx")),
    ):
        merged[key] = _merge_entity_lists(
            [item.get(key, []) for item in extractions], fields
        )
    merged["reference_evidence"] = _merge_entity_lists(
        [item.get("reference_evidence", []) for item in extractions],
        ("source_note_id", "chunk_index", "extractor"),
    )
    merged["schema_name"] = "Structured Clinical Reference"
    merged["extraction_status"] = "VALID"
    return merged


# Backward-compatible function names.
extract_gtmf_chunked = extract_scr_chunked
merge_gtmf_extractions = merge_scr_extractions

def process_notes(results, azure_client: AzureAIClient, output_dir: str = 'gtmf'):
    os.makedirs(output_dir, exist_ok=True)

    # CRITICAL: Load existing GTMFs to avoid regeneration
    existing_ids = get_existing_gtmf_ids(output_dir)
    logger.info(f"Skipping {len(existing_ids)} existing profiles")

    quality_summary = {
        "total_processed": 0,
        "skipped_existing": 0,
        "json_parse_failures": 0,
        "light_case_passed": 0,
        "light_case_failed": 0,
        "gtmfs_created": 0,
        "scr_extraction_failures": 0,
    }

    for idx, row in enumerate(results):
        try:
            # Progress tracking
            if idx % 50 == 0:
                logger.info(f"Progress: {idx}/{len(results)} notes processed")
                logger.info(f"  GTMFs created so far: {quality_summary['gtmfs_created']}")
                logger.info(f"  Skipped existing: {quality_summary['skipped_existing']}")

            # CRITICAL: Skip if already exists
            subject_id = row['subject_id']
            hadm_id = row['hadm_id']

            if (subject_id, hadm_id) in existing_ids:
                logger.info(f"  Skipping existing profile: {subject_id}_{hadm_id}")
                quality_summary["skipped_existing"] += 1
                continue

            light_case_result = is_light_common_case(row['text'])
            if not light_case_result['passed']:
                quality_summary["light_case_failed"] += 1
                continue
            else:
                quality_summary["light_case_passed"] += 1

            dob_formatted = format_date(row['dob'], '%Y-%m-%d')
            adm_formatted = format_date(row['admittime'], '%Y-%m-%d %H:%M:%S')
            dis_formatted = format_date(row['dischtime'], '%Y-%m-%d %H:%M:%S')
            age = calculate_age(dob_formatted, adm_formatted)

            demographics = {
                'Date_of_Birth': dob_formatted,
                'Age': age,
                'Sex': row.get('gender', 'Not provided'),
                'Religion': row.get('religion', 'Not provided'),
                'Marital_Status': row.get('marital_status', 'Not provided'),
                'Ethnicity': row.get('ethnicity', 'Not provided'),
                'Insurance': row.get('insurance', 'Not provided'),
                'Admission_Type': row.get('admission_type', 'Not provided'),
                'Admission_Date': adm_formatted,
                'Discharge_Date': dis_formatted
            }

            gtmf_instance = extract_scr_chunked(
                row['text'],
                azure_client,
                source_note_id=f"{row['subject_id']}_{row['hadm_id']}",
            )
            quality_summary["total_processed"] += 1

            gtmf_instance = gtmf_instance.model_copy(update={
                "row_id": row['row_id'],
                "subject_id": row['subject_id'],
                "hadm_id": row['hadm_id'],
                "Context_Fields": gtmf_instance.Context_Fields.model_copy(update={
                    "Patient_Demographics": gtmf_instance.Context_Fields.Patient_Demographics.model_copy(update=demographics)
                })
            })

            result = gtmf_instance.model_dump()
            result["light_case_filter"] = light_case_result
            result["case_type"] = "LOWER_ACUITY_LEXICAL_CANDIDATE"

            subject_id = row['subject_id']
            hadm_id = row['hadm_id']
            filename = f"gtmf_{subject_id}_{hadm_id}.md"
            output_path = os.path.join(output_dir, filename)
            save_gtmf_markdown(result, output_path)

            quality_summary["gtmfs_created"] += 1

        except ClinicalReferenceExtractionError as e:
            logger.error(f"SCR extraction failed for note at index {idx}: {e}")
            quality_summary["scr_extraction_failures"] += 1
            quality_summary["total_processed"] += 1
        except json.JSONDecodeError as e:
            logger.error(f"JSON parsing failed for note at index {idx}: {e}")
            quality_summary["json_parse_failures"] += 1
            quality_summary["total_processed"] += 1
        except Exception as e:
            logger.error(f"Error processing note at index {idx}: {e}")
            quality_summary["total_processed"] += 1

    return quality_summary

def main():
    try:
        azure_client = AzureAIClient()
    except Exception as e:
        logger.error(f"Failed to initialize Azure AI client: {e}")
        return

    csv_dir = os.getenv("MIMIC_CSV_DIR")

    if not csv_dir:
        logger.error("MIMIC_CSV_DIR environment variable not set")
        return

    if not os.path.exists(csv_dir):
        logger.error(f"CSV directory not found: {csv_dir}")
        return

    try:
        from Utils.csv_data_loader import CSVDataLoader
        loader = CSVDataLoader(csv_dir)
        # Fetch more notes to reach target after skipping existing (95 existing + ~205 new = 300 total)
        # Selection is deterministic and recorded in a manifest. The filter
        # identifies lexical candidates; it does not establish primary-care severity.
        results = loader.fetch_notes_with_light_case_filter(
            category_filter="Discharge summary",
            limit=800,
            seed=42,
            manifest_path="gtmf/cohort_manifest.json",
        )
        if not results:
            logger.error("No eligible lower-acuity lexical candidates found")
            return
    except Exception as e:
        logger.error(f"Error loading CSV data: {e}")
        return

    try:
        output_dir = 'gtmf'
        summary = process_notes(results, azure_client, output_dir)

        summary_path = os.path.join(output_dir, 'processing_summary.json')
        with open(summary_path, 'w', encoding='utf-8') as outfile:
            json.dump(summary, outfile, indent=2)

        print(f"\n=== SCR Processing Summary ===")
        print(f"Total processed: {summary['total_processed']}")
        print(f"Skipped existing: {summary['skipped_existing']}")
        print(f"SCRs created (NEW): {summary['gtmfs_created']}")
        print(f"Lexical candidates passed: {summary['light_case_passed']}")
        print(f"Lexical candidates rejected: {summary['light_case_failed']}")
        print(f"JSON parse failures: {summary['json_parse_failures']}")

    except Exception as e:
        logger.error(f"Error in main execution: {e}")

if __name__ == '__main__':
    main()
