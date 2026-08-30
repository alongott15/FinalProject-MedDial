# MedDial — Product Requirements Document

| | |
|---|---|
| **Subject** | Rebuilding the MedDial codebase so its results can support a peer-reviewed publication |
| **Baseline** | `github.com/alongott15/FinalProject-MedDial` @ branch `new`, commit `61156f7` |
| **Author** | Alon Gottlib |
| **Version** | 2.0 — full specification |
| **Date** | 26 August 2026 |
| **Companion** | `MedDial_Implementation_Plan.md` · publication strategy artifact |
| **Status** | Decisions D1–D5 resolved (§13). Ready to implement. |

### Version history

| Ver | Change |
|---|---|
| 1.0 | Initial requirements from the code audit of branch `new` |
| 1.1 | D1–D5 recorded; throughput constraint C6 added; EVAL-10 added |
| **2.0** | D4 revised to no external corpus; D5 revised to TMLR-first; GRND promoted to the project's sole external anchor; expanded to full specification density — glossary, data model, metric definitions, non-functional requirements, traceability |

---

## 1. Context

MedDial generates synthetic patient–physician dialogues from MIMIC-III clinical notes with a multi-agent LLM pipeline and validates them with an LLM-as-judge that scores naturalness, profile compliance and faithfulness into a weighted composite behind a 0.70 pass gate. The thesis reports 498 GTMFs, 1,494 dialogues, a 98.66% pass rate and mean faithfulness 0.787.

A code audit of branch `new` found that those numbers cannot support a publication in their current form. This document specifies what must be true of the codebase before they can.

### 1.1 Defect inventory

Each defect is traced to the requirements that close it.

| # | Defect | Evidence in `new` | Closed by |
|---|---|---|---|
| **D-01** | Restricted MIMIC-derived data is public | 498 files in `gtmf/`, 1,494 in `output_dialogue_framework/`, keyed by `subject_id`/`hadm_id` in filename and body; `MTS-Dialog.csv` bundled | GOV-1, GOV-2 |
| **D-02** | Faithfulness ignores the doctor entirely | `_compute_ragas_faithfulness(patient_turns, …)`; compliance prompt says "Focus ONLY on the patient's utterances. Ignore the doctor's turns." | EVAL-1 |
| **D-03** | Faithfulness reference shrinks with the policy | `_build_faithfulness_context(patient_profile, profile_type)` returns "what the patient is *allowed to know*", so the reference narrows at each disclosure step — confounding the headline trend | EVAL-2 |
| **D-04** | Masking does not mask diagnosis-equivalent fields | `generate_partial_profiles` clears only `Core_Fields.Diagnoses` and `Core_Fields.Treatment_Options`; `Context_Fields.Current_Medications`, `.Discharge_Medications` (each `Medication` carries `purpose`) and `Medical_History.Past_Medical_History` survive — under **both** no-diagnosis policies | KNOW-5, KNOW-7 |
| **D-05** | The policy also rewrites the doctor's prompt | `DoctorAgent._consultation_guidance` branches on `profile_type`, so patient information and doctor conditioning vary together | KNOW-6, EXP-8 |
| **D-06** | Silent fallback with no provenance | On any exception the judge logs a warning and switches to an ad-hoc direct-LLM scorer; the result dict records the score but never which scorer produced it | EVAL-3, EVAL-4 |
| **D-07** | Degenerate dialogues score perfectly | `if not statements: return 1.0` | EVAL-5 |
| **D-08** | Provider errors become dialogue text | `chat_generate` returns `"[ERROR: …]"` as a **string**; that string can enter a transcript and be scored as an utterance | GOV-3 (raise semantics) |
| **D-09** | Cohort selection is lexical | Keyword include/exclude over note text; Appendix A's own example is a 71-year-old post-CABG sternal wound drainage with CHF, AF, PVD, TIAs and diabetes, admitted URGENT, classified `FULL` | COH-1 |
| **D-10** | Results are unreproducible | No run manifest, config hash, seed or per-attempt record; output is markdown plus two aggregate JSON files | EXP-1 to EXP-4 |
| **D-11** | Statistics ignore the nested design | 1,494 dialogues from 498 cases, three policies each; intervals treat policies as independent samples | STAT-1 |
| **D-12** | Evaluation is throughput-bound | `_is_statement_faithful` is called once per claim — ~18 model calls per dialogue per reference mode | EVAL-10 |
| **D-13** | No licence, no tests, inaccurate README | README is ReadmeCodeGen output documenting `analysis/`, `tests/`, `configs/` and `DialogueSummarizerAgent.py`, none of which exist; 5,514 LOC with zero tests | GOV-7, REPO-1 to REPO-6 |

### 1.2 The organising constraint

Clinician evaluation is permanently unavailable — no expert rating study, no cohort review, no adjudication, at any scale. Under D4 there is additionally no external dialogue corpus. The project therefore cannot make clinical-validity or comparative-realism claims, and must instead **measure its own evaluation instrument** against the two forms of ground truth that remain available: faults injected deliberately (correct by construction) and MIMIC's own clinician-coded structured tables.

---

## 2. Product goal

Convert MedDial from a system that asserts its output is good into a **measured evaluation instrument** whose properties are known, reported and reproducible by a third party — such that a manuscript built on it can be defended at TMLR, and subsequently CMPB, with no clinician input and no external corpus.

### 2.1 Goals

| ID | Goal | Measured by |
|----|------|-------------|
| G1 | Every reported score carries known provenance — which scorer, which reference, which turns | EVAL-3 |
| G2 | Detection ability is quantified against objective ground truth, not asserted | BENCH-1, BENCH-2 |
| G3 | The pipeline is anchored to clinician-authored ground truth wherever any exists | GRND-1 to GRND-4 |
| G4 | The information-disclosure finding is attributed to cause, not merely observed | E0 protocol |
| G5 | Every experiment is reproducible from a frozen configuration by a stranger with MIMIC credentials | EXP-1 to EXP-4 |
| G6 | No restricted data leaves an approved environment; no restricted artifact is distributed | GOV-1 to GOV-6 |
| G7 | The repository is a citable, reusable research artifact | REPO-1 to REPO-6 |

### 2.2 Non-goals

- **Improving dialogue quality, realism or pass rate.** The generator is the *system under test*. Tuning it to score better confounds the measurement and is explicitly forbidden after threshold freeze (EXP-6).
- Any claim about clinical validity, safety, diagnostic correctness or fitness for patient-facing deployment.
- Any claim comparing generated dialogues to real clinical conversation. Under D4 there is no evidence for such a claim in any form.
- Complex, multi-system or high-acuity cases.
- Multilingual or Hebrew-language generation.
- A production service, API or user interface.
- Rewriting from scratch. Branch `main` is read as a **design reference** (D1); its code is not ported, but its structure informs the rebuild.

---

## 3. Users and user stories

### 3.1 Author (primary)

| # | Story | Satisfied by |
|---|---|---|
| U-A1 | As the author, I can re-score the existing corpus under a different faithfulness reference without regenerating anything, so I can determine what the disclosure trend actually measures. | EVAL-2, E0 |
| U-A2 | As the author, I can state for any number in the manuscript which run produced it, under which configuration, scored by which model. | EXP-2, EXP-3, EVAL-3 |
| U-A3 | As the author, I can regenerate every table and figure with one command, so no statistic is ever transcribed by hand. | STAT-4 |
| U-A4 | As the author, I can answer a reviewer's question about any individual dialogue from stored records rather than from memory or a re-run. | EXP-3 |
| U-A5 | As the author, I can run the whole pipeline on synthetic fixtures with no network and no MIMIC access, so I can develop without touching restricted data. | REPO-3, GOV-3 |
| U-A6 | As the author, I am prevented by the tooling — not by discipline — from sending restricted data to an unapproved provider. | GOV-3 |

### 3.2 Peer reviewer

| # | Story | Satisfied by |
|---|---|---|
| U-R1 | As a reviewer, I can see that the evaluation instrument was validated *before* it was used to draw conclusions. | BENCH-2 reported before EVAL results |
| U-R2 | As a reviewer, I can see exactly which claims the evidence supports and which are out of scope. | §11, manuscript Methods paragraph |
| U-R3 | As a reviewer, I can confirm the data governance is sound without taking it on trust. | GOV-1 to GOV-6 |
| U-R4 | As a reviewer, I can check that a reported effect is not an artifact of the measurement. | E0 four-way attribution |

### 3.3 Independent researcher

| # | Story | Satisfied by |
|---|---|---|
| U-I1 | As another researcher, I can run the error-injection benchmark against my own judge without MIMIC credentials. | BENCH-3 |
| U-I2 | As another researcher, I can reproduce the headline result from the released configuration and a PhysioNet deposit. | EXP-1, GOV-5 |
| U-I3 | As another researcher, I can reuse the disclosure-policy formalism in my own simulator. | KNOW-2 |

---

## 4. Constraints and assumptions

| ID | Constraint | Consequence |
|----|------------|-------------|
| **C1** | **No human clinical input of any kind** — no rating study, no cohort review, no adjudication | Any requirement implying one is invalid. COH-2 exists to enforce this. |
| **C2** | **MIMIC-III is restricted.** Notes, references, profiles and dialogues are all restricted. No third-party API that retains data; no public distribution; sharing via PhysioNet under credentialed access only | GOV-3, GOV-5 |
| **C3** | **Single part-time developer**, ~17–19 weeks | Prefer the smallest implementation that satisfies each requirement. |
| **C4** | **Existing assets are reusable** — 498 references and 1,494 dialogues exist and can be re-scored without regeneration, inside an approved environment | E0 runs on existing data |
| **C5** | **Zero publication budget.** Free venues only; no paid tooling or annotation services | D5 |
| **C6** | **Local inference is throughput-bound.** One model call per claim means tens of thousands of serial calls per pass | EVAL-10 is a prerequisite, not an optimisation |
| **C7** | **No external dialogue corpus.** No MTS-Dialog, ACI-Bench, MEDIQA-Chat or equivalent. The only human-authored ground truth is MIMIC's coded structured tables | BENCH-4/5 and DOWN-1 are internal; GRND is the sole anchor |
| **C8** | **Determinism is required but not guaranteed by the stack.** Quantised local inference varies with batch size and server version | Record digest, quantisation, seed, batch configuration (EXP-2) |
| **A1** | A locally hosted OpenAI-compatible inference server is available | If false, C2 forces documented zero-retention Azure approval as a **blocking** prerequisite |
| **A2** | MIMIC-III structured tables are available in the same PostgreSQL instance as `NOTEEVENTS`: `DIAGNOSES_ICD`, `PRESCRIPTIONS`, `LABEVENTS`, `ICUSTAYS`, `ADMISSIONS`, `PATIENTS`, `PROCEDURES_ICD`, `D_ICD_DIAGNOSES`, `D_ICD_PROCEDURES` | COH-1, GRND-1/2 |
| **A3** | Sufficient GPU capacity exists for the confirmatory run, or the fallbacks in Implementation Plan Appendix B are acceptable | M4 schedule |

---

## 5. Glossary

| Term | Definition |
|---|---|
| **SCR** — Structured Clinical Reference | The canonical structured record extracted from a clinical note. Supersedes GTMF; `GTMF` is retained as an alias. Carries per-entity evidence provenance. |
| **GTMF** | Ground Truth Medical Form. The thesis's name for the SCR. Retained for compatibility and for continuity with the published thesis. |
| **Knowledge policy** | A versioned, declarative object stating which SCR fields are visible to the patient role and which to the doctor role. Instances: `FULL`, `NO_DIAGNOSIS`, `NO_DIAGNOSIS_NO_TREATMENT`. |
| **Permissible-knowledge set** | The set of SCR field paths a given role may reference under a given policy. |
| **Patient context** | The policy-masked view of the SCR given to the patient agent. |
| **Doctor context** | The view given to the doctor agent. Under KNOW-6 its content and its guidance text are configured independently of the patient policy. |
| **Evaluator context** | The privileged, unmasked view given to the evaluator. Never reaches a generating agent. |
| **Reference mode** | Which reference a faithfulness score is computed against. `policy_context` = the role's permissible-knowledge set (reproduces the thesis). `full_reference` = the complete SCR, constant across policies. |
| **Turn scope** | Which turns a metric covers: `patient`, `doctor`, or `both`. |
| **Claim** | An atomic, verifiable assertion extracted from one turn, tagged with speaker role, turn index and claim type. |
| **Leakage event** | An instance of a role referencing information outside its permissible-knowledge set, recorded with turn index, field path and policy violated. |
| **Injected fault** | A deliberate corruption applied to a clean dialogue with a known label, used as ground truth for detector validation. |
| **Retention** | The proportion of clinician-coded clinical facts for an admission that are recoverable from the dialogue alone. |
| **Attempt record** | An immutable per-attempt artifact containing inputs, outputs, all scores with provenance, timings and cost. |
| **Run manifest** | The frozen description of a run: configuration, hashes, model digests, environment. |
| **INCOMPLETE** | A first-class evaluation outcome, distinct from pass and fail, meaning a metric could not be computed. Excluded from aggregates; never defaulted. |

---

## 6. Data model

Canonical structures. The implementation plan gives the Python and JSON forms; this section fixes the semantics.

### 6.1 Structured Clinical Reference

```
SCR
├─ identifiers      row_id, subject_id, hadm_id, study_id (salted)
├─ core
│   ├─ symptoms[]         description, onset, duration, severity, evidence
│   ├─ diagnoses[]        primary, icd9_codes[], notes, evidence
│   └─ treatments[]       procedure, details, treatment, medications[], evidence
├─ context
│   ├─ demographics       age, sex, dob, ethnicity, marital_status,
│   │                     religion, insurance, admission_type, dates
│   ├─ medical_history    past_medical_history, evidence
│   ├─ allergies[]
│   ├─ current_medications[]     name, purpose, dosage, frequency, evidence
│   └─ discharge_medications[]   same shape
├─ additional
│   └─ chief_complaint
└─ provenance
    ├─ extractor_model, extractor_digest, prompt_version
    └─ per-entity EvidenceSpan(note_id, char_start, char_end, text)
```

**Every entity carries an `EvidenceSpan`** (KNOW-1). An entity without one is an extraction the pipeline cannot defend, and it is recorded as such rather than silently trusted.

### 6.2 Claim types

Only some claim types are subject to faithfulness scoring. This distinction is essential and absent from `new`.

| Type | Scored for faithfulness? | Rationale |
|---|---|---|
| `patient_fact` | **Yes**, against the patient's permissible set or the full reference | A patient asserting an unsupported symptom is a hallucination |
| `doctor_fact` | **Yes**, against the doctor's permissible set or the full reference | A doctor asserting an unsupported finding is a hallucination |
| `diagnostic_hypothesis` | **No** — scored for plausibility only | "This could be a urinary tract infection" is clinical reasoning, not fabrication. Penalising it as hallucination is a category error, and doing so would systematically punish exactly the behaviour the doctor agent is supposed to exhibit |
| `recommendation` | No | Forward-looking; not a claim about the record |
| `advice` | No | As above |
| `question` | No | Not an assertion |
| `non_medical` | No | Conversational filler |

### 6.3 Evaluation result

```
EvaluationResult
├─ dialogue_id, run_id, attempt_index
├─ scores{}
│   └─ per dimension: value | null, status ∈ {PASS, FAIL, INCOMPLETE},
│                     provenance{...}, detail{...}
├─ leakage_events[]     turn_index, role, field_path, policy, excerpt
├─ claims[]             turn_index, role, type, text, verdict, justification
├─ acceptance           per-dimension verdicts + overall ∈ {ACCEPT, REJECT, INCOMPLETE}
├─ composite            reporting only; never used for acceptance
└─ cost                 calls, prompt_tokens, completion_tokens, wall_ms
```

### 6.4 Score provenance

Attached to **every** score; a score is not constructible without it (EVAL-3).

| Field | Meaning |
|---|---|
| `scorer_id` | Which scorer implementation |
| `model_family` | Pretraining lineage — the unit that matters for EVAL-8 |
| `model_id`, `model_digest` | Exact weights |
| `quantisation` | Q4_K_M, Q8, fp16 — affects results (C8) |
| `reference_mode` | `policy_context` \| `full_reference` |
| `turn_scope` | `patient` \| `doctor` \| `both` |
| `prompt_version` | Hash of the prompt template |
| `sampling` | temperature, top_p, seed |
| `fallback_used` | Always `false` after EVAL-4; retained so historical records remain interpretable |

### 6.5 Attempt record

One per generation attempt, append-only, never mutated (EXP-3).

```
runs/<run_id>/
├─ run_manifest.json            config, all hashes, model digests, environment,
│                               freeze timestamp, git commit
├─ attempts/<case>_<policy>_<n>.json
│   ├─ inputs                   scr_hash, policy_id+version, contexts_hash,
│   │                           prompt_versions, seed
│   ├─ dialogue                 turns[] with role, index, text
│   ├─ evaluation               full EvaluationResult
│   ├─ repair                   which dimension failed, what was changed
│   └─ cost                     per-call metadata
├─ outcomes/<case>_<policy>.json   final accepted attempt, or terminal failure
└─ logs/
```

Aggregation reads `attempts/` only and never triggers generation (EXP-4).

---

## 7. Functional requirements

**Priority:** P0 blocks submission · P1 required for the target venues · P2 strengthens the submission.
Each requirement states its acceptance criterion — the condition an automated test or a reported artifact must satisfy.

### 7.1 Compliance and governance — `GOV`

| ID | Requirement | Acceptance criteria | Pri |
|----|-------------|---------------------|-----|
| GOV-1 | No restricted artifact in the working tree or in git history | Fresh clone of every branch contains zero files under `gtmf/`, `output_dialogue_framework/`, and no bundled external corpus. A history scan for `<subject>_<hadm>`-shaped filenames returns nothing across all refs. | P0 |
| GOV-2 | Restricted artifacts cannot be re-committed | CI fails on any commit adding a path matching the restricted patterns. `.gitignore` covers every generated output directory. A deliberate test commit is rejected. | P0 |
| GOV-3 | Restricted-data calls reach only approved providers | A call carrying `RESTRICTED_CLINICAL` to a non-approved provider raises **before any network I/O**; a mocked transport asserts zero requests were made. Provider errors raise rather than returning error strings (closes D-08). | P0 |
| GOV-4 | Provider configuration is recorded per run | Run manifest records provider class, model id, model digest, quantisation, endpoint host and a data-handling attestation string. | P0 |
| GOV-5 | The corpus is deposited on PhysioNet under credentialed access | A deposit exists with a DOI; the manuscript's data-availability statement cites it and points nowhere else. | P1 |
| GOV-6 | A public release manifest contains no identifiers | Release manifest holds salted study IDs and aggregate metadata only; the salt is never committed; a test asserts no `subject_id` or `hadm_id` appears. | P1 |
| GOV-7 | The repository carries an explicit licence | `LICENSE` present (MIT or Apache-2.0), referenced from README and `CITATION.cff`. | P0 |

### 7.2 Knowledge model and disclosure policy — `KNOW`

| ID | Requirement | Acceptance criteria | Pri |
|----|-------------|---------------------|-----|
| KNOW-1 | The reference records field-level provenance | Every extracted entity carries `EvidenceSpan(note_id, char_start, char_end, text)`. Round-trip serialisation preserves it. Entities without evidence are flagged, not silently trusted. | P1 |
| KNOW-2 | Disclosure policies are declarative, not dictionary deletion | Each policy declares, as data, the permissible-knowledge set for the patient role and for the doctor role. The three policies are instances of one type. Adding a policy requires no code change. | P0 |
| KNOW-3 | Three role contexts are constructed and separated | `PatientContext`, `DoctorContext`, `EvaluatorContext` built from (reference, policy). Property test: for every policy, no field outside the permitted set is reachable from `PatientContext` by any accessor or serialisation path. | P0 |
| KNOW-4 | Policy definitions are versioned | Each policy carries a version string recorded in run manifests, so results are attributable to a specific masking definition. Changing a policy without bumping its version fails a test. | P1 |
| KNOW-5 | Masking covers diagnosis-**equivalent** information | A documented audit maps every reference field to the information it discloses. Under **both** `NO_DIAGNOSIS` and `NO_DIAGNOSIS_NO_TREATMENT`, `context.current_medications` and `context.discharge_medications` are **dropped entirely**; `past_medical_history` is retained with index-diagnosis terms redacted. Tests: no permitted patient field under a no-diagnosis policy contains a term from that case's masked index-diagnosis set; NDNT's visible set is a proper subset of NO_DIAGNOSIS's. Closes D-04. | P0 |
| KNOW-7 | Thesis-compatible policies remain runnable but cannot enter headline results | The v1.0 policy definitions (masking `core.diagnoses` and `core.treatments` only) are registered and marked `deprecated`. The confirmatory runner refuses a deprecated policy; only an E0 configuration may use one. This makes the masking confound a direct A/B rather than an estimate. | P0 |
| KNOW-6 | Patient-side and doctor-side conditioning are separable | The patient policy and the doctor's guidance are independently configurable; an experiment can vary one while holding the other fixed. Closes D-05. | P0 |

### 7.3 Evaluation — `EVAL`

| ID | Requirement | Acceptance criteria | Pri |
|----|-------------|---------------------|-----|
| EVAL-1 | Faithfulness covers both roles, reported separately | Result carries `patient_factuality` and `doctor_factuality` as distinct scores. Fixture with a fabricated doctor diagnosis fails `doctor_factuality` and passes `patient_factuality`. Closes D-02. | P0 |
| EVAL-2 | The faithfulness reference is explicitly selectable | Scorer accepts `reference_mode ∈ {policy_context, full_reference}` and records which was used. The same dialogue scored under both yields two independently reported scores. Closes D-03. | P0 |
| EVAL-3 | Every score records its provenance | Every score carries the §6.4 provenance block. Constructing a score without it raises. | P0 |
| EVAL-4 | The evaluator fails closed | No fallback path exists. A metric that cannot be computed yields `INCOMPLETE`; the dialogue is excluded from aggregates rather than defaulted. Test: a raising scorer produces `INCOMPLETE`, not a number. Closes D-06. | P0 |
| EVAL-5 | Degenerate inputs never score well | Empty, whitespace-only or claim-free dialogues return `INCOMPLETE`. Regression test covers the historical `return 1.0` path. Closes D-07. | P0 |
| EVAL-6 | Knowledge-boundary leakage is first-class | Per dialogue: a leakage event list (turn index, role, field path, policy, excerpt). Per run: zero-leakage rate by policy. | P0 |
| EVAL-7 | Acceptance is per-dimension | A dialogue failing any mandatory dimension is rejected regardless of composite. Composite is computed and reported for continuity with the thesis but cannot influence acceptance. | P1 |
| EVAL-8 | Multi-family judging with reported agreement | The same dialogue is scorable by ≥3 evaluators of **distinct pretraining lineages**; harness reports per-dimension agreement and a consensus verdict. Headline results reportable under the most conservative evaluator. A configuration using two members of one family fails validation. | P1 |
| EVAL-9 | Structural validation is deterministic and LLM-free | Turn alternation, role validity, empty turns, turn-count bounds and repetition checked by code. Identical input yields identical output with no model call. | P1 |
| EVAL-10 | Claim verification is batched | All claims for a dialogue are verified in ≤2 structured calls with per-claim verdicts recovered from one response, validated against the claim list by count and index. Measured speed-up recorded. Closes D-12. | P0 |

### 7.4 Benchmarks and internal measurement — `BENCH`

| ID | Requirement | Acceptance criteria | Pri |
|----|-------------|---------------------|-----|
| BENCH-1 | A labelled error-injection benchmark exists | Given a clean dialogue and a reference, produces a corrupted copy plus ground-truth labels for: patient diagnosis leakage, patient treatment leakage, fabricated patient symptom, doctor hidden-fact leakage, unsupported doctor fact, role-order violation, empty turn. Seeded and reproducible. | P0 |
| BENCH-2 | Detector performance is reported per fault class | Precision, recall, F1 and AUC per corruption type with bootstrap intervals, plus a confusion matrix and localisation accuracy (was the fault flagged at the right turn?). | P0 |
| BENCH-3 | The benchmark is publicly releasable | Harness and fixtures are built from **synthetic** references only and contain no MIMIC-derived content. A test asserts no fixture references a real identifier. | P1 |
| BENCH-4 | Information retention is measured against coded ground truth | Clinical facts are extracted from each dialogue **in isolation** — the extractor's prompt provably excludes the reference — and matched against `DIAGNOSES_ICD` and `PRESCRIPTIONS` via the GRND matcher. Retention precision and recall reported per policy with case-clustered intervals. | P0 |
| BENCH-5 | Policy discriminability is measured | A held-out classifier recovers the disclosure policy from dialogue text alone; AUC and driving features reported. Train/test split is **by case**, so policy-triplets cannot leak across the split. High separability is reported as a leakage signal independent of the judge. | P1 |

### 7.5 Cohort selection — `COH`

| ID | Requirement | Acceptance criteria | Pri |
|----|-------------|---------------------|-----|
| COH-1 | Selection uses structured fields, not lexical matching | Exclusions computed from `ICUSTAYS`, `ADMISSIONS.hospital_expire_flag`, mechanical-ventilation procedure codes, admission type, length of stay, Charlson comorbidity from ICD-9 and high-acuity ICD-9 chapters. Closes D-09. | P0 |
| COH-2 | Selection requires no clinical review | The pipeline runs end to end with no human-in-the-loop step. No code path requires a reviewer file. Enforces C1. | P0 |
| COH-3 | Selection is deterministic and auditable | Same snapshot + criteria version reproduces the cohort exactly, verified by hash. A per-case audit record states which criteria fired. | P0 |
| COH-4 | An exclusion flow is reportable | Counts at each exclusion stage, suitable for a CONSORT-style figure. | P1 |
| COH-5 | One admission per patient | No `subject_id` appears twice in the final cohort; asserted by test. | P1 |
| COH-6 | The cohort is exactly 200 cases, sampled reproducibly | Candidate pool drawn, exclusions applied, 200 sampled with a recorded seed. Pool size, per-stage exclusion counts and seed are in the manifest. | P0 |

### 7.6 Reference grounding — `GRND` — **the project's sole external anchor**

| ID | Requirement | Acceptance criteria | Pri |
|----|-------------|---------------------|-----|
| GRND-1 | Extracted diagnoses compared to coded ground truth | Precision, recall and F1 against `DIAGNOSES_ICD` for the same admission, with case-clustered intervals, using the frozen matcher. | P0 |
| GRND-2 | Extracted medications compared to coded ground truth | Same, against `PRESCRIPTIONS`, with generic/brand normalisation. | P0 |
| GRND-3 | Cross-family extraction agreement is measured | The same notes extracted by ≥2 model families; agreement reported per field group with a chance-corrected statistic. | P0 |
| GRND-4 | The matcher is itself specified, validated and frozen | Normalisation and matching rules written down and version-stamped **before** use; evaluated on a hand-built fixture with known codes; its own error rate reported alongside every result depending on it. Changing a rule after seeing study output requires a new matcher version and a re-run. | P0 |

### 7.7 Experiment harness — `EXP`

| ID | Requirement | Acceptance criteria | Pri |
|----|-------------|---------------------|-----|
| EXP-1 | Experiments are declared as versioned configuration | A run is fully specified by a config file: variant, patient policy, doctor guidance setting, seed, turn budget, attempt budget, thresholds, model ids and digests, prompt versions. | P0 |
| EXP-2 | Runs are identified and hashed | Each run has a run ID and records `config_hash`, `input_manifest_hash`, `prompt_set_hash`, git commit, model digests and quantisation. Resume reuses a prior run only on a full hash match; a mismatch raises rather than mixing results. | P0 |
| EXP-3 | Attempt records are immutable and complete | Every attempt writes the §6.5 record. Records are append-only; a test asserts an existing record is never rewritten. | P0 |
| EXP-4 | Aggregation is separate from generation | Statistics computed by a distinct entry point reading attempt records. Re-running aggregation never triggers a model call; a test asserts zero provider calls during aggregation. | P0 |
| EXP-5 | Architecture variants are implemented and distinguishable | `direct_llm`, `structured_single_agent`, `basic_multi_agent`, `knowledge_controlled`, `full_meddial`. The runner refuses to start if a named variant has no distinct implementation, so no variant can be silently aliased to another. | P1 |
| EXP-6 | Thresholds and prompts are frozen before the confirmatory run | Calibration on a development split, then a freeze recorded in the manifest with a timestamp. Post-freeze changes force a new run ID. | P1 |
| EXP-7 | Model calls capture cost | Token counts and estimated cost per call, aggregated per variant, so cost-per-dialogue is reportable alongside quality. | P1 |
| EXP-8 | A control isolates patient from doctor conditioning | A configuration exists in which the patient policy varies while doctor guidance is held fixed, and vice versa. Reported alongside the main disclosure comparison. Closes D-05. | P0 |

### 7.8 Analysis and statistics — `STAT`

| ID | Requirement | Acceptance criteria | Pri |
|----|-------------|---------------------|-----|
| STAT-1 | Analysis respects the nested design | Dialogues are nested within cases (three policies per case). Intervals use case-level clustered bootstrap; policy comparisons are paired by case. Closes D-11. | P0 |
| STAT-2 | Ceiling proportions use appropriate intervals | Wilson or Clopper–Pearson, never normal approximation. | P0 |
| STAT-3 | Multiplicity is controlled | Comparison families declared in advance; adjustment method recorded and applied. | P1 |
| STAT-4 | Every manuscript number is generated, never transcribed | One command regenerates all tables and figures from attempt records into a versioned outputs directory. A manuscript statistic absent from that output is a defect. | P0 |
| STAT-5 | A power calculation precedes the confirmatory study | Effect sizes from a development pilot; the choice of 200 cases is **derived and documented** before the run, not asserted. | P0 |

### 7.9 Downstream signal and privacy — `DOWN`

| ID | Requirement | Acceptance criteria | Pri |
|----|-------------|---------------------|-----|
| DOWN-1 | Coded-fact recovery from dialogue is measured | A small model trained on MedDial dialogues to extract clinical facts, evaluated on **held-out cases** against `DIAGNOSES_ICD` and `PRESCRIPTIONS`, versus a no-synthetic control and a simple-synthetic control. Held-out cases are provably absent from every earlier stage. The manuscript states that the evaluation target is clinician-coded while the training signal is not. | P1 |
| DOWN-2 | Memorization is measured | Longest-common-substring and n-gram overlap distributions between dialogues and source notes; near-duplicate rate at a stated threshold; distribution reported, not just a mean. | P1 |
| DOWN-3 | Membership signal is probed | Overlap for source-linked versus unrelated notes, with the gap reported. | P2 |

### 7.10 Repository quality — `REPO`

| ID | Requirement | Acceptance criteria | Pri |
|----|-------------|---------------------|-----|
| REPO-1 | Clean-clone install works | Documented steps install the package and run the suite on a machine with no prior state, verified on a clean container. | P0 |
| REPO-2 | The README is accurate and hand-written | Every path, module and command it mentions exists. No generated content. Closes D-13. | P0 |
| REPO-3 | The scoring path is tested | Unit tests cover claim extraction, each metric, acceptance logic, fail-closed behaviour, degenerate inputs and the provider gate, with mocked providers. An integration test runs the pipeline end to end on synthetic fixtures with no network. | P0 |
| REPO-4 | CI enforces the guarantees | CI runs tests, the restricted-artifact guard and a secret scan on every push. | P1 |
| REPO-5 | Dependencies are pinned and resolvable | Lockfile or fully pinned requirements; the documented install reproduces the tested environment. | P1 |
| REPO-6 | Citation metadata exists | `CITATION.cff` with author, title, licence and the PhysioNet DOI once available. | P2 |

---

## 8. Non-functional requirements

| ID | Requirement | Target |
|---|---|---|
| NFR-1 | **Reproducibility** — identical config, seed and model digest reproduce identical outputs | Bit-identical attempt records for the deterministic path; documented tolerance for sampled generation |
| NFR-2 | **Auditability** — any number in the manuscript traceable to a record | ≤2 hops: statistic → aggregation output → attempt record |
| NFR-3 | **Fail-closed** — no metric silently defaults, no provider error becomes content | Zero default-valued scores in any aggregate |
| NFR-4 | **Throughput** — a full evaluation pass over 200 cases × 3 policies completes overnight on the target hardware | ≤12 h wall clock with batching and the chosen serving stack |
| NFR-5 | **Isolation** — development and testing require no MIMIC access and no network | Full suite green offline |
| NFR-6 | **Portability** — the benchmark and policy formalism are usable without MIMIC | BENCH-3 fixtures run standalone |
| NFR-7 | **Cost transparency** — token and time cost reportable per variant | Per-variant cost table in the aggregation output |
| NFR-8 | **Resumability** — an interrupted run resumes without recomputation or contamination | Resume on hash match; refuse on mismatch |

---

## 9. Metric definitions

Formal definitions. Ambiguity here becomes an unanswerable reviewer question later.

### 9.1 Role-aware factuality

For role *r* ∈ {patient, doctor} and reference mode *m*:

> **F_r,m = |supported factual claims by r| / |factual claims by r|**

- Factual claims are types `patient_fact` and `doctor_fact` only (§6.2). Hypotheses, recommendations, advice, questions and filler are excluded from both numerator and denominator.
- Support is judged against the reference selected by *m*.
- **If the denominator is 0, the score is `INCOMPLETE`, never 1.0** (EVAL-5).
- Reported separately per role and per reference mode. Never averaged into a single "faithfulness" figure without both subscripts stated.

### 9.2 Knowledge-boundary leakage

- A **leakage event** is a claim by role *r* referencing a field outside *r*'s permissible-knowledge set under the active policy.
- **Zero-leakage rate** = proportion of dialogues with zero events, reported per policy with case-clustered intervals.
- **Leakage rate** = events per dialogue, reported as a distribution.
- Under `FULL` the patient's permissible set is the whole reference, so patient leakage is 0 by construction. This must be stated, or the policy comparison looks better than it is.

### 9.3 Naturalness

- GEval-style continuous score in [0,1] over the full transcript.
- On scorer failure: `INCOMPLETE`. No fallback (EVAL-4).
- Deterministic settings: temperature ≤0.1, fixed seed, recorded.

### 9.4 Structural validity

Deterministic boolean conjunction, no model call: role alternation valid · no empty or whitespace-only turns · turn count within `[min_turns, max_turns]` · no exact-duplicate consecutive turns · repetition below threshold · no provider-error sentinel present in any turn.

### 9.5 Composite — reporting only

> composite = 0.4·naturalness + 0.3·profile_compliance + 0.3·faithfulness

Retained **solely** for continuity with the thesis. It is computed, reported, and explicitly excluded from acceptance (EVAL-7). Its definition is preserved unchanged even though its faithfulness term is now known to be role-partial, so that thesis and manuscript numbers remain comparable — and the manuscript says exactly that.

### 9.6 Retention

For each case, let *C* be the set of clinician-coded facts (ICD-9 diagnoses, prescribed medications) for that admission, and *E* the facts extracted from the dialogue alone.

> **retention_precision = |E ∩ C| / |E|**  ·  **retention_recall = |E ∩ C| / |C|**

Matching uses the frozen GRND matcher (GRND-4). Reported per policy with case-clustered intervals. The matcher's own measured error rate is reported alongside.

### 9.7 Detector performance

Per fault class: precision, recall, F1, AUC, and **localisation accuracy** — the proportion of detected faults flagged at the correct turn index. A detector that notices something is wrong but cannot say where is materially weaker, and reporting only F1 hides that.

---

## 10. Success criteria

The project is complete when all of the following hold.

1. A fresh clone contains no restricted data, installs cleanly, and passes its suite offline.
2. **E0 has been run** and the disclosure/faithfulness relationship is reported under both reference modes, over both roles, with corrected masking, and with the patient/doctor conditioning control — and the manuscript claim matches whatever result obtained.
3. Detector performance is reported per fault class against injected ground truth, with intervals and localisation accuracy.
4. Reference extraction is scored against `DIAGNOSES_ICD` and `PRESCRIPTIONS` with a **frozen, separately validated** matcher whose own error rate is reported; information retention is reported per policy.
5. Five architecture variants have been run under a frozen configuration, with quality and cost reported.
6. Every table and figure is regenerated by one command from immutable attempt records.
7. No sentence in the manuscript asserts clinical validity **or comparative realism**, and a Methods paragraph states the scope boundary explicitly.
8. A third party with MIMIC credentials can reproduce the headline result from the released configuration.

---

## 11. Out of scope

Dialogue quality improvements · prompt tuning aimed at raising scores · Hebrew or multilingual support · complex or high-acuity cases · real-time or interactive use · a web interface · model fine-tuning other than DOWN-1 · any form of human annotation · any comparison against real clinical conversation.

### 11.1 Claims the evidence cannot support

Listed so they can be audited out of the manuscript (W10):

- That the dialogues are clinically plausible, valid, or safe.
- That a physician would accept them as realistic.
- That they are indistinguishable from, comparable to, or of similar quality to real consultations — **hedged forms included**.
- That the doctor agent reasons soundly.
- That the corpus is suitable for training patient-facing systems, triage, or telemedicine.
- Any generalisation from MIMIC-III ICU-derived records to primary care.

---

## 12. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| E0 shows the disclosure trend was an artifact — of shrinking reference, incomplete masking, or doctor conditioning | The thesis's headline result disappears | Plan for it. Three confounds are identified and separately testable, so the analysis attributes the effect rather than merely losing it. The alternative paper — how reference scope, masking completeness and role conditioning each bias a widely used faithfulness measure — is legitimate and arguably stronger. No framing is written before E0 completes. |
| GRND is weak — coded diagnoses match extracted ones poorly because coding and documentation diverge | The project's only external anchor fails | Validate the matcher first (GRND-4) so a poor result is attributable to coding practice rather than to extraction. A low match rate that is *explained* is a reportable finding about EHR coding versus note content; an unexplained one is fatal. Decide the framing before the confirmatory run. |
| No local inference server | GOV-3 unsatisfiable; all restricted work blocked | Escalate at once. Fallback is documented zero-retention Azure under Limited Access approval, evidenced before any further run. |
| No GPU for the confirmatory run | M4 slips by weeks | Request access now — it has lead time. Fallbacks in order: reduce Phase B to two policies, drop the optional fourth judge, reduce turn budget 30→20. Reducing below 200 cases is last resort; D3 already sets the powered minimum. |
| Regeneration needed rather than re-scoring | 4–6 weeks added | Re-score first. Batch all corpus-invalidating changes — cohort criteria, masking correction, doctor-conditioning separation — so regeneration happens once. |
| History purge breaks clones or loses work | Lost work; broken supervisor clones | Verified backup and a tagged pre-purge archive before rewriting; warn everyone holding a clone. |
| Multi-family judging blocked by compute | EVAL-8 degraded | Use smaller models of genuinely distinct lineages rather than three sizes of one family; report the constraint honestly. |
| Scope creep into improving the generator | Confounds the measurement; burns the schedule | Non-goal in §2.2. Post-freeze generation changes force a new run ID, which makes the cost visible. |
| Corrected policies make v2.0 results incomparable to the thesis, and it reads as quietly changing the goalposts | Credibility | State the change explicitly and quantify it: the v1.0-vs-v2.0 gradient difference is reported as the size of the masking confound (KNOW-7), which turns the correction into a result rather than an unexplained discrepancy. |
| Quantisation or server version changes mid-study | Results not comparable across runs | Pin digest and quantisation in the manifest (EXP-2, C8); a mismatch on resume raises. |

---

## 13. Decisions — resolved 26 August 2026

| # | Decision | Resolution | Consequence |
|---|----------|------------|-------------|
| **D1** | Port from `main` or rebuild on `new`? | **Use `main` as reference; rebuild on `new`.** | No code ported. Effort is the build case (~17–19 weeks part-time). Upside: `main`'s clinician-dependent cohort module is not inherited, and every module lands with tests written against it. |
| **D2** | Local inference server and models | **Local serving; model stack per role.** Implementation Plan Appendix A. | GOV-3 satisfiable. Introduces C6, which reshapes E0 and the confirmatory run. |
| **D3** | Cohort size | **200 cases, powered.** | COH-6. STAT-5 promoted to P0 — the number must be derived. |
| **D4** | External corpus | **None.** No external dialogue corpus at all. | BENCH-4/5 and DOWN-1 become internal measures. **GRND becomes the sole external anchor** and is promoted to P0 throughout. See §13.1. |
| **D5** | First submission target | **TMLR first, CMPB second.** | TMLR's criterion is that claims match evidence, which an internally-anchored measurement paper satisfies; CMPB's readership asks "compared to what?". Draft to TMLR's unlimited length, cut for CMPB. REPO-1 to REPO-6 remain critical for the resubmission. |

### 13.1 What removing the external corpus costs

| Removed | Replaced by | Genuinely lost |
|---|---|---|
| Comparison against real dialogues | BENCH-4 information retention | Any claim that the dialogues resemble real clinical conversation. Nothing internal can establish this. |
| Real-vs-synthetic discrimination | BENCH-5 policy discriminability | An external realism check. What remains is a useful internal diagnostic. |
| Utility on an external benchmark | DOWN-1 coded-fact recovery | The strongest argument that the corpus is useful. The replacement is weaker and must be presented as such. |

**GRND is now load-bearing.** `DIAGNOSES_ICD` and `PRESCRIPTIONS` are coded by hospital clinical coders — real human judgement, recorded during real care, already inside the credentialed environment. They are the only human-authored ground truth remaining, which is why GRND-1 to GRND-4 are all P0 and why the matcher must be frozen before it is used to judge anything.

**A reporting obligation created by the masking fix.** Correcting KNOW-5 means the v2.0 disclosure policies are not the thesis's policies — medications are dropped under both no-diagnosis levels, where the thesis retained them. v2.0 numbers are therefore **not directly comparable** to published thesis numbers, and the manuscript must say so rather than presenting a quiet improvement. Retaining the v1.0 definitions as a deprecated comparison arm (KNOW-7) turns that liability into a measured quantity: the difference between the two gradients *is* the size of the masking confound.

**Deliberately rejected:** using the source MIMIC note as a downstream reconstruction target. The dialogue was generated *from* that note, so reconstruction measures lossy round-tripping through the pipeline. It resembles an external anchor and is not one.

---

## 14. Traceability

Defect → requirement → workstream → milestone. Workstream and milestone IDs are defined in the Implementation Plan.

| Defect | Requirements | Workstream | Milestone |
|---|---|---|---|
| D-01 restricted data public | GOV-1, GOV-2 | W0 | M0 |
| D-02 doctor turns unevaluated | EVAL-1 | W3 | M1 |
| D-03 reference shrinks with policy | EVAL-2 | W3 | M1 |
| D-04 masking incomplete | KNOW-5, KNOW-7 | W2 | M1 |
| D-05 policy rewrites doctor prompt | KNOW-6, EXP-8 | W2, W7 | M1, M4 |
| D-06 silent fallback | EVAL-3, EVAL-4 | W3 | M1 |
| D-07 empty dialogue scores 1.0 | EVAL-5 | W3 | M1 |
| D-08 errors become dialogue text | GOV-3 | W1 | M1 |
| D-09 lexical cohort | COH-1 | W5 | M3 |
| D-10 unreproducible runs | EXP-1 to EXP-4 | W7 | M4 |
| D-11 nested design ignored | STAT-1 | W8 | M4 |
| D-12 throughput | EVAL-10 | W3 | M1 |
| D-13 repo quality | GOV-7, REPO-1 to REPO-6 | W0 | M0, M6 |

### 14.1 Requirement → manuscript claim

Which reported claim each requirement group underwrites. A requirement with no downstream claim is scope creep; a claim with no upstream requirement is unsupported.

| Requirement group | Manuscript claim it supports |
|---|---|
| BENCH-1, BENCH-2 | "The leakage detector achieves *x* recall at *y* precision on injected faults, localised to the correct turn in *z*% of cases." |
| EVAL-1, EVAL-2 + E0 | "The reported disclosure–faithfulness relationship is / is not attributable to reference scope, turn scope, masking completeness and role conditioning." |
| EVAL-6, BENCH-5 | "Knowledge-boundary leakage occurs at rate *r* under policy *p*, corroborated by an independent classifier." |
| GRND-1 to GRND-4 | "Extracted references agree with clinician-coded diagnoses and prescriptions at *p*/*r*, using a matcher whose own error rate is *e*." |
| BENCH-4 | "Clinical content recoverable from the dialogue alone falls from *a* to *b* as information is withheld." |
| EXP-5, EXP-7 | "The multi-agent architecture yields Δ over a single-prompt baseline at *n*× the token cost." |
| DOWN-1 | "A model trained on this corpus recovers clinician-coded facts on held-out cases at Δ over controls." |
| DOWN-2, DOWN-3 | "Near-duplicate rate against source notes is *d*; membership signal is *m*." |
| STAT-1 to STAT-5 | Every interval and comparison in the paper. |
| GOV, REPO | Data-availability and code-availability statements. |
