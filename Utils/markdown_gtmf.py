import os
import re
from pathlib import Path
from Models.classes import GTMF, Symptom, Diagnosis, Medication, TreatmentOption


def gtmf_to_markdown(gtmf: GTMF | dict[str, any]) -> str:
    if isinstance(gtmf, GTMF):
        gtmf_dict = gtmf.model_dump() if hasattr(gtmf, 'model_dump') else gtmf.dict()
    else:
        gtmf_dict = gtmf

    lines = []

    # Title
    lines.append("# Ground Truth Medical Form (GTMF)\n")

    # Patient Information
    lines.append("## Patient Information\n")
    subject_id = gtmf_dict.get('subject_id', 0)
    hadm_id = gtmf_dict.get('hadm_id', 0)
    row_id = gtmf_dict.get('row_id', 0)
    profile_type = gtmf_dict.get('profile_type', 'FULL')
    lines.append(f"- **Subject ID**: {subject_id}")
    lines.append(f"- **Admission ID**: {hadm_id}")
    lines.append(f"- **Row ID**: {row_id}")
    lines.append(f"- **Profile Type**: {profile_type}\n")

    # Demographics
    demo = gtmf_dict.get('Context_Fields', {}).get('Patient_Demographics', {})
    if demo and any(v != "not provided" and v != 0 for v in demo.values()):
        lines.append("## Demographics\n")
        if demo.get('Age', 0) != 0:
            lines.append(f"- **Age**: {demo.get('Age')}")
        if demo.get('Sex', 'not provided') != 'not provided':
            lines.append(f"- **Sex**: {demo.get('Sex')}")
        if demo.get('Date_of_Birth', 'not provided') != 'not provided':
            lines.append(f"- **Date of Birth**: {demo.get('Date_of_Birth')}")
        if demo.get('Ethnicity', 'not provided') != 'not provided':
            lines.append(f"- **Ethnicity**: {demo.get('Ethnicity')}")
        if demo.get('Marital_Status', 'not provided') != 'not provided':
            lines.append(f"- **Marital Status**: {demo.get('Marital_Status')}")
        if demo.get('Religion', 'not provided') != 'not provided':
            lines.append(f"- **Religion**: {demo.get('Religion')}")
        if demo.get('Insurance', 'not provided') != 'not provided':
            lines.append(f"- **Insurance**: {demo.get('Insurance')}")
        if demo.get('Admission_Type', 'not provided') != 'not provided':
            lines.append(f"- **Admission Type**: {demo.get('Admission_Type')}")
        if demo.get('Admission_Date', 'not provided') != 'not provided':
            lines.append(f"- **Admission Date**: {demo.get('Admission_Date')}")
        if demo.get('Discharge_Date', 'not provided') != 'not provided':
            lines.append(f"- **Discharge Date**: {demo.get('Discharge_Date')}")
        lines.append("")

    # Chief Complaint
    chief_complaint = gtmf_dict.get('Additional_Context', {}).get('Chief_Complaint', 'not provided')
    if chief_complaint and chief_complaint != 'not provided':
        lines.append("## Chief Complaint\n")
        lines.append(f"{chief_complaint}\n")

    # Symptoms
    symptoms = gtmf_dict.get('Core_Fields', {}).get('Symptoms', [])
    if symptoms:
        lines.append("## Symptoms\n")
        for symptom in symptoms:
            if isinstance(symptom, dict):
                desc = symptom.get('description', '')
                onset = symptom.get('onset', 'not provided')
                duration = symptom.get('duration', 'not provided')
                severity = symptom.get('severity', 'not provided')

                line_parts = [f"- **{desc}**"]
                if onset != 'not provided':
                    line_parts.append(f"onset: {onset}")
                if duration != 'not provided':
                    line_parts.append(f"duration: {duration}")
                if severity != 'not provided':
                    line_parts.append(f"severity: {severity}")

                lines.append(" | ".join(line_parts))
            else:
                lines.append(f"- {symptom}")
        lines.append("")

    # Medical History
    history = gtmf_dict.get('Context_Fields', {}).get('Medical_History', {})
    past_history = history.get('Past_Medical_History', 'not provided') if history else 'not provided'
    if past_history and past_history != 'not provided':
        lines.append("## Medical History\n")
        lines.append(f"{past_history}\n")

    # Allergies
    allergies = gtmf_dict.get('Context_Fields', {}).get('Allergies', [])
    if allergies:
        lines.append("## Allergies\n")
        allergies_str = ', '.join(allergies) if isinstance(allergies, list) else str(allergies)
        lines.append(f"{allergies_str}\n")

    # Current Medications
    meds = gtmf_dict.get('Context_Fields', {}).get('Current_Medications', [])
    if meds:
        lines.append("## Current Medications\n")
        for med in meds:
            if isinstance(med, dict):
                name = med.get('name', '')
                dosage = med.get('dosage', 'not provided')
                frequency = med.get('frequency', 'not provided')
                purpose = med.get('purpose', 'not provided')

                line_parts = [f"- **{name}**"]
                if dosage != 'not provided':
                    line_parts.append(f"dosage: {dosage}")
                if frequency != 'not provided':
                    line_parts.append(f"frequency: {frequency}")
                if purpose != 'not provided':
                    line_parts.append(f"purpose: {purpose}")

                lines.append(" | ".join(line_parts))
            else:
                lines.append(f"- {med}")
        lines.append("")

    # Discharge Medications
    discharge_meds = gtmf_dict.get('Context_Fields', {}).get('Discharge_Medications', [])
    if discharge_meds:
        lines.append("## Discharge Medications\n")
        for med in discharge_meds:
            if isinstance(med, dict):
                name = med.get('name', '')
                dosage = med.get('dosage', 'not provided')
                frequency = med.get('frequency', 'not provided')

                line_parts = [f"- **{name}**"]
                if dosage != 'not provided':
                    line_parts.append(f"dosage: {dosage}")
                if frequency != 'not provided':
                    line_parts.append(f"frequency: {frequency}")

                lines.append(" | ".join(line_parts))
            else:
                lines.append(f"- {med}")
        lines.append("")

    # Diagnoses (if in profile type)
    diagnoses = gtmf_dict.get('Core_Fields', {}).get('Diagnoses', [])
    if diagnoses:
        lines.append("## Diagnoses\n")
        for diag in diagnoses:
            if isinstance(diag, dict):
                primary = diag.get('primary', '')
                notes = diag.get('notes', 'not provided')

                if notes != 'not provided':
                    lines.append(f"- **{primary}** | notes: {notes}")
                else:
                    lines.append(f"- {primary}")
            else:
                lines.append(f"- {diag}")
        lines.append("")

    # Treatment Options (if in profile type)
    treatments = gtmf_dict.get('Core_Fields', {}).get('Treatment_Options', [])
    if treatments:
        lines.append("## Treatment Options\n")
        for tx in treatments:
            if isinstance(tx, dict):
                procedure = tx.get('procedure', '')
                details = tx.get('details', 'not provided')
                treatment = tx.get('treatment', 'not provided')

                lines.append(f"### {procedure}\n")
                if details != 'not provided':
                    lines.append(f"**Details**: {details}")
                if treatment != 'not provided':
                    lines.append(f"**Treatment**: {treatment}")

                meds = tx.get('medications', [])
                if meds:
                    lines.append("\n**Medications**:")
                    for med in meds:
                        if isinstance(med, dict):
                            lines.append(f"- {med.get('name', '')} ({med.get('dosage', 'dose not specified')})")
                        else:
                            lines.append(f"- {med}")
                lines.append("")
            else:
                lines.append(f"- {tx}\n")

    # Structured Data Section
    has_structured = False
    structured_lines = ["## Structured Clinical Data\n"]

    # Structured Diagnoses
    struct_diag = gtmf_dict.get('structured_diagnoses', [])
    if struct_diag:
        has_structured = True
        structured_lines.append("### ICD-9 Diagnoses\n")
        for diag in struct_diag[:15]:
            code = diag.get('icd9_code', '')
            desc = diag.get('description', '')
            seq = diag.get('seq_num', '')
            if seq:
                structured_lines.append(f"- **{code}** (seq: {seq}): {desc}")
            else:
                structured_lines.append(f"- **{code}**: {desc}")
        structured_lines.append("")

    # Structured Procedures
    struct_proc = gtmf_dict.get('structured_procedures', [])
    if struct_proc:
        has_structured = True
        structured_lines.append("### ICD-9 Procedures\n")
        for proc in struct_proc[:15]:
            code = proc.get('icd9_code', '')
            desc = proc.get('description', '')
            seq = proc.get('seq_num', '')
            if seq:
                structured_lines.append(f"- **{code}** (seq: {seq}): {desc}")
            else:
                structured_lines.append(f"- **{code}**: {desc}")
        structured_lines.append("")

    # Structured Prescriptions
    struct_rx = gtmf_dict.get('structured_prescriptions', [])
    if struct_rx:
        has_structured = True
        structured_lines.append("### Prescriptions\n")
        for rx in struct_rx[:20]:
            drug = rx.get('drug', '')
            dose = rx.get('dose_val_rx', '')
            structured_lines.append(f"- {drug} {dose}".strip())
        structured_lines.append("")

    if has_structured:
        lines.extend(structured_lines)

    return "\n".join(lines)


def markdown_to_gtmf_dict(markdown_content: str) -> dict[str, any]:
    gtmf = {
        'row_id': 0,
        'subject_id': 0,
        'hadm_id': 0,
        'Core_Fields': {
            'Symptoms': [],
            'Diagnoses': [],
            'Treatment_Options': []
        },
        'Context_Fields': {
            'Patient_Demographics': {
                'Date_of_Birth': 'not provided',
                'Age': 0,
                'Sex': 'not provided',
                'Religion': 'not provided',
                'Marital_Status': 'not provided',
                'Ethnicity': 'not provided',
                'Insurance': 'not provided',
                'Admission_Type': 'not provided',
                'Admission_Date': 'not provided',
                'Discharge_Date': 'not provided'
            },
            'Medical_History': {
                'Past_Medical_History': 'not provided'
            },
            'Allergies': [],
            'Current_Medications': [],
            'Discharge_Medications': []
        },
        'Additional_Context': {
            'Chief_Complaint': 'not provided'
        }
    }

    lines = markdown_content.split('\n')
    current_section = None
    current_subsection = None
    current_treatment = None

    for line in lines:
        line_stripped = line.strip()

        # Section headers
        if line_stripped.startswith('## '):
            current_section = line_stripped[3:].strip()
            current_subsection = None
            current_treatment = None
            continue

        # Subsection headers
        if line_stripped.startswith('### '):
            current_subsection = line_stripped[4:].strip()
            continue

        # Skip empty lines and title
        if not line_stripped or line_stripped.startswith('# '):
            continue

        # Parse content based on section
        if current_section == 'Patient Information':
            if '**Subject ID**:' in line_stripped:
                gtmf['subject_id'] = int(_extract_value(line_stripped))
            elif '**Admission ID**:' in line_stripped:
                gtmf['hadm_id'] = int(_extract_value(line_stripped))
            elif '**Row ID**:' in line_stripped:
                gtmf['row_id'] = int(_extract_value(line_stripped))
            elif '**Profile Type**:' in line_stripped:
                gtmf['profile_type'] = _extract_value(line_stripped)

        elif current_section == 'Demographics':
            demo = gtmf['Context_Fields']['Patient_Demographics']
            if '**Age**:' in line_stripped:
                age_str = _extract_value(line_stripped)
                try:
                    demo['Age'] = int(age_str)
                except:
                    pass
            elif '**Sex**:' in line_stripped:
                demo['Sex'] = _extract_value(line_stripped)
            elif '**Date of Birth**:' in line_stripped:
                demo['Date_of_Birth'] = _extract_value(line_stripped)
            elif '**Ethnicity**:' in line_stripped:
                demo['Ethnicity'] = _extract_value(line_stripped)
            elif '**Marital Status**:' in line_stripped:
                demo['Marital_Status'] = _extract_value(line_stripped)
            elif '**Religion**:' in line_stripped:
                demo['Religion'] = _extract_value(line_stripped)
            elif '**Insurance**:' in line_stripped:
                demo['Insurance'] = _extract_value(line_stripped)
            elif '**Admission Type**:' in line_stripped:
                demo['Admission_Type'] = _extract_value(line_stripped)
            elif '**Admission Date**:' in line_stripped:
                demo['Admission_Date'] = _extract_value(line_stripped)
            elif '**Discharge Date**:' in line_stripped:
                demo['Discharge_Date'] = _extract_value(line_stripped)

        elif current_section == 'Chief Complaint':
            if not line_stripped.startswith('-'):
                current_complaint = gtmf['Additional_Context'].get('Chief_Complaint', '')
                if current_complaint == 'not provided':
                    current_complaint = ''
                gtmf['Additional_Context']['Chief_Complaint'] = (current_complaint + ' ' + line_stripped).strip()

        elif current_section == 'Symptoms':
            if line_stripped.startswith('- '):
                symptom_data = _parse_symptom(line_stripped[2:])
                if symptom_data:
                    gtmf['Core_Fields']['Symptoms'].append(symptom_data)

        elif current_section == 'Medical History':
            if not line_stripped.startswith('-'):
                current_history = gtmf['Context_Fields']['Medical_History'].get('Past_Medical_History', '')
                if current_history == 'not provided':
                    current_history = ''
                gtmf['Context_Fields']['Medical_History']['Past_Medical_History'] = (current_history + ' ' + line_stripped).strip()

        elif current_section == 'Allergies':
            if not line_stripped.startswith('-'):
                allergies_str = line_stripped.strip()
                gtmf['Context_Fields']['Allergies'] = [a.strip() for a in allergies_str.split(',') if a.strip()]

        elif current_section == 'Current Medications':
            if line_stripped.startswith('- '):
                med_data = _parse_medication(line_stripped[2:])
                if med_data:
                    gtmf['Context_Fields']['Current_Medications'].append(med_data)

        elif current_section == 'Discharge Medications':
            if line_stripped.startswith('- '):
                med_data = _parse_medication(line_stripped[2:])
                if med_data:
                    gtmf['Context_Fields']['Discharge_Medications'].append(med_data)

        elif current_section == 'Diagnoses':
            if line_stripped.startswith('- '):
                diag_data = _parse_diagnosis(line_stripped[2:])
                if diag_data:
                    gtmf['Core_Fields']['Diagnoses'].append(diag_data)

        elif current_section == 'Treatment Options':
            if current_subsection and current_subsection != 'Structured Clinical Data':
                # Treatment subsection
                if '**Details**:' in line_stripped:
                    if current_treatment:
                        current_treatment['details'] = _extract_value(line_stripped)
                elif '**Treatment**:' in line_stripped:
                    if current_treatment:
                        current_treatment['treatment'] = _extract_value(line_stripped)
                elif line_stripped.startswith('- ') and current_treatment:
                    # Medication under treatment
                    med_name = line_stripped[2:].strip()
                    current_treatment['medications'].append({'name': med_name, 'dosage': 'not provided', 'frequency': 'not provided', 'purpose': 'not provided'})
            else:
                # New treatment
                current_treatment = {
                    'procedure': current_subsection if current_subsection else line_stripped[2:],
                    'details': 'not provided',
                    'treatment': 'not provided',
                    'medications': []
                }
                gtmf['Core_Fields']['Treatment_Options'].append(current_treatment)

        elif current_section == 'Structured Clinical Data':
            if current_subsection == 'ICD-9 Diagnoses':
                if line_stripped.startswith('- '):
                    if 'structured_diagnoses' not in gtmf:
                        gtmf['structured_diagnoses'] = []
                    diag_entry = _parse_icd_entry(line_stripped[2:])
                    if diag_entry:
                        gtmf['structured_diagnoses'].append(diag_entry)

            elif current_subsection == 'ICD-9 Procedures':
                if line_stripped.startswith('- '):
                    if 'structured_procedures' not in gtmf:
                        gtmf['structured_procedures'] = []
                    proc_entry = _parse_icd_entry(line_stripped[2:])
                    if proc_entry:
                        gtmf['structured_procedures'].append(proc_entry)

            elif current_subsection == 'Prescriptions':
                if line_stripped.startswith('- '):
                    if 'structured_prescriptions' not in gtmf:
                        gtmf['structured_prescriptions'] = []
                    rx_entry = _parse_prescription(line_stripped[2:])
                    if rx_entry:
                        gtmf['structured_prescriptions'].append(rx_entry)

    return gtmf


def _extract_value(line: str) -> str:
    parts = line.split(':', 1)
    if len(parts) > 1:
        return parts[1].strip()
    return ''


def _parse_symptom(line: str) -> dict[str, str]:
    parts = [p.strip() for p in line.split('|')]

    # Extract description (bolded)
    desc_match = re.match(r'\*\*(.+?)\*\*', parts[0])
    description = desc_match.group(1) if desc_match else parts[0].strip('*').strip()

    symptom = {
        'description': description,
        'onset': 'not provided',
        'duration': 'not provided',
        'severity': 'not provided'
    }

    for part in parts[1:]:
        if 'onset:' in part.lower():
            symptom['onset'] = part.split(':', 1)[1].strip()
        elif 'duration:' in part.lower():
            symptom['duration'] = part.split(':', 1)[1].strip()
        elif 'severity:' in part.lower():
            symptom['severity'] = part.split(':', 1)[1].strip()

    return symptom


def _parse_diagnosis(line: str) -> dict[str, str]:
    parts = [p.strip() for p in line.split('|')]

    # Extract primary diagnosis (bolded)
    primary_match = re.match(r'\*\*(.+?)\*\*', parts[0])
    primary = primary_match.group(1) if primary_match else parts[0].strip('*').strip()

    diagnosis = {
        'primary': primary,
        'notes': 'not provided'
    }

    for part in parts[1:]:
        if 'notes:' in part.lower():
            diagnosis['notes'] = part.split(':', 1)[1].strip()

    return diagnosis


def _parse_medication(line: str) -> dict[str, str]:
    parts = [p.strip() for p in line.split('|')]

    # Extract name (bolded)
    name_match = re.match(r'\*\*(.+?)\*\*', parts[0])
    name = name_match.group(1) if name_match else parts[0].strip('*').strip()

    medication = {
        'name': name,
        'dosage': 'not provided',
        'frequency': 'not provided',
        'purpose': 'not provided'
    }

    for part in parts[1:]:
        if 'dosage:' in part.lower():
            medication['dosage'] = part.split(':', 1)[1].strip()
        elif 'frequency:' in part.lower():
            medication['frequency'] = part.split(':', 1)[1].strip()
        elif 'purpose:' in part.lower():
            medication['purpose'] = part.split(':', 1)[1].strip()

    return medication


def _parse_icd_entry(line: str) -> dict[str, any]:
    match = re.match(r'\*\*(.+?)\*\*(?:\s*\(seq:\s*(\d+)\))?\s*:\s*(.+)', line)
    if match:
        entry = {
            'icd9_code': match.group(1).strip(),
            'description': match.group(3).strip()
        }
        if match.group(2):
            entry['seq_num'] = int(match.group(2))
        return entry
    return None


def _parse_prescription(line: str) -> dict[str, any]:
    parts = line.split(maxsplit=1)
    if parts:
        return {
            'drug': parts[0],
            'dose_val_rx': parts[1] if len(parts) > 1 else ''
        }
    return None


def save_gtmf_markdown(gtmf: GTMF | dict[str, any], output_path: str) -> None:
    markdown_content = gtmf_to_markdown(gtmf)

    # Create directory if needed
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(markdown_content)


def load_gtmf_markdown(file_path: str) -> dict[str, any]:
    with open(file_path, 'r', encoding='utf-8') as f:
        markdown_content = f.read()

    return markdown_to_gtmf_dict(markdown_content)


def load_all_gtmfs_from_directory(directory: str) -> list[dict[str, any]]:
    gtmfs = []
    path = Path(directory)

    if not path.exists():
        return gtmfs

    for md_file in sorted(path.glob('*.md')):
        try:
            gtmf_dict = load_gtmf_markdown(str(md_file))
            gtmfs.append(gtmf_dict)
        except Exception as e:
            print(f"Error loading {md_file}: {e}")

    return gtmfs
