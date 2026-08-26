# CMPB experiment protocol

Use `meddial-study-plan` to freeze a hashed plan before generation. The recommended design has
three paired phases using the same clinician-validated cohort, generator checkpoint, decoding
configuration, turn budget, and blinded offline evaluator suite.

## Phase A: architecture ablation

Run the five variants under `NO_DIAGNOSIS_NO_TREATMENT` with one attempt each. This measures raw
first-attempt quality without confounding recovery budget.

## Phase B: knowledge-policy sensitivity

Run `knowledge_controlled` and `full_meddial` under `FULL`, `NO_DIAGNOSIS`, and
`NO_DIAGNOSIS_NO_TREATMENT`, one attempt each.

## Phase C: targeted recovery

Compare full MedDial with one attempt against full MedDial with up to three targeted attempts.

## Outcomes and analysis

- Primary: all mandatory dimensions pass on the first attempt.
- Secondary: patient factuality, doctor factuality, zero-leakage rate, structural validity,
  naturalness, attempts, latency, and token/compute cost.
- Pair by clinical case; report case-level bootstrap confidence intervals and multiplicity-
  adjusted comparisons.
- Use a 30-case development pilot, then conduct a power calculation before the planned 200-case
  final study.
- Calibrate prompts and thresholds on development/error-injection data only and freeze them
  before final evaluation.

The runner refuses to start unless the supplied executor declares an implementation for every
named variant. This prevents the legacy multi-agent path from being mislabeled as direct or
single-agent ablations.
