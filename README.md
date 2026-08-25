# MedDial

MedDial is a research framework for generating synthetic clinician–patient dialogues from structured EHR-derived references. The publication-oriented implementation controls what each conversational role can know, evaluates patient and doctor claims separately, and records experiments reproducibly.

This software generates research data. It is not a medical device and does not provide patient care or medical advice.

## What is implemented

- Explicit `PatientContext`, `DoctorContext`, and privileged `EvaluatorContext` objects.
- Formal `FULL`, `NO_DIAGNOSIS`, and `NO_DIAGNOSIS_NO_TREATMENT` policies. The last variant hides diagnoses, treatment options, current medications, and discharge medications.
- A doctor that starts with demographics only; symptoms, history, diagnoses, treatments, medications, and allergies must be revealed through dialogue.
- A provider-independent LLM interface with an Azure AI Foundry adapter, dependency injection, mock provider, and per-call metadata when exposed by the provider.
- Structured Clinical Reference (`SCR`) Pydantic v2 models with evidence-provenance fields. `GTMF` remains a compatibility alias.
- Fail-closed role-aware clinical faithfulness across patient and doctor claims, using these claim types: `patient_fact`, `doctor_fact`, `question`, `diagnostic_hypothesis`, `recommendation`, `advice`, and `non_medical`.
- Knowledge-boundary leakage events/rates and deterministic structural validation.
- Mandatory per-dimension acceptance. A composite score is retained for reporting but cannot override a failed or incomplete dimension.
- Independent multi-model evaluator ensemble hooks. They are disabled until explicit providers/models are configured.
- Failure classification and targeted recovery instead of broad prompt rewriting.
- Labelled error-injection benchmark utilities and five CMPB ablation configurations.
- Immutable per-attempt records, run IDs, config/input hashes, and a separate aggregation path.
- Deterministic lexical cohort selection with manifest support. It is not presented as a clinical severity or primary-care classifier.

## Architecture

```text
Clinical note
  -> lower-acuity lexical candidate filter + deterministic manifest
  -> SCR extraction + per-entity evidence provenance
  -> role context builder
       -> masked patient context
       -> demographics-only doctor context
       -> privileged evaluator context
  -> dialogue simulation
  -> structural + boundary + role-aware factuality + naturalness evaluators
  -> per-dimension acceptance
  -> immutable attempt record
  -> separate aggregate analysis
```

New publication modules live in `meddial/`. The historical `Agents/`, `Models/`, and `Utils/` paths remain available as compatibility imports; thesis `.docx` and `.pptx` artifacts and historical generated data are unchanged.

## Setup

Python 3.10 or newer is required.

```bash
git clone https://github.com/alongott15/FinalProject-MedDial.git
cd FinalProject-MedDial
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[azure]"
```

Configure real Azure calls through environment variables; do not commit `.env`:

```bash
cp .env.example .env
# edit AZURE_AI_ENDPOINT, AZURE_AI_API_KEY, and MIMIC_CSV_DIR
```

Dependency definitions are in `pyproject.toml`; `uv.lock` records the resolved environment. `requirements.txt` is retained as a compatibility entry point.

## Usage

Extract new Structured Clinical References from a local MIMIC CSV export:

```bash
python gtmf_creation.py
```

This writes a deterministic `gtmf/cohort_manifest.json` and retains historical `gtmf_*.md` filenames for compatibility. New files contain a human-readable SCR plus a lossless machine-readable appendix.

Generate dialogues:

```bash
python dialogue_generation_framework.py
```

New results are isolated under:

```text
output_dialogue_framework/runs/<run_id>/
  run_manifest.json
  attempt_records/*.json
  outcomes/*.json
  dialogue_*.md
  per_profile_stats.json
  global_stats.json
```

Resume reuses the latest run only when the experiment configuration and input-reference manifest hash match. A mismatched requested run ID raises an error instead of mixing results.

## Ablation configurations

`configs/experiments/` contains:

- `direct_llm.json`
- `structured_single_agent.json`
- `basic_multi_agent.json`
- `knowledge_controlled.json`
- `full_meddial.json`

The configuration layer declares which components belong to each variant. Some legacy generation paths still require a study-specific runner to execute every ablation end to end; the configs and component hooks are present so those runners do not encode variants ad hoc.

## Development and validation

All automated tests use mocks or deterministic components; CI makes no paid model calls.

```bash
python -m pip install -e ".[dev]"
ruff check meddial tests Models dialogue_generation_framework.py
mypy meddial
pytest
```

## Knowledge policies

| Variant | Diagnosis visible to patient | Treatment options | Current meds | Discharge meds |
|---|---:|---:|---:|---:|
| `FULL` | yes | yes | yes | yes |
| `NO_DIAGNOSIS` | no | yes | yes | yes |
| `NO_DIAGNOSIS_NO_TREATMENT` | no | no | no | no |

Background demographics, documented symptoms, medical history, allergies, and chief complaint remain available to the patient simulation. These policies describe simulated starting knowledge, not real-world patient knowledge.

## Limitations

- The lexical cohort filter uses precise include/exclude terms but cannot determine actual acuity, care setting, or suitability without clinical review.
- Rule-based claim extraction and boundary detection provide deterministic baselines and may miss paraphrases. Publication experiments should configure and report independent evaluator models, then validate them with the injected-error benchmark.
- A clinical reference is not ground truth in the absolute sense: it is a structured representation of a source note and can inherit documentation/extraction errors. Evidence provenance supports auditing but does not remove that risk.
- Historical GTMF artifacts can be loaded, but they have no entity-level provenance unless re-extracted.
- Real model behavior, cost, availability, and exact deployment configuration are environment-specific and are not validated in CI.
- The ablation configuration system defines the five variants, but study runners still need to execute and report each variant consistently.

See [migration notes](docs/MIGRATION.md) for behavior changes.

## Data and secrets

Do not commit MIMIC exports, generated run artifacts, credentials, or `.env`. The repository contains historical generated examples; new generated files are ignored by default.

## License

No license has been selected. Until the repository owner adds an approved `LICENSE`, no permission to reuse, modify, or redistribute the code is granted beyond applicable law. License selection is an explicit owner TODO; this change does not invent licensing terms.
