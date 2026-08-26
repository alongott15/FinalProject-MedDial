# MedDial

MedDial is a research framework for generating synthetic clinician–patient dialogues from structured EHR-derived references. The publication-oriented implementation controls what each conversational role can know, evaluates patient and doctor claims separately, and records experiments reproducibly.

This software generates research data. It is not a medical device and does not provide patient care or medical advice.

## What is implemented

- Explicit `PatientContext`, `DoctorContext`, and privileged `EvaluatorContext` objects.
- Formal `FULL`, `NO_DIAGNOSIS`, and `NO_DIAGNOSIS_NO_TREATMENT` policies. The last variant hides diagnoses, treatment options, current medications, and discharge medications.
- A doctor that starts with demographics only; symptoms, history, diagnoses, treatments, medications, and allergies must be revealed through dialogue.
- A provider-independent LLM interface with a local OpenAI-compatible adapter for restricted clinical data, an explicitly public/synthetic-only Azure adapter, dependency injection, mocks, and per-call metadata.
- Structured Clinical Reference (`SCR`) Pydantic v2 models with evidence-provenance fields. `GTMF` remains a compatibility alias.
- Fail-closed role-aware clinical faithfulness across patient and doctor claims, using these claim types: `patient_fact`, `doctor_fact`, `question`, `diagnostic_hypothesis`, `recommendation`, `advice`, and `non_medical`.
- Knowledge-boundary leakage events/rates and deterministic structural validation.
- Mandatory per-dimension acceptance. A composite score is retained for reporting but cannot override a failed or incomplete dimension.
- A strict three-family evaluator ensemble with claim-level verdicts, per-dimension scores, majority consensus, and fail-closed completeness.
- Failure classification and targeted recovery instead of broad prompt rewriting.
- Labelled error-injection benchmark utilities and five CMPB ablation configurations.
- Immutable per-attempt records, run IDs, config/input hashes, and a separate aggregation path.
- Lexical-v3 candidate selection, one admission per patient, two-clinician review/adjudication, private manifests, and release-safe salted manifests.
- A hashed 13-cell publication study plan separating architecture, policy sensitivity, and targeted recovery.

## Architecture

```text
Clinical note
  -> lexical-v3 candidate filter
  -> two independent clinical reviews + adjudication
  -> private deterministic manifest / redacted release manifest
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

New publication modules live in `meddial/`. The historical `Agents/`, `Models/`, and `Utils/` paths remain available as compatibility imports. Previously tracked generated clinical profiles, generated dialogues, and the bundled MTS-Dialog CSV have been removed from the current tree; public Git history still requires a separate owner-approved data-governance review.

## Setup

Python 3.10 or newer is required.

```bash
git clone https://github.com/alongott15/FinalProject-MedDial.git
cd FinalProject-MedDial
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Configure the local controlled inference server and private data paths; do not commit `.env`:

```bash
cp .env.example .env
# edit MIMIC_CSV_DIR, MEDDIAL_LOCAL_LLM_BASE_URL, MEDDIAL_LOCAL_LLM_MODEL,
# and MEDDIAL_COHORT_REVIEWS
```

Dependency definitions are in `pyproject.toml`; `uv.lock` records the resolved environment. `requirements.txt` is retained as a compatibility entry point.

## Usage

First create the private lexical-candidate review template:

```bash
meddial-cohort review-template \
  --mimic-dir "$MIMIC_CSV_DIR" \
  --candidate-limit 1000 \
  --output private/cohort_reviews.json
```

After two independent clinical reviews and adjudication, set `MEDDIAL_COHORT_REVIEWS` and extract new Structured Clinical References locally:

```bash
python gtmf_creation.py
```

This writes a private deterministic `gtmf/cohort_manifest.json`. New files contain a human-readable SCR plus a lossless machine-readable appendix and are ignored by Git.

To produce a public metadata-only manifest, keep the salt in the environment:

```bash
meddial-cohort release-manifest \
  --private-manifest gtmf/cohort_manifest.json \
  --output outputs/cohort_manifest.release.json
```

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

## Publication study

`configs/experiments/` contains:

- `direct_llm.json`
- `structured_single_agent.json`
- `basic_multi_agent.json`
- `knowledge_controlled.json`
- `full_meddial.json`

Generate the frozen, hashed 13-cell plan without making model calls:

```bash
meddial-study-plan \
  --cohort-manifest gtmf/cohort_manifest.json \
  --generation-model gpt-oss-20b \
  --output private/cmpb-study-plan.json
```

The plan separates raw one-attempt architecture comparisons, knowledge-policy sensitivity, and targeted recovery. `PublicationStudyRunner` refuses to run if its supplied executor does not declare all five variant implementations, preventing legacy multi-agent runs from being mislabeled as direct or single-agent ablations. See [the experiment protocol](docs/EXPERIMENT_PROTOCOL.md).

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

- The lexical cohort filter cannot determine acuity; publication selection requires the two-reviewer workflow and adjudication.
- Rule-based claim extraction and boundary detection provide deterministic baselines and may miss paraphrases. Publication experiments should configure and report independent evaluator models, then validate them with the injected-error benchmark.
- A clinical reference is not ground truth in the absolute sense: it is a structured representation of a source note and can inherit documentation/extraction errors. Evidence provenance supports auditing but does not remove that risk.
- Historical GTMF artifacts are excluded from the publication cohort and must be re-extracted locally with provenance.
- Real local-model behavior, compute cost, availability, and quantization are environment-specific and are not validated in CI.
- The study runner requires a variant executor; it deliberately fails rather than pretending the legacy pipeline implements every ablation.

See [migration notes](docs/MIGRATION.md) for behavior changes.

## Data and secrets

Do not commit MIMIC exports, SCRs, dialogues derived from them, private manifests/reviews, credentials, salts, or `.env`. External model services are blocked for inputs classified as `restricted_clinical`. Read [data governance](docs/DATA_GOVERNANCE.md) before any public release.

## License

Apache-2.0 is recommended for original MedDial code, but no license has yet been granted. The owner must first confirm copyright authority and add approved `LICENSE`/`NOTICE` files. MIMIC derivatives are excluded, and MTS-Dialog retains separate CC BY 4.0 terms. See [licensing guidance](docs/LICENSING.md).
