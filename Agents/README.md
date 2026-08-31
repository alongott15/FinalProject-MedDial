# Agents

Five agents, each a thin object around one injected `LLMProvider` and one
prompt template. An agent decides *how to ask*; it does not decide what the
model may see and it does not decide what a failure means.

| Agent | Role | Prompt | Provider |
|---|---|---|---|
| [`PatientAgent`](PatientAgent.md) | Speaks as the patient, within a disclosure policy | `PATIENT_SYSTEM_PROMPT` | generator |
| [`DoctorAgent`](DoctorAgent.md) | Conducts the consultation | `DOCTOR_SYSTEM_PROMPT` | generator |
| [`EHRSummarizerAgent`](EHRSummarizerAgent.md) | Condenses a clinical note into grounding prose | `EHR_SUMMARIZER_PROMPT` | generator |
| [`DeepEvalJudgeAgent`](DeepEvalJudgeAgent.md) | Scores a finished dialogue | GEval criteria, in-file | judge |
| [`PromptImprovementAgent`](PromptImprovementAgent.md) | Turns judge feedback into prompt edits | `PROMPT_IMPROVEMENT_PROMPT` | judge |

`gtmf_creation.py` is not an agent but follows the same contract, using
`GTMF_CREATION_PROMPT` and its own provider (`MEDDIAL_GTMF_*`).

## Four rules that hold for every agent

**The provider is injected, never constructed.** An agent that built its own
client would put a second, unrecorded model configuration into a run, and the
manifest would describe only one of them (GOV-4). Every constructor takes
`provider: LLMProvider` as its first or second positional argument, and
`test_agent_provider_injection.py` asserts that omitting it raises.

**Everything derived from a MIMIC-III note is labelled
`RESTRICTED_CLINICAL`.** Notes, profiles, dialogues and judge prompts all
carry that label into `provider.complete(...)`, and the provider layer refuses
the call before it opens a socket if the provider is not approved for that
classification (GOV-3, constraint C2). The label is not advisory — it is the
only thing standing between a discharge summary and a hosted endpoint.

**A `ProviderError` propagates.** No agent catches one to return a placeholder
string. A placeholder becomes a turn, the turn becomes a transcript, the
transcript gets scored, and nothing downstream can tell it apart from
generated text. This is defect D-08, and it is regression-tested per agent.

**Prompts live in `Utils/bias_aware_prompts.py`, not here.** One file holds
every string the pipeline sends to a model, so "what was this run asked to do"
has one answer. Editing a string changes what the numbers mean: treat an edit
as a new prompt version and record it in the run manifest.

## What is a prompt's job, and what is not

Prompt text is the *second* line of defence. What the patient is allowed to
know is decided in `meddial/knowledge/` by a hash-locked policy that removes
fields from the profile before an agent sees them. An instruction not to
mention the diagnosis is worthless while the diagnosis is still in the
context, and redundant once it has been removed — it is there to stop the
model reasoning its way back to a masked field, not to do the masking.

Consequently: a leak is a bug in the policy layer, not in the prompt. Fix it
there.

## The two factors

Two things vary across an experimental arm and they are kept apart on purpose:

- **`policy_id`** — what the patient knows (`PATIENT_PROFILE_TYPE_KNOWLEDGE`).
- **`guidance_id`** — what the doctor is briefed to expect (`DOCTOR_GUIDANCE`).

They default to the same key, so an ordinary run is unchanged, but a run may
cross them. Deriving one from the other fuses "the patient discloses less"
with "the doctor is briefed differently" into a single treatment that no
analysis can decompose — defect D-05, and confound 4 of experiment E0.
`DoctorAgent` takes `guidance_id` from its caller for exactly this reason.

## Testing an agent without a model

`meddial.llm.MockProvider` replays scripted responses, records every call with
its classification, and can be told to fail or to be unapproved for a
classification:

```python
from meddial.llm import DataClassification, MockProvider

provider = MockProvider(["scripted reply"])
agent = SomeAgent(provider)
agent.do_something(...)
assert provider.calls[0].classification is DataClassification.RESTRICTED_CLINICAL
```

See `tests/unit/test_agent_provider_injection.py` for the pattern.
