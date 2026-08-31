# PromptImprovementAgent

Reads a judge evaluation and proposes targeted adjustments to the doctor's and
patient's instructions for the next attempt.

```python
from Agents.PromptImprovementAgent import PromptImprovementAgent

improvements = PromptImprovementAgent(judge_provider).improve_prompts(
    judge_feedback, dialogue, current_prompts=None
)
```

| | |
|---|---|
| Constructed at | `dialogue_generation_framework.py:97` |
| Provider | judge (`MEDDIAL_JUDGE_*`) |
| Classification | `RESTRICTED_CLINICAL` — the dialogue under review is MIMIC-derived |
| Prompt | `PROMPT_IMPROVEMENT_PROMPT` |
| Defaults | `temperature=0.3`, `max_tokens=600`, `seed=None` |

## Inputs and output

`judge_feedback` is the dict from `DeepEvalJudgeAgent.evaluate_dialogue`. Read
from it: `score`, `decision`, `justification`,
`feedback_for_improvement.{patient_side,doctor_side,conversation_flow}`, and
`deepeval_scores.{naturalness,profile_compliance,ragas_faithfulness,profile_type}`.

`dialogue` supplies the first four turns as a sample.

Returns `{"patient_improvements", "doctor_improvements", "general_improvements"}` —
three strings, fed back through `PatientAgent.update_prompt` and
`DoctorAgent.update_prompt` for the next iteration.

## Bottleneck diagnosis

Rather than passing three numbers and hoping the model reads them, the agent
identifies the lowest sub-score and states the diagnosis explicitly:

| Worst metric | Threshold | What the prompt is told |
|---|---|---|
| `profile_compliance` | < 0.70 | The patient is disclosing outside their boundary; the arm's `disclosure_rules` are quoted verbatim |
| `ragas_faithfulness` | < 0.70 | The patient is asserting details not in the profile |
| `naturalness` | < 0.60 | The dialogue is formulaic; vary phrasing and pacing |

The compliance branch pulls the same `disclosure_rules` string the patient was
originally given, so the correction refers to the instruction that was actually
issued rather than a paraphrase of it.

## The boundary this agent must not cross

A self-improvement loop optimises whatever it is scored on, and the shortest
path from a low compliance score to a high one is to change what the patient
is allowed to know. That would fix the number by dissolving the experiment.
`PROMPT_IMPROVEMENT_PROMPT` therefore fences three things off:

- **Grounding rules.** Never loosen an instruction against inventing symptoms,
  diagnoses, medications or results.
- **The knowledge boundary.** What the patient knows is set by the run's
  disclosure policy. Never propose revealing a diagnosis the patient was not
  told, or hiding one they were. "Fixing a low score by moving that boundary
  destroys the very comparison the run exists to make."
- **Demographic neutrality.**

What is in scope: question shape, pacing, phrasing, varied openings, a patient
who sounds like a person rather than a symptom list.

The prompt also asks for the *smallest* change that addresses the feedback —
a wholesale rewrite cannot be compared with what preceded it, and the loop
produces a sequence of prompt versions that need to remain comparable.

Note that the fence is prompt text, not enforcement. The real guarantee is
structural: the policy layer has already removed withheld fields from the
profile, so a suggestion to reveal a diagnosis cannot succeed even if the model
makes one. That is why the boundary is stated here as well as enforced there —
belt and braces, in that order of reliability.

## Failure semantics

Deliberately different from the other agents, and the difference is
intentional:

- **`ProviderError` propagates.** A broken provider must halt the run rather
  than quietly degrade every subsequent iteration to generic advice.
- **A malformed response falls back.** If the model answers but not as JSON,
  `_parse_improvements` tries text extraction, then `_fallback_improvements`
  produces sub-score-driven advice. This is an acceptable degradation: the
  output is coaching text for the next attempt, not a measurement. It never
  enters a results table, and the next iteration is judged on its own merits.

The distinction is the D-08 line drawn precisely: a failure that would
contaminate data must raise; a failure that only makes the next attempt less
well-targeted may degrade.
