import json
import logging
import os
import re

from meddial.knowledge import GTMF, Demographics
from meddial.llm import (
    DataClassification,
    LLMProvider,
    LocalOpenAICompatibleProvider,
    ProviderConfigurationError,
    resolve_ollama_digest,
    to_chat_messages,
)
from Utils.bias_aware_prompts import GTMF_CREATION_PROMPT
from Utils.markdown_gtmf import save_gtmf_markdown
from Utils.utils import calculate_age, format_date

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LIGHT_CASE_INCLUDE_TERMS = [
    "cough", "sore throat", "throat pain", "runny nose", "nasal congestion",
    "upper respiratory", "cold symptoms", "flu-like", "sneezing", "stuffy nose",
    "post-nasal drip", "scratchy throat", "hoarse voice", "mild shortness of breath",
    "headache", "mild dizziness", "sinus pressure", "sinus pain", "earache",
    "ear pain", "pressure in head", "tension headache", "migraine",
    "fever", "low-grade fever", "low grade fever", "mild fever", "chills", "malaise",
    "fatigue", "tiredness", "weakness", "body aches", "muscle aches",
    "nausea", "upset stomach", "mild abdominal pain", "diarrhea", "constipation",
    "heartburn", "indigestion", "loss of appetite",
    "back pain", "neck pain", "joint pain", "minor pain", "muscle soreness",
    "stiffness", "sprain", "strain", "pain", "discomfort",
    "mild swelling", "inflammation", "redness",
    "rash", "skin irritation", "itching", "minor wound", "bruise",
    "not feeling well", "under the weather", "viral illness", "viral infection",
    "common cold", "seasonal allergies", "allergy symptoms"
]

LIGHT_CASE_EXCLUDE_TERMS = [
    "icu", "intubated", "cardiac arrest", "shock", "sepsis", "septic",
    "mechanical ventilation", "multi organ failure", "multiorgan failure",
    "malignancy", "cancer", "metastatic", "critical", "life-threatening",
    "severe", "acute respiratory distress", "ards", "transplant",
    "dialysis", "cardiac surgery", "trauma", "hemorrhage", "stroke"
]


def is_light_common_case(note_text: str, chief_complaint: str = "") -> dict:
    text_lower = note_text.lower()
    cc_lower = chief_complaint.lower() if chief_complaint else ""
    combined_text = text_lower + " " + cc_lower

    for term in LIGHT_CASE_EXCLUDE_TERMS:
        if term in combined_text:
            return {"passed": False, "reason": f"Contains severe/ICU indicator: '{term}'"}

    matched_terms = []
    for term in LIGHT_CASE_INCLUDE_TERMS:
        if term in combined_text:
            matched_terms.append(term)

    if matched_terms:
        return {"passed": True, "reason": f"Contains light symptoms: {', '.join(matched_terms)}"}
    else:
        return {"passed": False, "reason": "No light/common symptoms detected"}

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
        return {}

    try:
        result = json.loads(json_str)
        if isinstance(result, dict):
            return result
        return {}
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
        except Exception:
            pass

        return {
            "Core_Fields": {
                "Symptoms": [],
                "Diagnoses": [],
                "Treatment_Options": []
            },
            "Context_Fields": {
                "Patient_Demographics": {
                    "Date_of_Birth": "not provided",
                    "Age": 0,
                    "Sex": "not provided",
                    "Religion": "not provided",
                    "Marital_Status": "not provided",
                    "Ethnicity": "not provided",
                    "Insurance": "not provided",
                    "Admission_Type": "not provided",
                    "Admission_Date": "not provided",
                    "Discharge_Date": "not provided"
                },
                "Medical_History": {"Past_Medical_History": "not provided"},
                "Allergies": [],
                "Current_Medications": [],
                "Discharge_Medications": []
            },
            "Additional_Context": {"Chief_Complaint": "not provided"}
        }

def provider_from_env(prefix: str = "MEDDIAL_GTMF") -> LLMProvider:
    """Build a local provider from ``{prefix}_BASE_URL`` / ``{prefix}_MODEL``.

    Decision D2: extraction reads MIMIC-III discharge summaries, so the only
    approved destination is a model served on this host. Provisional — the W2
    config layer replaces this with a run manifest.
    """
    base_url = os.environ.get(f"{prefix}_BASE_URL", "http://localhost:11434/v1")
    model_id = os.environ.get(f"{prefix}_MODEL")
    if not model_id:
        raise ProviderConfigurationError(
            f"Set {prefix}_MODEL to the model this run should use."
        )
    return LocalOpenAICompatibleProvider(
        base_url,
        model_id,
        model_digest=resolve_ollama_digest(base_url, model_id),
        model_family=os.environ.get(f"{prefix}_FAMILY", model_id.split(":")[0]),
        quantisation=os.environ.get(f"{prefix}_QUANT", "unknown"),
    )

class ExtractionError(RuntimeError):
    """No chunk of a note yielded a parseable extraction.

    Raised rather than returning an empty reference. A note that could not be
    read must not produce a profile that looks like one that was: downstream,
    an empty Structured Clinical Reference is indistinguishable from a genuinely
    sparse case, and every metric computed against it silently counts a
    successful extraction (D-08).
    """


def _repair_evidence_offsets(node, note_id: str, source_note: str) -> int:
    """Recompute evidence offsets by locating each quoted span in the note.

    Models quote accurately and count characters badly. In practice every span
    qwen3.5:9b produced carried the right text and wrong integers, which made
    100% of entities fail EvidenceSpan validation and left the reference
    ungrounded -- GRND-1/2 measure extraction against coded truth, and they
    cannot do that through evidence that never resolves.

    So the quote is treated as the claim and the offsets as a derived value:
    the text is searched for verbatim and the offsets rewritten from the match.
    A quote that does not appear in the note is left exactly as the model
    produced it, so a fabricated citation still fails validation instead of
    being quietly relocated to whatever happens to match.

    Returns the number of spans repaired. Mutates ``node`` in place.
    """
    repaired = 0
    if isinstance(node, list):
        for item in node:
            repaired += _repair_evidence_offsets(item, note_id, source_note)
        return repaired
    if not isinstance(node, dict):
        return repaired

    for key, value in node.items():
        if key == "evidence" and isinstance(value, list):
            for span in value:
                if not isinstance(span, dict):
                    continue
                text = span.get("text")
                if not isinstance(text, str) or not text:
                    continue
                start = source_note.find(text)
                if start < 0:
                    continue
                span["note_id"] = note_id
                span["char_start"] = start
                span["char_end"] = start + len(text)
                repaired += 1
        else:
            repaired += _repair_evidence_offsets(value, note_id, source_note)
    return repaired


def extract_gtmf(
    medical_text: str,
    provider: LLMProvider,
    *,
    note_id: str = "source-note",
    max_tokens: int = 4096,
) -> GTMF:
    """Extract one Structured Clinical Reference from a whole note, in one call.

    The note is not split. Splitting it cost more than it bought: the ~1,200
    token JSON schema had to be resent with every piece, an entity described
    across a boundary was seen whole by neither call, and merging partial
    extractions made the result depend on where the boundary happened to fall.
    A single call sees the note the way a reader does.

    This is affordable because the whole note fits. The longest concatenated
    discharge documentation in the 1,000-case cohort is 27,717 characters,
    roughly 7,000 tokens; with the schema and a 4,096-token answer that is about
    12,500 against a 16,384-token window. A note that does not fit truncates,
    and a truncated answer is unparseable, so it raises :class:`ExtractionError`
    rather than yielding a partial reference.
    """
    schema_json = GTMF.model_json_schema()
    system_message = GTMF_CREATION_PROMPT + """

    CRITICAL: Output ONLY valid JSON - no explanations, no markdown, no code blocks.
    Always start your response directly with the opening brace { and end with closing brace }"""

    user_message = f"""
    Extract medical information from this clinical note and format it according to the JSON schema below.

    IMPORTANT: Respond with ONLY the JSON object, no other text.

    EVIDENCE SPANS:
    - Set every evidence.note_id to exactly {json.dumps(note_id)}.
    - evidence.text must be copied verbatim from the note.
    - char_start and char_end are offsets into the note. Do not count
      characters carefully; the quoted text is what matters and the offsets
      are recomputed from it.

    JSON Schema:
    {json.dumps(schema_json, indent=2)}

    Medical Note:
    {medical_text}

    JSON Output:
    """

    # C2: the note is MIMIC-derived, so it is labelled restricted and only a
    # provider approved for that classification will accept it. A ProviderError
    # propagates: a model that failed to answer is not a note without content.
    result = provider.complete(
        to_chat_messages([
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ]),
        classification=DataClassification.RESTRICTED_CLINICAL,
        temperature=0.0,
        max_tokens=max_tokens,
    ).text

    extraction = _first_json_object(aggressive_json_clean(result), note_id)
    repaired = _repair_evidence_offsets(extraction, note_id, medical_text)
    if repaired:
        logger.info("Relocated %d evidence span(s) from their quoted text", repaired)

    reference = GTMF(**extraction)
    issues = reference.evidence_issues({note_id: medical_text})
    if issues:
        logger.warning(
            "Extracted %d unresolved evidence span(s): %s",
            len(issues),
            "; ".join(f"{path}: {reason}" for path, reason in issues.items()),
        )
    unevidenced = reference.unevidenced_entities({note_id: medical_text})
    if unevidenced:
        logger.warning(
            "Extracted %d entity/entities without resolvable evidence: %s",
            len(unevidenced),
            ", ".join(unevidenced),
        )
    return reference


def _first_json_object(text: str, note_id: str) -> dict:
    """The first balanced JSON object in a response, or raise.

    An empty or unbalanced result means the note was not read -- most often a
    truncated answer. Returning an empty reference instead would record the
    note as read when it was not (D-08).
    """
    json_start, json_end, depth = -1, -1, 0
    for index, char in enumerate(text):
        if char == "{":
            if json_start == -1:
                json_start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and json_start != -1:
                json_end = index + 1
                break

    data = {}
    if json_start >= 0 and json_end > json_start:
        data = safe_json_parse_object(text[json_start:json_end], note_id)
    if not data:
        raise ExtractionError(
            f"Note {note_id!r} produced no parseable extraction. Returning an "
            "empty reference here would record the note as read when it was "
            "not. A truncated answer is the usual cause: raise max_tokens, "
            "lower the reasoning budget, or widen the serving context so the "
            "note and its answer both fit."
        )
    return data


def process_notes(results, provider: LLMProvider, output_dir: str = 'gtmf'):
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
        "gtmfs_created": 0
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

            gtmf_instance = extract_gtmf(row['text'], provider)
            quality_summary["total_processed"] += 1

            updated_demographics = Demographics.model_validate(demographics)
            gtmf_instance = gtmf_instance.model_copy(update={
                "row_id": row['row_id'],
                "subject_id": row['subject_id'],
                "hadm_id": row['hadm_id'],
                "context": gtmf_instance.context.model_copy(
                    update={"demographics": updated_demographics}
                ),
            })

            subject_id = row['subject_id']
            hadm_id = row['hadm_id']
            filename = f"gtmf_{subject_id}_{hadm_id}.md"
            output_path = os.path.join(output_dir, filename)
            # Pass the model, not a canonical-name dict: the Markdown
            # compatibility layer is responsible for rendering aliases and
            # embedding the lossless SCR payload (including evidence spans).
            save_gtmf_markdown(gtmf_instance, output_path)

            quality_summary["gtmfs_created"] += 1

        except json.JSONDecodeError as e:
            logger.error(f"JSON parsing failed for note at index {idx}: {e}")
            quality_summary["json_parse_failures"] += 1
            quality_summary["total_processed"] += 1
        except Exception as e:
            logger.error(f"Error processing note at index {idx}: {e}")
            quality_summary["total_processed"] += 1

    return quality_summary

def main():
    """Refuse: this entry point selected cases by reading note text.

    It called CSVDataLoader.fetch_notes_with_light_case_filter, which ranks
    notes by is_light_common_case -- a keyword scan over the note body. The
    cohort design forbids exactly that: configs/cohort/criteria_v1.sql states
    "No clinical eligibility decision is made from note vocabulary", because a
    cohort chosen by reading the text is a function of the same text the study
    then measures extraction against, and it cannot reproduce by hash (M3).

    The replacement is two commands: meddial-cohort applies E1-E10 to the
    structured tables and writes an auditable manifest, and meddial-scr
    extracts a reference for exactly the admissions that manifest names.

    extract_gtmf and the parsing helpers in this module are unchanged
    and are what meddial-scr calls.
    """
    raise SystemExit(
        "gtmf_creation.main() has been withdrawn: it selected cases by scanning "
        "note text, which the cohort criteria forbid and which cannot reproduce "
        "by hash.\n\nUse instead:\n"
        "  meddial-cohort --csv-dir <MIMIC_CSV_DIR> --out <dir outside repo>\n"
        "  meddial-scr --csv-dir <MIMIC_CSV_DIR> --cohort <dir>/cohort_private_manifest.json "
        "--out <dir outside repo>\n"
    )


if __name__ == '__main__':
    main()
