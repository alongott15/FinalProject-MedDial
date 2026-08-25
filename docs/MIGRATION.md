# MedDial 0.2 migration notes

This release changes scientific defaults. Existing generated dialogue and thesis artifacts are not modified.

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

## Setup

Install with `pip install -e ".[azure]"`. Development checks use `pip install -e ".[dev]"`.

The historical `requirements.txt` remains as a one-line compatibility entry point; dependency definitions live in `pyproject.toml` and the generated `uv.lock`.

## Licensing TODO

No license is granted by this repository. The repository owner must choose and approve a license before adding a `LICENSE` file or encouraging reuse.
