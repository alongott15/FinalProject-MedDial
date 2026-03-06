import json
import logging
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import SystemMessage, UserMessage
from azure.core.credentials import AzureKeyCredential
from Models.classes import GTMF
from Utils.utils import format_date, calculate_age
from Utils.bias_aware_prompts import GTMF_CREATION_PROMPT
from Utils.markdown_gtmf import save_gtmf_markdown
import re
import os


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

class AzureAIClient:
    def __init__(self, endpoint: str = None, api_key: str = None, model_name: str = "gpt-4.1"):
        self.endpoint = endpoint or os.getenv("AZURE_AI_ENDPOINT")
        self.api_key = api_key or os.getenv("AZURE_AI_API_KEY")
        self.model_name = model_name

        if not self.endpoint or not self.api_key:
            raise ValueError("Azure AI endpoint and API key must be provided")

        self.client = ChatCompletionsClient(
            endpoint=self.endpoint,
            credential=AzureKeyCredential(self.api_key)
        )

    def chat_completion(self, system_message: str, user_message: str, temperature: float = 0.0) -> str:
        try:
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

def extract_gtmf_chunked(medical_text: str, azure_client: AzureAIClient) -> GTMF:
    schema_json = GTMF.model_json_schema()
    chunks = chunk_medical_text(medical_text, max_chunk_size=3000, overlap=200)

    system_message = GTMF_CREATION_PROMPT + """

    CRITICAL: Output ONLY valid JSON - no explanations, no markdown, no code blocks.
    Always start your response directly with the opening brace { and end with closing brace }"""

    all_extractions = []

    for i, chunk in enumerate(chunks):
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
                    all_extractions.append(data)

        except Exception as e:
            logger.error(f"Error processing chunk {i+1}: {e}")
            continue

    if not all_extractions:
        logger.error("No valid extractions obtained")
        minimal_extraction = safe_json_parse_object("", "minimal_fallback")
        all_extractions = [minimal_extraction]

    merged_extraction = merge_gtmf_extractions(all_extractions)

    try:
        return GTMF(**merged_extraction)
    except Exception as e:
        logger.error(f"Error in extract_gtmf_chunked: {e}")
        raise

def merge_gtmf_extractions(extractions: list[dict]) -> dict:
    if not extractions:
        raise ValueError("No extractions to merge")

    if len(extractions) == 1:
        return extractions[0]

    merged = extractions[0].copy()
    merged_symptoms = []
    merged_diagnoses = []
    merged_treatments = []
    seen_symptoms = set()
    seen_diagnoses = set()
    seen_treatments = set()

    for extraction in extractions:
        core_fields = extraction.get("Core_Fields", {})

        for symptom in core_fields.get("Symptoms", []):
            desc = symptom.get("description", "").strip().lower()
            if desc and desc != "not provided" and desc not in seen_symptoms:
                merged_symptoms.append(symptom)
                seen_symptoms.add(desc)

        for diagnosis in core_fields.get("Diagnoses", []):
            primary = diagnosis.get("primary", "").strip().lower()
            if primary and primary != "not provided" and primary not in seen_diagnoses:
                merged_diagnoses.append(diagnosis)
                seen_diagnoses.add(primary)

        for treatment in core_fields.get("Treatment_Options", []):
            procedure = treatment.get("procedure", "").strip().lower()
            if procedure and procedure != "not provided" and procedure not in seen_treatments:
                merged_treatments.append(treatment)
                seen_treatments.add(procedure)

    merged["Core_Fields"]["Symptoms"] = merged_symptoms
    merged["Core_Fields"]["Diagnoses"] = merged_diagnoses
    merged["Core_Fields"]["Treatment_Options"] = merged_treatments

    for extraction in extractions[1:]:
        context_fields = extraction.get("Context_Fields", {})

        merged_allergies = merged.get("Context_Fields", {}).get("Allergies", [])
        for allergy in context_fields.get("Allergies", []):
            if allergy not in merged_allergies:
                merged_allergies.append(allergy)
        merged["Context_Fields"]["Allergies"] = merged_allergies

        merged_current_meds = merged.get("Context_Fields", {}).get("Current_Medications", [])
        for med in context_fields.get("Current_Medications", []):
            if med not in merged_current_meds:
                merged_current_meds.append(med)
        merged["Context_Fields"]["Current_Medications"] = merged_current_meds

        merged_discharge_meds = merged.get("Context_Fields", {}).get("Discharge_Medications", [])
        for med in context_fields.get("Discharge_Medications", []):
            if med not in merged_discharge_meds:
                merged_discharge_meds.append(med)
        merged["Context_Fields"]["Discharge_Medications"] = merged_discharge_meds

    return merged

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

            gtmf_instance = extract_gtmf_chunked(row['text'], azure_client)
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
            result["case_type"] = "LIGHT_COMMON_SYMPTOMS"

            subject_id = row['subject_id']
            hadm_id = row['hadm_id']
            filename = f"gtmf_{subject_id}_{hadm_id}.md"
            output_path = os.path.join(output_dir, filename)
            save_gtmf_markdown(result, output_path)

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
        # Using 800 to ensure we have enough after light case filtering and skip-existing logic
        results = loader.fetch_notes_with_light_case_filter(
            category_filter="Discharge summary",
            limit=10000000000000  # Increased to ensure we reach 300 total after filtering and skipping
        )
        if not results:
            logger.error("No light case notes found")
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

        print(f"\n=== GTMF Processing Summary ===")
        print(f"Total processed: {summary['total_processed']}")
        print(f"Skipped existing: {summary['skipped_existing']}")
        print(f"GTMFs created (NEW): {summary['gtmfs_created']}")
        print(f"Light cases passed: {summary['light_case_passed']}")
        print(f"Light cases filtered: {summary['light_case_failed']}")
        print(f"JSON parse failures: {summary['json_parse_failures']}")

    except Exception as e:
        logger.error(f"Error in main execution: {e}")

if __name__ == '__main__':
    main()