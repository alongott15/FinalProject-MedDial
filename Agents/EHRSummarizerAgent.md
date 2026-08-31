# EHRSummarizerAgent

Condenses a clinical note into 5–8 sentences of continuous prose.

```python
from Agents.EHRSummarizerAgent import EHRSummarizerAgent

summary = EHRSummarizerAgent(provider).summarize(ehr_text, metadata)
```

| | |
|---|---|
| Constructed at | no production caller; exercised by `tests/unit/test_agent_provider_injection.py` |
| Provider | generator-class |
| Classification | `RESTRICTED_CLINICAL` — the input is a MIMIC-III note |
| Prompt | `EHR_SUMMARIZER_PROMPT` |
| Defaults | `temperature=0.1`, `max_tokens=400`, `seed=None` |

Temperature is near-zero. This is a transcription task: two runs over the same
note should differ as little as the stack allows.

**No pipeline currently calls this.** The structured path from note to profile
runs through `gtmf_creation.py`, which extracts a schema rather than prose.
The agent is kept because it is the intended source of narrative grounding
where a summary, not a form, is wanted — and because it is the smallest
complete example of the injection, classification and failure contract that
every other agent follows.

## Inputs and output

- `ehr_text` — the note. **Truncated to the first 2000 characters** before the
  call. For a MIMIC-III discharge summary that is typically the admission and
  history sections; the hospital course and discharge plan are usually past the
  cut. Anything the note documents after that point is absent from the summary,
  and nothing downstream can tell truncation from silence.
- `metadata` — optional; only `Patient_Demographics.{Age,Sex}` is read, as a
  one-line preamble.

Returns the stripped summary text.

## What the prompt asks for

Six ordered elements, each skipped if the note does not document it: chief
complaint, symptoms with severity and duration, relevant history, findings
with figures as recorded, assessment, plan.

Two instructions carry the weight:

- **"Anything you add here, the rest of the pipeline will treat as established
  fact; anything you drop, it can never recover."** The summary becomes
  grounding. An invented detail is indistinguishable from a real one the
  moment it is written down.
- **"If the note documents no diagnosis, your summary contains no diagnosis."**
  Reaching for the most likely candidate is exactly the behaviour that would
  manufacture a diagnosis for a case that never had one.

## Failure semantics

A `ProviderError` propagates. The prior implementation returned
`"Unable to generate summary"` on failure — a string that reads as content,
grounds a dialogue, and is scored (D-08). Covered by
`test_summarizer_raises_instead_of_returning_a_placeholder`.

`test_summarizer_cannot_reach_a_provider_barred_from_clinical_data` covers the
other half: a provider not approved for `RESTRICTED_CLINICAL` raises before any
network call.
