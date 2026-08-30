# MedDial — Implementation Plan

| | |
|---|---|
| **Companion to** | `MedDial_PRD.md` v2.0 — all `GOV-`/`EVAL-`/`GRND-` etc. IDs refer to it |
| **Baseline** | branch `new` @ `61156f7` — 5,514 LOC Python, no tests, no configs, no licence |
| **Version** | 2.0 — full specification |
| **Date** | 26 August 2026 |
| **Effort** | 17–19 weeks part-time across 11 workstreams and 7 milestones |

**Notation.** `Wn` = workstream · `Mn` = milestone · `E0`–`E8` = experiments (matching the strategy document; note `EXP-n` IDs are *requirements*, not experiments) · *[ref: `main`]* = branch `main` has an implementation worth reading before writing your own.

---

## 0. Decisions and their consequences

| # | Resolution | Effect on this plan |
|---|-----------|---------------------|
| **D1** | `main` as **reference**, rebuild on `new` | No porting. Read `main`'s implementation, then write your own with a test first. Schedule is the build case. |
| **D2** | Local serving, Ollama / vLLM | GOV-3 satisfied. **Appendix A** = model per role; **Appendix B** = throughput, now a first-order constraint. |
| **D3** | 200-case powered cohort | W5 samples 200 from a pool with a recorded seed. STAT-5 is P0 — derive the number. |
| **D4** | **No external corpus** | W4 loses meta-evaluation, gains retention and discriminability. **W6 moves onto the critical path** as the sole external anchor. `Utils/mts_dialog_loader.py` becomes dead code. |
| **D5** | **TMLR first, CMPB second** | Draft to TMLR's unlimited length, then cut. Repository quality still matters for the CMPB resubmission. |

### 0.1 Four things to internalise before writing code

**Rebuilding means tests come first.** The reason to rebuild rather than port is to know what the code does. That only pays off if each module lands with the test that would have caught the corresponding defect in `new` — empty dialogue scoring 1.0, provider errors entering transcripts, masking that doesn't mask. Read `main` for the design, write the test, then the module.

**W6 is now load-bearing.** With no external corpus, `DIAGNOSES_ICD` and `PRESCRIPTIONS` are the only human-authored ground truth in the project. The normalisation and matching rules carry the paper's external validity by themselves, and must be specified and validated *before* they judge anything (GRND-4). Start W6 early; give it more time than its size suggests.

**Throughput is a design constraint.** `_is_statement_faithful` runs once per claim: ~18 model calls per dialogue per reference mode, and the confirmatory run is ~170,000 calls. Batching (EVAL-10) is roughly an 8× reduction and is a prerequisite for E0.

**Nothing is written before E0.** E0 determines what the paper claims. Every hour spent on framing before it completes is at risk.

---

## 1. Current architecture

```
gtmf_creation.py (494)               ─┐
dialogue_generation_framework.py(515) │  three top-level scripts, no shared config
simulation.py (447)                  ─┘

Agents/
  DeepEvalJudgeAgent.py    (756)  patient turns only; faithfulness vs policy context;
                                  silent fallback; no provenance; empty → 1.0
  PatientAgent.py          (473)  temp 0.6, max_tokens 300
  DoctorAgent.py           (337)  temp 0.5; _consultation_guidance branches on profile_type
  PromptImprovementAgent.py(267)  broad prompt rewriting, not targeted repair
  EHRSummarizerAgent.py           bias-aware note summarisation
  JudgeAgent.py            (246)  legacy, unused — delete
Models/classes.py                 GTMF Pydantic models; no provenance fields
Utils/
  llms_utils.py             (96)  one hardcoded AzureAIFoundryClient;
                                  errors RETURN "[ERROR: …]" strings instead of raising
  partial_profile.py              policy = clearing TWO dict keys, inline;
                                  Context_Fields (medications, history) survive masking
  markdown_gtmf.py         (561)
  mts_dialog_loader.py            dead under D4 — delete
  bias_aware_prompts.py    (236)
  conversation_variety.py  (415)
  repetition_filter.py     (135)  reusable in structural.py
agent_prompts/*.txt               7 prompt files, unversioned
```

### 1.1 Six structural problems

1. **No provider abstraction.** `load_gpt_model()` builds an Azure client at six call sites. Nowhere to enforce a classification gate (GOV-3), capture call metadata (EXP-7), or swap in a local model (D2).
2. **Errors become dialogue text.** `chat_generate` returns `"[ERROR: Rate limit exceeded after retries]"` as a string. It can enter a transcript and be scored as an utterance. Fail-closed (EVAL-4) is impossible until this raises instead.
3. **Policy is implicit.** Masking is dictionary-key deletion inside `generate_partial_profiles`. Nothing declares what each role may know, so a role-aware evaluator has nothing to evaluate against.
4. **Reference is conflated with policy.** `_build_faithfulness_context` takes `profile_type`, so the reference shrinks with the policy — the confound at the centre of the PRD. Making the reference selectable (EVAL-2) is the highest-value single change in this plan.
5. **Masking is incomplete and the policy touches both agents.** Only `Core_Fields.Diagnoses` and `Core_Fields.Treatment_Options` are cleared; `Current_Medications`, `Discharge_Medications` (each `Medication` carries `purpose`) and `Past_Medical_History` survive. A medication whose purpose reads "for atrial fibrillation" hands over the diagnosis under the policy meant to withhold it — and compliance cannot flag it, because the field is legitimately permitted. Meanwhile `DoctorAgent._consultation_guidance` branches on `profile_type`.
6. **Runs are not records.** Markdown plus two aggregate JSON files. No manifest, config hash, seed, or per-attempt record, so nothing is reproducible or auditable.

---

## 2. Target architecture

New code lands under `meddial/`. `Agents/`, `Models/`, `Utils/` stay importable during migration and are retired at M4.

```
meddial/
  llm/
    provider.py        LLMProvider protocol · ChatMessage · CompletionResult · CallMetadata
    classification.py  DataClassification · ensure_provider_compatible()      → GOV-3
    local_openai.py    LocalOpenAICompatibleProvider  (RESTRICTED_CLINICAL ok)
    azure.py           AzureProvider  (PUBLIC/SYNTHETIC only unless attested)
    mock.py            MockProvider — deterministic, for tests                → REPO-3
    errors.py          ProviderError hierarchy (never returned as text)
  knowledge/
    reference.py       StructuredClinicalReference · EvidenceSpan             → KNOW-1
    policy.py          KnowledgePolicy · FieldPath · PolicyRegistry           → KNOW-2/4/5
    contexts.py        PatientContext · DoctorContext · EvaluatorContext      → KNOW-3/6
    redaction.py       diagnosis-term redaction for permitted fields          → KNOW-5
  cohort/
    criteria.py        versioned structured exclusion criteria                → COH-1
    select.py          deterministic selection · audit records · sampling     → COH-2/3/5/6
    manifest.py        private + salted release manifests                     → GOV-6
  evaluation/
    claims.py          role-tagged, typed claim extraction                    → §6.2
    faithfulness.py    role-aware · reference-mode selectable · batched       → EVAL-1/2/10
    boundary.py        leakage events and rates                               → EVAL-6
    naturalness.py     GEval wrapper, raises on failure                       → EVAL-4
    structural.py      deterministic, LLM-free                                → EVAL-9
    provenance.py      ScoreProvenance — required on every score              → EVAL-3
    acceptance.py      per-dimension gates · INCOMPLETE as first class        → EVAL-5/7
    ensemble.py        multi-family judging · agreement                       → EVAL-8
    models.py          Score · EvaluationResult · EvaluationStatus
  benchmarks/
    injection.py       seeded corruption + ground-truth labels                → BENCH-1
    detector_eval.py   P/R/F1/AUC + localisation per fault class              → BENCH-2
    retention.py       dialogue-only extraction vs coded facts                → BENCH-4
    discriminate.py    policy-recovery classifier                             → BENCH-5
    fixtures/          synthetic references — publicly releasable             → BENCH-3
  grounding/
    normalise.py       frozen, versioned normalisation rules                  → GRND-4
    structured_match.py  vs DIAGNOSES_ICD / PRESCRIPTIONS                     → GRND-1/2
    agreement.py       cross-family extraction agreement                      → GRND-3
  experiments/
    config.py          versioned config + hashing                             → EXP-1/2
    variants.py        five architectures, distinct implementations           → EXP-5
    runner.py          orchestration · immutable attempt records              → EXP-3
    repair.py          targeted repair keyed to the failed dimension
    aggregate.py       separate aggregation pass                              → EXP-4
  analysis/
    stats.py           clustered bootstrap · paired tests · multiplicity      → STAT-1/2/3
    tables.py          regenerate every manuscript table and figure           → STAT-4
    power.py           power calculation from the pilot                       → STAT-5
  cli.py               meddial-cohort · meddial-run · meddial-analyze · meddial-bench
tests/{unit,integration,property,fixtures}/                                   → REPO-3
configs/{experiments,policies,matchers}/                                      → EXP-1, KNOW-4, GRND-4
docs/  LICENSE  CITATION.cff  README.md                                       → GOV-7, REPO-2/6
```

---

## 3. Core interfaces

Copy these signatures; they are the contracts the workstreams below assume.

### 3.1 Provider layer (W1)

```python
class DataClassification(str, Enum):
    PUBLIC = "public"
    SYNTHETIC = "synthetic"
    RESTRICTED_CLINICAL = "restricted_clinical"

@dataclass(frozen=True)
class CallMetadata:
    model_id: str; model_digest: str; model_family: str; quantisation: str
    temperature: float; top_p: float; seed: int | None
    prompt_tokens: int; completion_tokens: int; latency_ms: int
    provider_class: str; classification: DataClassification

@dataclass(frozen=True)
class CompletionResult:
    text: str
    metadata: CallMetadata

class LLMProvider(Protocol):
    @property
    def approved_classifications(self) -> frozenset[DataClassification]: ...
    @property
    def model_family(self) -> str: ...
    def complete(self, messages: Sequence[ChatMessage], *,
                 classification: DataClassification,
                 temperature: float, max_tokens: int,
                 seed: int | None = None) -> CompletionResult: ...

def ensure_provider_compatible(p: LLMProvider, c: DataClassification) -> None:
    """Raise ProviderClassificationError BEFORE any network I/O."""
```

**Error semantics change (closes D-08):** every current `return "[ERROR: …]"` becomes `raise ProviderError(...)`. Callers handle it or let it propagate; nothing turns an error into content.

### 3.2 Knowledge layer (W2)

```python
FieldPath = str  # dotted: "core.diagnoses", "context.current_medications[].purpose"

@dataclass(frozen=True)
class KnowledgePolicy:
    policy_id: str            # FULL | NO_DIAGNOSIS | NO_DIAGNOSIS_NO_TREATMENT
    version: str              # bump on any change (KNOW-4)
    patient_visible: frozenset[FieldPath]
    doctor_visible: frozenset[FieldPath]
    redact_diagnosis_terms_in: frozenset[FieldPath]   # KNOW-5
    def mask(self, ref: SCR, role: Role) -> Mapping[str, Any]: ...

@dataclass(frozen=True)
class PatientContext:  policy: KnowledgePolicy; visible: Mapping[str, Any]
@dataclass(frozen=True)
class DoctorContext:   guidance_id: str; visible: Mapping[str, Any]   # KNOW-6: independent
@dataclass(frozen=True)
class EvaluatorContext: reference: SCR; policy: KnowledgePolicy       # privileged
```

**KNOW-6 in practice:** `DoctorContext.guidance_id` is set by configuration, not derived from `policy_id`. The default configuration reproduces current behaviour (`guidance_id == policy_id`) so the thesis result stays comparable; EXP-8's control pins `guidance_id` to a constant while the patient policy varies.

### 3.3 Evaluation layer (W3)

```python
class ClaimType(str, Enum):
    PATIENT_FACT="patient_fact"; DOCTOR_FACT="doctor_fact"; QUESTION="question"
    DIAGNOSTIC_HYPOTHESIS="diagnostic_hypothesis"; RECOMMENDATION="recommendation"
    ADVICE="advice"; NON_MEDICAL="non_medical"

FACTUAL = {ClaimType.PATIENT_FACT, ClaimType.DOCTOR_FACT}   # only these score faithfulness

class ReferenceMode(str, Enum):
    POLICY_CONTEXT="policy_context"; FULL_REFERENCE="full_reference"

class EvaluationStatus(str, Enum):
    PASS="pass"; FAIL="fail"; INCOMPLETE="incomplete"

@dataclass(frozen=True)
class Score:
    value: float | None            # None iff status is INCOMPLETE
    status: EvaluationStatus
    provenance: ScoreProvenance    # required — no default (EVAL-3)
    detail: Mapping[str, Any]

def score_faithfulness(claims, ctx: EvaluatorContext, *, role: Role,
                       reference_mode: ReferenceMode, provider) -> Score:
    """EVAL-1/2/10. Batched verification. Empty factual set → INCOMPLETE (EVAL-5)."""
```

### 3.4 Experiment layer (W7)

```python
@dataclass(frozen=True)
class RunConfig:
    name: str; variant: str
    patient_policy_id: str; doctor_guidance_id: str      # KNOW-6 / EXP-8
    reference_mode: ReferenceMode
    seed: int; max_turns: int; max_attempts: int
    thresholds: Mapping[str, float]
    models: Mapping[str, ModelSpec]                      # role → id+digest+quant
    prompt_versions: Mapping[str, str]
    frozen_at: str | None                                # EXP-6
    def config_hash(self) -> str: ...
```

---

## 4. Data formats

### 4.1 Run manifest

```json
{
  "run_id": "run_2026-09-14_a3f21c",
  "created_utc": "2026-09-14T08:02:11Z",
  "git_commit": "…", "config_hash": "…",
  "input_manifest_hash": "…", "prompt_set_hash": "…",
  "frozen_at": "2026-09-12T17:40:00Z",
  "config": { "...": "full RunConfig" },
  "models": {
    "patient": {"id":"mistral-small3.2:24b","digest":"sha256:…","quant":"Q4_K_M","family":"mistral"},
    "judge_1": {"id":"qwen3.5:32b","digest":"sha256:…","quant":"Q4_K_M","family":"qwen"}
  },
  "provider": {"class":"LocalOpenAICompatibleProvider","host":"localhost:11434",
               "attestation":"local inference; no third-party retention"},
  "environment": {"python":"3.11.9","package_versions":{"…":"…"},"server":"vllm 0.x"},
  "cohort": {"manifest_hash":"…","n_cases":200,"sampling_seed":20260914}
}
```

### 4.2 Attempt record (abridged)

```json
{
  "dialogue_id": "case0042_NO_DIAGNOSIS_a1", "run_id": "run_…", "attempt_index": 1,
  "inputs": {"scr_hash":"…","policy":{"id":"NO_DIAGNOSIS","version":"2.0"},
             "doctor_guidance_id":"NEUTRAL","seed":20260914,"prompt_versions":{"…":"…"}},
  "dialogue": [{"index":0,"role":"Doctor","text":"…"}],
  "evaluation": {
    "scores": {
      "patient_factuality": {"value":0.86,"status":"pass",
        "provenance":{"scorer_id":"faithfulness.v2","model_family":"qwen",
                      "model_id":"qwen3.5:32b","model_digest":"sha256:…",
                      "quantisation":"Q4_K_M","reference_mode":"full_reference",
                      "turn_scope":"patient","prompt_version":"…",
                      "sampling":{"temperature":0.1,"seed":7},"fallback_used":false},
        "detail":{"claims_total":14,"claims_supported":12}},
      "doctor_factuality": {"value":0.91,"status":"pass","provenance":{"...":"..."}},
      "knowledge_boundary": {"value":1.0,"status":"pass","provenance":{"...":"..."}},
      "naturalness": {"value":0.88,"status":"pass","provenance":{"...":"..."}},
      "structural_validity": {"value":1.0,"status":"pass","provenance":{"...":"..."}}
    },
    "leakage_events": [],
    "acceptance": {"overall":"ACCEPT","per_dimension":{"...":"pass"}},
    "composite": {"value":0.89,"note":"reporting only; not used for acceptance"}
  },
  "cost": {"calls":57,"prompt_tokens":41230,"completion_tokens":6120,"wall_ms":48210}
}
```

### 4.3 Policy definitions

**Settled:** under any no-diagnosis policy, `context.current_medications` and `context.discharge_medications` are **dropped entirely** — not retained with `purpose` redacted. Cleaner, fully testable, and nothing depends on a fallible redaction step for the field that leaks hardest.

Two consequences follow, and both matter.

**Medications must drop under `NO_DIAGNOSIS` too, not only under `NO_DIAGNOSIS_NO_TREATMENT`.** The leak is `Medication.purpose` naming the condition, and that field is just as present under `NO_DIAGNOSIS`. Dropping it only at the strictest level would leave the middle policy leaking, which is precisely the confound E0 is trying to eliminate. The ordering is preserved — NDNT remains strictly more restrictive, because it additionally masks `core.treatments`, which carries its own nested medication list.

**Past medical history is a different case and is handled differently.** A real patient does know their prior conditions, and prior conditions are not the index diagnosis. Dropping the field outright would damage realism and depress naturalness for no gain. The default below therefore **keeps** `past_medical_history` and redacts only terms matching that case's masked index-diagnosis set — which is exactly what the KNOW-5 test already checks. `meddial/knowledge/redaction.py` exists for this and this alone. Override in the policy file if you would rather drop it.

```json
{
  "policy_id": "NO_DIAGNOSIS", "version": "2.0",
  "patient_visible": ["core.symptoms", "core.treatments",
                      "context.demographics", "context.allergies",
                      "context.medical_history.past_medical_history",
                      "additional.chief_complaint"],
  "patient_masked":  ["core.diagnoses",
                      "context.current_medications",
                      "context.discharge_medications"],
  "redact_index_diagnosis_terms_in": ["context.medical_history.past_medical_history"],
  "doctor_visible":  ["context.demographics"],
  "rationale": "KNOW-5. Medication.purpose names the condition, so medications disclose the
                diagnosis and are dropped. Past medical history is retained — patients know
                their history — with index-diagnosis terms redacted."
}
```

```json
{
  "policy_id": "NO_DIAGNOSIS_NO_TREATMENT", "version": "2.0",
  "patient_visible": ["core.symptoms", "context.demographics", "context.allergies",
                      "context.medical_history.past_medical_history",
                      "additional.chief_complaint"],
  "patient_masked":  ["core.diagnoses", "core.treatments",
                      "context.current_medications",
                      "context.discharge_medications"],
  "redact_index_diagnosis_terms_in": ["context.medical_history.past_medical_history"],
  "doctor_visible":  ["context.demographics"],
  "rationale": "As NO_DIAGNOSIS, plus core.treatments — which carries its own nested
                medication list, so masking it closes the last medication route."
}
```

#### 4.3.1 Keep the thesis policies as a comparison arm (KNOW-7)

The v2.0 policies above are **not** the thesis's policies, so v2.0 numbers are not directly comparable to published thesis numbers. That is a reporting obligation, not just a caveat — the manuscript must state it plainly.

It is also an opportunity. Register the original definitions as `NO_DIAGNOSIS@1.0` and `NO_DIAGNOSIS_NO_TREATMENT@1.0` — masking `core.diagnoses` and `core.treatments` only — and keep them runnable:

```json
{
  "policy_id": "NO_DIAGNOSIS", "version": "1.0",
  "patient_masked": ["core.diagnoses"],
  "deprecated": true,
  "rationale": "Thesis-compatible. Leaks the diagnosis via Medication.purpose and
                discharge medications. Retained solely as an E0 comparison arm."
}
```

This turns E0's third confound from an estimate into a **direct A/B**: run the same cases under v1.0 and v2.0 policies and read off exactly how much of the disclosure gradient was masking leakage. It costs one extra scoring pass and produces a cleanly attributable number, which is worth far more in the paper than a bound.

A deprecated policy is refused by the confirmatory runner (KNOW-7, EXP-6) and permitted only in an E0 configuration, so it cannot leak into the headline results.

---

## 5. Workstreams

Each states goal, requirements, files, work, tests and a done-checklist. Effort is part-time developer-weeks.

### W0 · Compliance and repository hygiene — 3–4 days · **blocks everything**

**Satisfies:** GOV-1, GOV-2, GOV-5, GOV-6, GOV-7, REPO-1, REPO-2, REPO-4, REPO-5, REPO-6

| # | Task |
|---|---|
| 1 | **Set the repository private today.** Independent of everything else. |
| 2 | Tag and back up every branch. Verify the backup restores from scratch. |
| 3 | Remove `gtmf/`, `output_dialogue_framework/`, `MTS-Dialog/MTS-Dialog.csv`. Under D4 the corpus copy is deleted outright, along with `Utils/mts_dialog_loader.py` and its call sites. |
| 4 | Purge those paths from history across **all** refs. This invalidates every existing clone, including supervisors'. Warn them first. |
| 5 | Harden `.gitignore`; add the CI guard rejecting restricted-artifact paths. |
| 6 | Secret scan across history; rotate `AZURE_AI_API_KEY` regardless of result. |
| 7 | Add `LICENSE` (MIT or Apache-2.0), `CITATION.cff`, hand-written `README.md` describing only what exists. |
| 8 | Move to `pyproject.toml` with a lockfile; pin every dependency. |
| 9 | Delete `Agents/JudgeAgent.py` (legacy, unused). |

**Tests:** `test_ci_guard_rejects_restricted_paths` · `test_release_manifest_has_no_identifiers` · clean-container install smoke test.

**Done when:** a fresh clone of every branch has no restricted paths, installs from the documented steps on a clean machine, and a deliberate restricted-path commit is rejected by CI.

---

### W1 · Provider and compliance layer — 1 week
**Satisfies:** GOV-3, GOV-4, EXP-7 · unblocks EVAL-4 · *[ref: `main` `meddial/llm.py`]*

- Implement the §3.1 protocol, `DataClassification`, and `ensure_provider_compatible` raising **before** any I/O.
- `LocalOpenAICompatibleProvider` (Ollama/vLLM), `AzureProvider`, `MockProvider`.
- **Change error semantics in one pass**: every `return "[ERROR: …]"` becomes a raise. Expect fallout at all six `load_gpt_model` call sites — `PatientAgent:17`, `DoctorAgent:13`, `DeepEvalJudgeAgent:90`, `PromptImprovementAgent:29`, `EHRSummarizerAgent:28`, and the deleted `JudgeAgent:29`.
- Inject providers by constructor; delete `load_gpt_model()`.
- Capture `CallMetadata` on every call, including digest and quantisation (C8).

**Tests:** `test_restricted_call_to_azure_raises_before_network` (mock transport asserts zero requests) · `test_provider_error_raises_not_returns_string` · `test_call_metadata_records_digest_and_quant` · `test_mock_provider_is_deterministic`.

**Done when:** no code path can send `RESTRICTED_CLINICAL` to an unapproved provider, and no provider failure can produce a string that reaches a transcript.

---

### W2 · Knowledge model and policies — 1.5 weeks
**Satisfies:** KNOW-1 to KNOW-7

- Extend `Models/classes.py` into `StructuredClinicalReference` with `EvidenceSpan` per entity. Keep `GTMF` as an alias so `Utils/markdown_gtmf.py` keeps working.
- Replace `Utils/partial_profile.py` with `KnowledgePolicy` (§3.2), policies as JSON in `configs/policies/` (§4.3).
- **Masking audit (KNOW-5).** Map every reference field to what it discloses. Per §4.3: **drop** `context.current_medications` and `context.discharge_medications` under **both** no-diagnosis policies; **keep** `past_medical_history` with index-diagnosis terms redacted via `redaction.py`. Register the thesis-compatible v1.0 policies alongside, marked `deprecated`, for the E0 A/B.
- **Separate conditioning (KNOW-6).** Make `DoctorContext.guidance_id` configuration-driven rather than derived from `profile_type`; refactor `DoctorAgent._consultation_guidance` to take it as a parameter.
- Build the three role contexts.

**Tests:** `test_masked_fields_unreachable_from_patient_context` (property, all policies × all fields) · `test_no_diagnosis_policy_leaks_no_diagnosis_terms` (for every case, no permitted patient field contains a term from that case's masked index-diagnosis set) · `test_medications_dropped_under_both_no_diagnosis_policies` · `test_ndnt_strictly_more_restrictive_than_no_diagnosis` (v2.0 NDNT's visible set is a proper subset) · `test_past_history_retained_but_redacted` · `test_deprecated_policy_refused_by_confirmatory_runner` · `test_policy_version_bump_required_on_change` · `test_doctor_guidance_independent_of_patient_policy` · `test_evidence_survives_roundtrip`.

**Done when:** a policy can be added by writing a JSON file, and the leak test passes on the full cohort.

---

### W3 · Evaluation rebuild — 2.5 weeks · **critical path**
**Satisfies:** EVAL-1 to EVAL-7, EVAL-9, EVAL-10 · *[ref: `main` `meddial/evaluation/`]*

Order matters: **E0 needs only items 1–3 and 8.**

| # | Module | Work |
|---|---|---|
| 1 | `claims.py` | Extract atomic claims from **all** turns, tagged with role, turn index and `ClaimType`. Only `FACTUAL` types enter faithfulness (§6.2) — a `diagnostic_hypothesis` must never be scored as a hallucination. |
| 2 | `faithfulness.py` | Score patient and doctor claims separately (EVAL-1) against a selectable reference (EVAL-2). Empty factual set → `INCOMPLETE` (EVAL-5). |
| 3 | `provenance.py` | `ScoreProvenance` required on every `Score`; construction without it raises (EVAL-3). |
| 4 | `naturalness.py` | GEval wrapper that raises on failure. **Delete the fallback scorer** — do not make it quieter (EVAL-4). |
| 5 | `boundary.py` | Leakage *events*, not just a rate (EVAL-6). |
| 6 | `structural.py` | Deterministic: alternation, empty turns, turn bounds, repetition (reuse `Utils/repetition_filter.py`), provider-error sentinel detection (EVAL-9). |
| 7 | `acceptance.py` | Per-dimension gates; `INCOMPLETE` first class; composite computed but non-binding (EVAL-5/7). |
| 8 | `faithfulness.py` | **Batch verification (EVAL-10)** — one structured call returning a verdict array for all claims, validated by count and index against the claim list. Do this *before* E0. |
| 9 | `ensemble.py` | ≥3 evaluator families, agreement, consensus (EVAL-8). Deferrable to W3b after E0. |

**Tests:** `test_fabricated_doctor_diagnosis_fails_doctor_factuality_only` · `test_empty_dialogue_returns_incomplete` · `test_raising_scorer_returns_incomplete_not_default` · `test_both_reference_modes_recorded_distinctly` · `test_diagnostic_hypothesis_not_scored_as_hallucination` · `test_batched_verdicts_align_with_claim_indices` · `test_score_without_provenance_raises` · `test_structural_detects_error_sentinel`.

**Done when:** every test above passes and a batched pass over 100 dialogues is ≥5× faster than the per-claim path.

---

### W4 · Benchmarks and internal measurement — 2 weeks
**Satisfies:** BENCH-1 to BENCH-5 · *[ref: `main` `meddial/benchmarks.py`]*

- `injection.py`: seeded corruption for seven fault classes, emitting `InjectedError(corruption_type, turn_index, label, expected_detector)`.
- `detector_eval.py`: clean/corrupted pairs → per-class precision, recall, F1, AUC, confusion matrix, **and localisation accuracy** (§9.7), with bootstrap intervals. **Under D4 this is the paper's primary validation result.**
- `fixtures/`: built from **synthetic** references only so BENCH-3 holds and the benchmark ships publicly. This is the project's main reusable artifact — build it to be used by strangers.
- `retention.py` (BENCH-4): extract clinical facts from each dialogue **in isolation**, match via W6's frozen matcher, report retention precision/recall per policy with case-clustered intervals. Use a **different model family from the generator**, or retention inherits the generator's blind spots.
- `discriminate.py` (BENCH-5): classifier recovering the policy from dialogue text; AUC plus driving features. Split **by case**, so policy triplets cannot leak across the split.

**Tests:** `test_injected_fault_detected_at_expected_turn` · `test_retention_extractor_prompt_excludes_reference` (asserts the reference string is absent from the rendered prompt) · `test_discriminator_split_is_by_case` · `test_fixtures_contain_no_real_identifiers`.

**Done when:** detector performance is reported per class with intervals, and the fixture set runs standalone with no MIMIC access.

---

### W5 · Cohort rebuild — 1 week
**Satisfies:** COH-1 to COH-6

- SQL exclusion criteria (Appendix C) against `ICUSTAYS`, `ADMISSIONS`, `PROCEDURES_ICD`, `DIAGNOSES_ICD`; Charlson from ICD-9; one admission per `subject_id`.
- Version the criteria set; per-case audit record naming which criteria fired; per-stage counts for a CONSORT-style figure.
- **Under D3:** draw a candidate pool, apply exclusions, sample exactly 200 with a recorded seed (COH-6). Run the STAT-5 power calculation on pilot effect sizes *before* fixing the number and record the derivation.
- Remove every clinical-reviewer code path (C1). `main`'s `meddial/cohort.py` requires two clinician reviews and must be **replaced, not referenced**.

**Tests:** `test_cohort_hash_stable_across_runs` · `test_appendix_a_case_is_excluded` (subject 10446, post-CABG sternal wound drainage, URGENT) · `test_one_admission_per_subject` · `test_no_reviewer_file_required`.

**Done when:** the cohort reproduces by hash and the exclusion flow is reportable.

---

### W6 · Reference grounding — **the external anchor** — 1.5 weeks · **critical path**
**Satisfies:** GRND-1 to GRND-4

With no external corpus this is the only place real human judgement enters the project — hospital coders assigned those ICD-9 codes and prescriptions during actual care.

- **Specify the matcher before using it (GRND-4).** Write down and version-stamp: ICD-9 description normalisation, synonym and abbreviation handling, generic↔brand drug mapping, and what counts as a match at what granularity (exact code · three-digit category · description overlap threshold). Freeze in `configs/matchers/`.
- Evaluate the matcher on a hand-built fixture with known codes; **report its own error rate** alongside every result depending on it.
- `structured_match.py`: diagnoses vs `DIAGNOSES_ICD`, medications vs `PRESCRIPTIONS`, case-clustered intervals.
- `agreement.py`: same notes extracted by ≥2 model families, chance-corrected agreement per field group (GRND-3, P0 under D4).
- **Plan for divergence.** Coded diagnoses and note content routinely disagree — coding follows billing and completeness rules, not narrative emphasis. A modest match rate is not necessarily an extraction failure. Decide *before* the confirmatory run how a low number will be framed.

**Tests:** `test_matcher_fixture_precision_recall` · `test_matcher_version_frozen_before_study_run` (run refuses if the matcher version postdates the freeze timestamp) · `test_generic_brand_mapping`.

**Done when:** the matcher is frozen with a measured error rate, and extraction precision/recall against coded ground truth is reported.

---

### W7 · Experiment harness — 1.5 weeks
**Satisfies:** EXP-1 to EXP-8 · *[ref: `main` `meddial/experiments/`]*

- `config.py`: §3.4 `RunConfig` with `config_hash`, `input_manifest_hash`, `prompt_set_hash`.
- `variants.py`: five architectures as **distinct implementations**; the runner refuses to start if a named variant lacks one, so nothing can be silently aliased (EXP-5).
- `runner.py`: immutable per-attempt records (§4.2); resume only on full hash match, raise on mismatch.
- `repair.py`: replace `PromptImprovementAgent`'s broad rewriting with targeted repair keyed to the failed dimension; record what changed per attempt.
- `aggregate.py`: separate entry point; a test asserts zero provider calls during aggregation (EXP-4).
- **EXP-8 control configs**: patient policy varies with `doctor_guidance_id` pinned constant, and the converse.

**Tests:** `test_same_config_and_seed_produce_identical_records` · `test_threshold_change_forces_new_run_id` · `test_aggregation_makes_no_provider_calls` · `test_runner_refuses_unimplemented_variant` · `test_attempt_records_are_append_only`.

**Done when:** two identical runs produce identical records and a config change is detectable by hash alone.

---

### W8 · Analysis and statistics — 1 week
**Satisfies:** STAT-1 to STAT-5 · Appendix E has the procedures

- `stats.py`: case-level clustered bootstrap; paired policy comparisons within case; Wilson intervals for proportions; declared comparison families with recorded adjustment.
- `tables.py`: one command regenerating every table and figure into `outputs/<run_id>/`.
- `power.py`: power calculation from the development pilot, run and recorded **before** the confirmatory run.

**Tests:** `test_clustered_bootstrap_recovers_known_intervals` (synthetic data with known structure) · `test_paired_comparison_uses_case_pairing` · `test_wilson_interval_matches_reference_values` · `test_tables_regenerate_deterministically`.

**Done when:** `meddial-analyze` reproduces every number in the draft with no manual step.

---

### W9 · Downstream signal and privacy — 1.5 weeks
**Satisfies:** DOWN-1 to DOWN-3

- **DOWN-1 (internal).** Train a small model on MedDial dialogues to extract clinical facts; evaluate on **held-out cases** against `DIAGNOSES_ICD` and `PRESCRIPTIONS`, versus a no-synthetic control and a simple-synthetic control. Held-out cases must be absent from *every* earlier stage. State plainly that the evaluation target is clinician-coded while the training signal is not.
- Memorization: longest-common-substring and n-gram overlap distributions vs source notes; near-duplicate rate at a stated threshold; report the distribution, not a mean.
- Membership probe: overlap for source-linked vs unrelated notes.

**Tests:** `test_heldout_cases_absent_from_all_earlier_stages` · `test_controls_reported_even_when_losing`.

**Done when:** controls run and are reported whatever the direction.

---

### W10 · Manuscript scope enforcement — 2 days
**Satisfies:** PRD §11.1

Mechanical pass removing every claim in PRD §11.1 — clinical validity **and** comparative realism, hedged forms included. Named targets in the current manuscript: "clinically grounded", "clinically plausible", "demonstrates clinical reasoning", "suitable for training medical dialogue systems", the §6.4 telemedicine/triage/chatbot paragraph, §7.1's "sufficient quality for downstream training", and every unmeasured use of "safety". Add the scope-boundary paragraph to **Methods**, naming both what no clinician assessed and what no external corpus was compared against. Keep the checklist in `docs/claim_audit.md` for re-audit before resubmission.

---

## 6. E0 — the gate

*Experiment 00 in the strategy document. Runs as soon as W3 items 1–3 and 8 land.*

**Question:** is the reported disclosure→faithfulness trend (0.739 → 0.787 → 0.835) a property of generation, or of measurement?

Four confounds, each separately testable:

| # | Confound | Test | Requires | Data |
|---|---|---|---|---|
| 1 | Reference scope shrinks with the policy | Re-score under `policy_context` vs `full_reference` | W3 · 1–3, 8 | existing corpus |
| 2 | Only patient turns are scored | Score patient and doctor claims separately | W3 · 1–2 | existing corpus |
| 3 | Masking leaves diagnosis-equivalent fields visible | **Direct A/B**: re-derive contexts under policy v1.0 (thesis) and v2.0 (medications dropped), regenerate the affected arm, compare gradients | W2 audit + v1.0 registry | **needs regeneration** |
| 4 | The policy also rewrites the doctor's prompt | Hold `doctor_guidance_id` fixed while patient policy varies | W2 KNOW-6 + W7 | **needs regeneration** |

**Protocol**

1. Resolve D2 first — re-scoring sends MIMIC-derived text to a model (GOV-3).
2. Draw a **stratified sample of 150 cases** (450 dialogues). The effect under test is ~0.5σ on a design paired by case; a paired comparison detects that at 80% power with well under 50 triplets, so 150 is comfortably overpowered and runs in hours rather than days (Appendix B).
3. Run tests 1–3. Report all three curves with case-clustered paired intervals.
4. Tests 3 and 4 both need generation, so batch them: one regeneration pass covering the v1.0/v2.0 policy A/B and the fixed-doctor-guidance control. Run it only if tests 1–2 leave the trend standing.
5. **Only then** choose the manuscript framing.

**Outcomes and what each means**

| Result | Framing |
|---|---|
| Trend survives all four | The generation finding stands, now properly evidenced and attributed. Strongest outcome. |
| Trend attributable to reference scope | A measurement finding: reference-relative faithfulness scoring is confounded with reference size. Publishable, arguably stronger, and the paper TMLR is best suited to. |
| Trend attributable to masking leakage | A finding about disclosure-policy design in simulated-patient work — that partial masking of structured records leaks through correlated fields. Also publishable. |
| Trend attributable to doctor conditioning | A finding about role-conditioned generation confounding role-conditioned evaluation. |
| Mixed | Report the decomposition. This is the most likely outcome and the most informative. |

---

## 7. Milestones

| M | Name | Contents | Weeks |
|---|------|----------|-------|
| **M0** | Safe and buildable | W0 | 0.5–1 |
| **M1** | The gate | W1, W2, W3·1–3+8, **E0** | 3.5–4.5 |
| **M2** | Instrument validated | W3 remainder, W4 | 4–5 |
| **M3** | Anchor established | W5, W6 | 2.5 |
| **M4** | System measured | W7, W8, confirmatory run | 3 |
| **M5** | Utility and privacy | W9 | 1.5 |
| **M6** | Submission ready | W10, README, PhysioNet deposit | 1.5 |

### 7.1 Exit checklists

**M0** ☐ repo private ☐ backup verified ☐ history purged on all refs ☐ CI guard rejects a test commit ☐ secrets rotated ☐ LICENSE + CITATION.cff + hand-written README ☐ clean-container install passes

**M1** ☐ restricted call to an unapproved provider raises before I/O ☐ provider errors raise, never return strings ☐ masking leak test passes on the full cohort ☐ doctor guidance independent of patient policy ☐ both reference modes recorded distinctly ☐ empty dialogue → INCOMPLETE ☐ batched verification ≥5× faster ☐ **E0 tests 1–3 reported with paired intervals** ☐ manuscript framing chosen and written down

**M2** ☐ per-fault-class P/R/F1/AUC with intervals ☐ localisation accuracy reported ☐ fixtures contain no real identifiers ☐ retention gradient reported per policy ☐ policy discriminability AUC reported ☐ ≥3 judge families with agreement statistics

**M3** ☐ matcher specified, version-stamped and frozen ☐ matcher error rate measured on fixtures ☐ cohort reproduces by hash ☐ Appendix A case excluded ☐ exclusion flow counts available ☐ GRND-1/2/3 reported ☐ **framing for a low match rate decided in advance**

**M4** ☐ five variants implemented and distinct ☐ thresholds and prompts frozen with a timestamp ☐ power calculation recorded before the run ☐ confirmatory run complete with immutable records ☐ EXP-8 control reported ☐ every table regenerated by one command ☐ cost per variant reported

**M5** ☐ held-out cases provably isolated ☐ DOWN-1 controls reported whatever the direction ☐ memorization distribution reported ☐ membership gap reported

**M6** ☐ claim audit passes for clinical validity **and** comparative realism ☐ scope paragraph in Methods ☐ PhysioNet deposit has a DOI ☐ data-availability statement cites it and nothing else ☐ manuscript drafted to TMLR length

### 7.2 Dependency graph

```
W0 ──┬─> W1 ──> W2 ──> W3(1-3,8) ──> [E0] ──> W3(rest) ──┐
     │                                                    ├──> W7 ──> W8 ──> W10
     └─> W5 ──> W6* ─────────────────────────────────────>┤          ▲
                 └── matcher frozen (GRND-4) ─> W4 ────────┘    W9 ───┘

* W6 is on the critical path under D4: W4's retention measurement consumes its matcher.
```

W5 and W6 can and **should** start in parallel with W1–W3 — different subsystem, no shared code, and W4 now depends on W6's matcher rather than merely complementing it.

---

## 8. Test strategy

| Level | Scope | Rule |
|-------|-------|------|
| Unit | Every metric, claim extraction, policy masking, acceptance, provider gate | Mocked providers only. No network. Deterministic. |
| Property | Policy masking, injection round-trips | For all policies × all fields, masked fields unreachable; every injected fault recoverable from its label. |
| Regression | The thirteen defects in PRD §1.1 | One named test per defect that **fails on branch `new`**. |
| Integration | Full pipeline on synthetic fixtures | Runs offline end to end and writes a complete attempt record. |
| Reproducibility | Runner and analysis | Same config + seed → identical records; aggregation idempotent and call-free. |
| Governance | CI | Restricted-artifact guard, secret scan, clean-clone install. |

**Acceptance bar:** every P0 requirement has at least one automated test that would fail on the current `new` branch. If it would pass on `new`, it is not testing the thing that was broken.

---

## 9. Sequencing rules

1. **E0 before anything discretionary.** It is cheap — a re-scoring pass over data that exists — and it determines what the paper claims.
2. **Batch corpus-invalidating changes.** New cohort criteria (W5), corrected masking (W2) and separated doctor conditioning (W2) each invalidate the existing corpus. Land all three, then regenerate once.
3. **Freeze the matcher before looking at any result it produces.** Under D4 it is the only external anchor; a rule adjusted after seeing output is a rule fitted to the desired answer.
4. **Freeze thresholds and prompts before the confirmatory run.** Calibrate on a development split, record the freeze, and treat any later change as a new run ID.
5. **Report the unflattering result.** Whatever E0, BENCH-4/5 and DOWN-1 produce, they go in as measured. That is the premise of the whole framing, and the reviewers most likely to accept this paper will notice if only the favourable direction is reported.

---

## Appendix A · Model stack (D2)

Verified against Ollama's August 2026 catalogue. VRAM figures are Q4_K_M unless noted.

### A.1 The rule that governs the stack

**Judge families must have distinct pretraining lineages, and no family may both generate and judge in the same condition.** Three sizes of Qwen are one family, not three. This matters more than any individual model choice, because EVAL-8 exists to address self-preference bias — and a medical fine-tune inherits its parent's lineage:

| Medical model | Derived from | Independent judge alongside… |
|---|---|---|
| MedGemma 1.5 (4B, 27B) | **Gemma 3** | ✗ not alongside Gemma |
| OpenBioLLM (8B, 70B) | **Llama 3** | ✗ not alongside Llama |
| BioMistral (7B) | **Mistral 7B** | ✗ not alongside Mistral |
| Meditron (7B, 70B), PMC-LLaMA | **Llama 2** | ✗ not alongside Llama |

A configuration violating this fails validation at run start (EVAL-8).

### A.2 Assignment by role

| Role | Model | VRAM | Rationale |
|------|-------|------|-----------|
| **Reference extraction** (SCR) | `qwen3.5:32b` | ~20 GB | 256K context handles long notes without chunking; strongest structured-output reliability per GB. Use the largest the hardware allows — extraction errors propagate into every downstream metric. |
| ↳ lower-VRAM | `qwen3:30b` (MoE, 3B active) | 19 GB | Much faster than dense 30B; small accuracy cost. |
| **Patient agent** | `mistral-small3.2:24b` | 15 GB | Natural conversational register at temperature 0.6. |
| **Doctor agent** | `gemma4:27b` | ~20 GB | Strong instruction-following for structured questioning. |
| ↳ laptop-feasible pair | `gpt-oss:20b` both | 14 GB | Runs in 16 GB RAM without a discrete GPU. Apache-2.0. |
| **Judge family 1** | `qwen3.5:32b` | ~20 GB | Alibaba lineage |
| **Judge family 2** | `gpt-oss:20b` | 14 GB | OpenAI lineage, Apache-2.0 |
| **Judge family 3** | `llama3.3:70b` | 43 GB | Meta lineage |
| ↳ optional 4th | `granite4:3b` | 2.1 GB | IBM lineage. Cheap enough to include as a deliberately weak judge, which makes the agreement analysis more informative. |
| **Claim extraction / verification** | `gpt-oss:20b` | 14 GB | JSON reliability matters more than world knowledge; highest-volume path, so pick for speed. |
| **Retention extractor** (BENCH-4) | `llama3.3:70b` or `mistral-small3.2:24b` | 43 / 15 GB | **Must differ in family from the generator**, or retention inherits the generator's blind spots. Reads the dialogue only — never the reference. |
| **Downstream fine-tune target** (DOWN-1) | `gemma4:4b` or `granite4:3b` | 4.3 / 2.1 GB | Trained three times (control, simple-synthetic, MedDial). Small is the point. |

**Conflict check:** generation uses Mistral (patient) and Gemma (doctor); judging uses Qwen, gpt-oss and Llama. No family appears on both sides. Keep it that way when substituting.

### A.3 Medical models — where they belong

`MedGemma 1.5:27b` is the strongest open medical model (~91% MedQA, Gemma-3 lineage) and is tempting for the doctor agent. It should **not** be the default: it collides with Gemma as a judge family, and swapping in a medically tuned generator changes what the study measures. It belongs as an **optional ablation arm** — *does a medically tuned doctor agent reduce hallucination relative to a general model of the same size?* — which is a good question and a clean addition to the W7 variant list if the schedule allows.

Worth reading for the auditability framing: *Fully Open Meditron: An Auditable Pipeline for Clinical LLMs* (arXiv 2605.16215).

### A.4 Serving and reproducibility

| Concern | Rule |
|---|---|
| Development, E0 on a sample, interactive work | **Ollama.** Simple; weak batching. |
| Every batch run | **vLLM** (or llama.cpp with continuous batching). Worth 10–20× on this workload. Do not run the confirmatory study through plain Ollama. |
| Model identity | **Pin digests, not tags.** Ollama tags mutate. Record the digest in the run manifest (GOV-4, EXP-2). |
| Quantisation | Record it. Q4_K_M and Q8 give different scores. Use one quant for the whole study and state it in the paper. |
| Sampling | Set `temperature`, `top_p` and `seed` explicitly on every call; record per call (EXP-3). |
| Judge determinism | Temperature 0.0–0.1 with a fixed seed. `new` already uses 0.1 — keep it and pin the seed. |
| Batch nondeterminism | Continuous batching can change results with batch size. Record the batch configuration; treat a change as a config change (C8). |

---

## Appendix B · Throughput (D2)

### B.1 Where the calls go

Current evaluator, per dialogue per reference mode:

| Step | Calls |
|---|---|
| Claim extraction | 1 |
| Claim verification (`_is_statement_faithful`, once per claim) | **≈15** |
| GEval naturalness | 1 |
| GEval compliance | 1 |
| **Total** | **≈18** |

After EVAL-10 batching, and adding doctor-side claims (EVAL-1):

| Step | Calls |
|---|---|
| Claim extraction, both roles | 1 |
| Batched verification, both roles | 1–2 |
| GEval naturalness | 1 |
| GEval compliance | 1 |
| **Total** | **≈5** |

### B.2 The two big jobs

**E0.** Full corpus, both reference modes, both roles: 1,494 × 5 × 2 ≈ 15,000 calls after batching, versus ~108,000 before. At laptop speeds (~7 s/call) that is ~29 hours instead of ~9 days.

But E0 does not need the full corpus. The effect is large — 0.739 → 0.835, roughly 0.5σ on the reported spread — and the design is paired by case. A paired comparison detects that at 80% power with well under 50 triplets; **150 cases (450 dialogues) is comfortably overpowered** and finishes in hours. Run E0 on a stratified sample of 150; extend only if borderline.

**Confirmatory run at D3 scale (200 cases).**

| Phase | Dialogues |
|---|---|
| A · architecture ablation — 5 variants × 200 × 1 policy | 1,000 |
| B · policy sensitivity — 2 variants × 200 × 3 policies | 1,200 |
| C · targeted recovery — extra attempts | ~400 |
| **Total** | **~2,600** |

Generation dominates: ~50 agent calls per dialogue → ~130,000 calls. Evaluation with three judge families after batching → ~39,000. Call it ~170,000 short calls.

| Hardware | Wall clock |
|---|---|
| Laptop-class (M-series 24 GB, 14–20B Q4, ~20 tok/s) | ~14 days continuous — technically possible, practically miserable, and any bug restarts it |
| Single 24–48 GB GPU, vLLM, continuous batching | **~20–35 hours** |

**Recommendation:** develop and run E0 locally; get GPU access for the confirmatory run. Ask now — it has lead time and M4 is on the critical path. If no GPU materialises, fall back in this order: reduce Phase B to two policies · drop the optional fourth judge · reduce turn budget 30 → 20. Reducing below 200 cases is last resort, since D3 already sets the powered minimum.

---

## Appendix C · Cohort criteria (W5)

A sketch to adapt, not to paste. Version the final SQL in `configs/cohort/criteria_v1.sql` and hash it into the manifest (COH-3).

### C.1 Exclusions

| Stage | Criterion | Source |
|---|---|---|
| E1 | Any ICU stay for the admission | `ICUSTAYS` |
| E2 | In-hospital death | `ADMISSIONS.hospital_expire_flag = 1` or `deathtime IS NOT NULL` |
| E3 | Mechanical ventilation or intubation | `PROCEDURES_ICD.icd9_code IN ('9670','9671','9672','9604')` |
| E4 | Newborn or paediatric | `admission_type = 'NEWBORN'`; age < 18 |
| E5 | Age ≥ 90 (MIMIC shifts DOB for these, so age is uninterpretable) | computed age |
| E6 | High-acuity diagnosis | ICD-9 sets in C.2 |
| E7 | Length of stay above threshold | `dischtime - admittime > N days` |
| E8 | Charlson comorbidity index above threshold | Quan et al. 2005 ICD-9 mapping |
| E9 | Insufficient note content for a valid SCR | `NOTEEVENTS` length / category filter |
| E10 | Not the patient's first qualifying admission | one per `subject_id` (COH-5) |

### C.2 High-acuity ICD-9 sets

```
sepsis / severe infection   038%, 99591, 99592, 78552
acute myocardial infarction 410%
stroke / cerebrovascular    430%–438%
cardiac arrest              4275
acute respiratory failure   51881, 51884
shock                       7855%
malignancy                  140%–239%
```

### C.3 Selection sketch

```sql
WITH ages AS (
  SELECT a.subject_id, a.hadm_id, a.admittime, a.dischtime, a.admission_type,
         DATE_PART('year', AGE(a.admittime, p.dob)) AS age
  FROM admissions a JOIN patients p USING (subject_id)
  WHERE a.hospital_expire_flag = 0 AND a.deathtime IS NULL          -- E2
),
excluded AS (
  SELECT hadm_id FROM icustays                                       -- E1
  UNION SELECT hadm_id FROM procedures_icd
        WHERE icd9_code IN ('9670','9671','9672','9604')             -- E3
  UNION SELECT hadm_id FROM diagnoses_icd
        WHERE icd9_code LIKE '038%' OR icd9_code LIKE '410%'
           OR icd9_code IN ('4275','51881','51884','99591','99592','78552')
           OR icd9_code BETWEEN '430' AND '438'
           OR icd9_code BETWEEN '140' AND '239'                      -- E6
),
eligible AS (
  SELECT g.* FROM ages g
  WHERE g.age BETWEEN 18 AND 89                                      -- E4, E5
    AND g.admission_type <> 'NEWBORN'
    AND g.dischtime - g.admittime <= INTERVAL '7 days'               -- E7
    AND g.hadm_id NOT IN (SELECT hadm_id FROM excluded)
),
first_per_subject AS (                                               -- E10
  SELECT DISTINCT ON (subject_id) * FROM eligible ORDER BY subject_id, admittime
)
SELECT * FROM first_per_subject;
```

Then apply E8 (Charlson) and E9 (note adequacy) in Python, record per-stage counts (COH-4), and sample 200 with the recorded seed (COH-6).

**Validation:** `test_appendix_a_case_is_excluded` must fail this cohort on subject 10446 — a post-CABG sternal wound drainage, URGENT admission, with CHF, AF, PVD, TIAs and diabetes. If it survives, the criteria are still too permissive.

---

## Appendix D · Prompt contracts (W3, W4, W6)

Each evaluator prompt is a versioned file with a declared input set, a required output schema and invariants a test enforces. `prompt_version` is a hash of the template and enters every `ScoreProvenance`.

| Prompt | Inputs | Output schema | Invariants |
|---|---|---|---|
| **Claim extraction** | full transcript with roles and turn indices | `[{turn_index, role, type ∈ ClaimType, text}]` | Every claim carries a valid turn index present in the input. Types outside the enum are rejected. |
| **Batched claim verification** | claim array + selected reference | `[{claim_index, verdict ∈ {supported, unsupported, unverifiable}, justification}]` | Response length equals claim count; every `claim_index` appears exactly once. Mismatch → retry once, then `INCOMPLETE` (EVAL-4/10). |
| **Naturalness (GEval)** | full transcript | `{score ∈ [0,1], rationale}` | Deterministic settings. Failure raises; no fallback. |
| **Boundary check** | transcript + permissible-knowledge set for the role | `[{turn_index, field_path, excerpt}]` | Field paths must exist in the reference schema. Empty list is a valid, meaningful result. |
| **Retention extraction** (BENCH-4) | **dialogue only** | `{diagnoses[], medications[]}` | **The reference must be absent from the rendered prompt.** Enforced by `test_retention_extractor_prompt_excludes_reference`, which asserts no reference substring appears. This is the single most important invariant in the project — retention is the anchor, and a leaked reference silently invalidates it. |
| **SCR extraction** | clinical note | SCR JSON with `EvidenceSpan` per entity | Every entity carries a span whose offsets resolve inside the source note. Entities without a resolvable span are flagged, not dropped silently. |

---

## Appendix E · Statistical procedures (W8)

### E.1 Case-clustered bootstrap (STAT-1)

The unit of resampling is the **case**, not the dialogue. Each case contributes a triplet of policy-linked dialogues that are not independent.

```
for b in 1..B (B ≥ 2000):
    sample n cases with replacement
    take ALL dialogues belonging to each sampled case
    compute the statistic on that resample
report percentile interval over the B values
```

Resampling dialogues rather than cases would understate the interval by treating three correlated observations as three independent ones — which is exactly defect D-11.

### E.2 Paired policy comparison (STAT-1)

For any comparison between policies, pair within case:

```
d_i = metric(case_i, policy_A) − metric(case_i, policy_B)
```

Report the mean of `d_i` with a bootstrap interval over cases, and a paired test. Never compare unpaired group means across policies — the same source case appears in all three arms.

### E.3 Proportions near the ceiling (STAT-2)

Pass rates around 98% break the normal approximation. Use **Wilson** intervals as default, **Clopper–Pearson** where a conservative bound is wanted. State which in the caption.

### E.4 Multiplicity (STAT-3)

Declare comparison families **before** the confirmatory run:

| Family | Comparisons |
|---|---|
| F1 · architecture | 5 variants pairwise on the primary outcome |
| F2 · policy sensitivity | 3 policies pairwise, per metric |
| F3 · judge families | agreement across ≥3 evaluators |
| F4 · fault classes | detection metrics across 7 classes |

Adjust within family (Holm–Bonferroni for small families; Benjamini–Hochberg for F4). Record the method in the manifest. Exploratory comparisons are reported separately and labelled exploratory.

### E.5 Power calculation (STAT-5)

Run on the development pilot before fixing 200:

1. Estimate the within-case paired SD of the primary outcome from the pilot.
2. State the smallest difference worth detecting — justify it in outcome terms, not as a round number.
3. Compute *n* for 80% and 90% power at α = 0.05, two-sided, paired.
4. Add an allowance for `INCOMPLETE` exclusions (EVAL-4) — these are not missing at random, since a dialogue that fails to score may differ systematically. Report the exclusion rate.
5. Record the derivation in `docs/power.md` and cite it in the manuscript. **A sample size that is asserted rather than derived is the first thing a methods reviewer asks about.**

### E.6 Reporting conventions

- Every point estimate carries an interval; no bare means.
- `INCOMPLETE` counts are reported per dimension per condition, never silently dropped.
- Faithfulness figures always carry both subscripts — role and reference mode. A number labelled just "faithfulness" is ambiguous under EVAL-1/2 and must not appear.
- Cost is reported alongside quality for every variant (EXP-7).
- Composite scores are labelled "reporting only, not used for acceptance" wherever they appear.
