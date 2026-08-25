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
- [x] `pyproject.toml` with Pydantic v2 and `uv.lock`
- [x] Generated-data/secret-aware `.gitignore`
- [x] Architecture/setup/limitations README update
- [x] Clear license-owner TODO; no license terms invented
- [x] New modular `meddial` package with legacy compatibility imports

## Known limitations / manual steps

- Independent evaluator model IDs/providers must be supplied for publication runs; tests use deterministic mocks.
- The lexical cohort filter is auditable but is not a clinical severity classifier; the study cohort still requires clinical review.
- Historical GTMF Markdown loads through compatibility code but lacks provenance unless re-extracted.
- The repository owner must select a license.
- Real Azure validation requires local credentials and may incur costs; CI never performs paid calls.
