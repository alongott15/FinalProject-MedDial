# DeepEvalJudgeAgent

Scores a finished dialogue on naturalness, patient-profile compliance and
RAGAS-style faithfulness, and returns a combined decision.

```python
from Agents.DeepEvalJudgeAgent import DeepEvalJudgeAgent

judge = DeepEvalJudgeAgent(judge_provider, threshold=0.70)
result = judge.evaluate_dialogue(dialogue, patient_profile)
```

| | |
|---|---|
| Constructed at | `dialogue_generation_framework.py:96` |
| Provider | judge (`MEDDIAL_JUDGE_*`) |
| Classification | `RESTRICTED_CLINICAL` — judge prompts embed the dialogue |
| Prompt | GEval criteria defined in-file, not in `bias_aware_prompts` |
| Defaults | `threshold=0.70`, `temperature=0.1`, `max_tokens=1000`, `seed=None` |

## Use a different model family than the generator

A model scoring its own family's output is not independent evidence. The
pipeline enforces this at construction: `dialogue_generation_framework.py:86`
compares `generator_provider.model_family` against
`judge_provider.model_family`. Point `MEDDIAL_JUDGE_MODEL` at a different
family from `MEDDIAL_GENERATOR_MODEL`.

## Where this judge sits relative to `meddial/evaluation/`

This agent is the **in-loop generation judge**: it drives the iterative
improvement loop that decides whether to regenerate a dialogue. It is not the
measurement instrument for reported results.

Reported results come from `meddial/evaluation/`, which scores five dimensions
(`patient_factuality`, `doctor_factuality`, `knowledge_boundary`,
`naturalness`, `structural_validity`), scores both speakers, records which
reference mode produced each number, reports `INCOMPLETE` rather than zero,
and versions its criteria by content hash. The composite below is retained and
reported for continuity with the thesis, but it does not decide acceptance —
a strong average can no longer offset a leaked diagnosis.

Keep that separation in mind when reading a score: this one is a gate on
generation, not a finding.

## Output

`evaluate_dialogue(dialogue, patient_profile, dialogue_transcript=None)`
returns:

| Key | Value |
|---|---|
| `decision` | `"REALISTIC"` if `score >= threshold`, else `"UNREALISTIC"` |
| `score` | Weighted combination, clamped to `[0.0, 1.0]` |
| `justification` | The arithmetic, in words, including `profile_type` |
| `feedback_for_improvement` | `patient_side`, `doctor_side`, `conversation_flow` |
| `deepeval_scores` | `naturalness`, `profile_compliance`, `ragas_faithfulness`, `profile_type`, `weights` |

`DEFAULT_WEIGHTS` is `{naturalness: 0.40, profile_compliance: 0.30,
ragas_faithfulness: 0.30}`; override via `weights=`, which must sum to 1.0.

`dialogue` is a list of `{"role", "content"}`. `patient_profile` is the GTMF
dict the patient agent was given, including `profile_type` — the judge scores
compliance against the arm the patient was actually run under, so it must be
the *masked* profile, not the full reference.

## The three metrics

1. **Naturalness** — GEval, via `ProviderDeepEvalLLM`. Does it read like a
   real consultation?
2. **Profile compliance** — GEval. Did the patient stay inside the knowledge
   boundary named by `profile_type`? The rules come from
   `PATIENT_PROFILE_TYPE_KNOWLEDGE[profile_type]["disclosure_rules"]`, the same
   text the patient was given, so the judge is checking against the instruction
   that was actually issued.
3. **RAGAS faithfulness** — computed manually through the injected provider
   rather than through a second client. Patient turns only, which is precisely
   confound 2 of E0: a doctor who invents a result is invisible to this metric.
   `meddial/evaluation/` scores both roles; this one does not.

`ProviderDeepEvalLLM` adapts `LLMProvider` to deepeval's `DeepEvalBaseLLM`
(`load_model`, `generate`, `a_generate`, `get_model_name`) so GEval runs
through the governed provider — same classification gate, same manifest, same
failure semantics. `a_generate` delegates to the sync path; it exists because
deepeval requires it.

## Failure semantics

`JudgeEvaluationError` is raised when a dimension **cannot be measured** —
a malformed verdict, an unparseable score, verdicts that cannot be aligned to
the claims they were asked about. It is raised at three points in
`_compute_naturalness`, `_compute_profile_compliance` and
`_compute_ragas_faithfulness`.

It exists because the alternative is worse. The prior implementation caught
these and silently switched to an ad-hoc direct-LLM scorer, so a results table
contained values from two different instruments with no way to tell which was
which (defect D-06). That fallback scorer has been deleted. A number in a
results table must come from the scorer its provenance names; a dimension that
could not be measured is reported as unmeasured, never as a substitute value
and never as zero.

A `ProviderError` propagates unchanged. `_complete` exists to make this
explicit: it runs one judge call and never returns error text.

## What must not change without a version bump

- **The GEval criteria strings.** They are the measuring instrument. Editing
  one silently rescales every score computed with it and makes comparisons
  across the edit meaningless.
- **`DEFAULT_WEIGHTS`.** The composite is reported for continuity with the
  thesis; changing the weights breaks that comparison.
- **`threshold`.** It decides which dialogues survive into the corpus, so it
  is a property of the dataset, not of the evaluation.

Record all three in the run manifest.
