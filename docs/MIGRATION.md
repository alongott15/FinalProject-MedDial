# MedDial 0.3 migration notes

This release changes scientific and data-governance defaults. Thesis artifacts remain unchanged;
generated clinical profiles and dialogues are removed from the tracked working tree and
preserved only in a local quarantine pending owner/institutional review.

## Terminology

- `StructuredClinicalReference` / `SCR` replaces “Ground Truth Medical Form”. `GTMF` remains a compatibility alias.
- `RoleAwareClinicalFaithfulness` replaces the custom “RAGAS Faithfulness” label.
- `TargetedRecoveryAgent` replaces generic prompt rewriting. `PromptImprovementAgent` remains a compatibility alias.
- “Lower-acuity lexical candidate” replaces claims that the MIMIC discharge-summary cohort represents primary care or clinically proven light cases.

## Behavior changes

- `NO_DIAGNOSIS_NO_TREATMENT` now masks treatment options, current medications, and discharge medications consistently in both data and prompts.
- The doctor receives demographics only at the start and learns clinical facts from patient turns.
- Evaluation extraction errors produce `ERROR` or `UNSCORABLE`, never a perfect score.
- Acceptance requires every mandatory dimension to pass. The composite score is reporting-only.
- An unaccepted “best” candidate is no longer counted as successful.
- Runs are stored under `output_dialogue_framework/runs/<run_id>/` with immutable attempt files and a config hash.
- Resume reuses only a matching config and input-reference manifest hash.
- `fetch_notes_with_light_case_filter` returns eligible notes only and can write/reuse a deterministic cohort manifest.
- The complete discharge-summary pool is scanned before deterministic, one-patient sampling;
  publication extraction requires a completed two-clinician review file.
- Restricted clinical calls now default to `LocalOpenAICompatibleProvider`; external providers
  raise `DataBoundaryError`.
- Independent evaluator output is claim-level and dimension-level, and publication ensembles
  require three complete judges.
- Private manifests contain source identifiers; release manifests contain salted study IDs only.
- Aggregation reports first-attempt success, factuality/plausibility dimensions, structural
  validity, leakage, and model-call/token totals separately from generation.

## Setup

Install with `pip install -e ".[dev]"`. Azure remains an optional extra for explicitly
public/synthetic inputs only.

The historical `requirements.txt` remains as a one-line compatibility entry point; dependency definitions live in `pyproject.toml` and the generated `uv.lock`.

## Licensing TODO

Apache-2.0 is recommended, but no license is granted until the repository owner confirms
copyright authority and approves the holder/year. See `docs/LICENSING.md`.
