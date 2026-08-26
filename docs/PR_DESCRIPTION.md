# MedDial publication-methodology upgrade (P0–P2)

## Summary

This change makes scientific correctness the default: role-separated contexts, formal profile policies, fail-closed role-aware evaluation, immutable experiment records, reproducible cohort manifests, evaluator/ablation hooks, and mocked CI.

## Checklist

### P0 — correctness and experiment integrity

- [x] Correct stats binding and move aggregation out of generation
- [x] Separate patient, doctor, and evaluator knowledge scopes
- [x] Apply consistent diagnosis/treatment/medication masking policies
- [x] Fail closed with explicit `ERROR` / `UNSCORABLE`
- [x] Rename custom faithfulness and include doctor claims
- [x] Require per-dimension acceptance
- [x] Add provider-independent LLM interface and DoctorAgent injection
- [x] Narrow and accurately describe the lexical cohort filter
- [x] Add deterministic sampling/manifests and reject-only behavior
- [x] Make SCR extraction failures explicit and improve chunk merging
- [x] Add SCR compatibility aliases, Pydantic v2 models, and safe defaults

### P1 — CMPB methodology

- [x] Evidence provenance structures
- [x] Role-aware claim taxonomy and knowledge-boundary leakage metrics
- [x] Deterministic structural validation
- [x] Independent multi-model ensemble configuration hooks
- [x] Failure classification and targeted recovery
- [x] Labelled injected-error benchmark scaffolding
- [x] Five publication ablation configurations
- [x] Immutable attempts, separate analysis, run IDs/config hashes
- [x] Per-call model/config metadata where exposed by providers

### P2 — publication engineering

- [x] Unit, integration, and regression tests with mock providers
- [x] GitHub Actions for Ruff, mypy, and pytest
- [x] CI guard against recommitting restricted/generated clinical artifacts
- [x] `pyproject.toml` with Pydantic v2 and `uv.lock`
- [x] Generated-data/secret-aware `.gitignore`
- [x] Architecture/setup/limitations README update
- [x] Clear license-owner TODO; no license terms invented
- [x] New modular `meddial` package with legacy compatibility imports

## Known limitations / manual steps

- Start the three recommended evaluator families on institution-controlled local inference
  endpoints; tests continue to use deterministic mocks.
- Complete and adjudicate the two-clinician review file before SCR extraction.
- Re-extract the final cohort locally; historical GTMFs and generated dialogues are excluded and
  removed from the current tree.
- The repository owner must confirm copyright authority before adding Apache-2.0 `LICENSE` and
  `NOTICE` files.
- Earlier Git history and thesis/poster examples require an owner/institutional data-governance
  audit before public release; this PR does not rewrite history.
- A study executor must implement and declare every variant before the fail-closed publication
  runner will execute the 13-cell plan.

## Compliance and study-design follow-up

- [x] Local controlled provider is the default for restricted clinical inputs
- [x] External providers fail closed for restricted clinical content
- [x] Strict three-family, claim-level evaluator ensemble
- [x] Lexical-v3 full-pool selection and one admission per patient
- [x] Two-clinician review/adjudication workflow
- [x] Private and release-safe cohort manifests
- [x] Architecture, policy-sensitivity, and recovery phases separated
- [x] Historical clinical profiles/dialogues and bundled third-party CSV removed from current tree
- [x] Data-governance, clinical-review, experiment, licensing, citation, and third-party notices
