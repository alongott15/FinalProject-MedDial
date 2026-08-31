# PatientAgent

Speaks as the patient across a consultation, saying only what a person in that
situation would know and would say.

```python
from Agents.PatientAgent import PatientAgent

agent = PatientAgent(profile, generator_provider, temperature=0.6, max_tokens=300)
reply = agent.respond(conversation_history)
```

| | |
|---|---|
| Constructed at | `dialogue_generation_framework.py:124` |
| Provider | generator (`MEDDIAL_GENERATOR_*`) |
| Classification | `RESTRICTED_CLINICAL` — the profile is MIMIC-derived |
| Prompt | `PATIENT_SYSTEM_PROMPT` + `PATIENT_PROFILE_TYPE_KNOWLEDGE` |
| Defaults | `temperature=0.6`, `max_tokens=300`, `seed=None` |

Temperature is the highest of any agent. The patient is the one voice in the
pipeline that is *supposed* to vary — a patient who answers identically twice
is the failure mode the naturalness metric exists to catch.

## Inputs

`profile` is a GTMF dict, already masked by the run's knowledge policy:

| Path | Used for |
|---|---|
| `profile_type` | Which knowledge block and disclosure rules apply |
| `Core_Fields.Symptoms[].description` | Symptom list, and gradual disclosure order |
| `Core_Fields.Diagnoses[]` | Only rendered when `profile_type == "FULL"` |
| `Core_Fields.Treatment_Options[]` | Only rendered for `FULL` and `NO_DIAGNOSIS` |
| `Context_Fields.Patient_Demographics.{Age,Sex}` | Persona, register, emotional state |
| `Context_Fields.Medical_History.Past_Medical_History` | Profile section |
| `Context_Fields.Allergies[]`, `Context_Fields.Current_Medications[]` | Profile section |
| `Additional_Context.Chief_Complaint` | Profile section |

A missing field renders as "Not specified in profile." — an honest gap the
patient can say they do not know, rather than a hole the model fills.

## Output

`respond(conversation_history)` returns the reply text. `conversation_history`
is a list of `{"role": "patient"|"doctor", "content": str}`; the agent maps
`patient` to `assistant` and `doctor` to `user` before calling the provider.

`update_prompt(instructions)` stores coach feedback, injected as one extra
user message on the next `respond` call and then cleared.

## The disclosure policy

`profile_type` selects one entry of `PATIENT_PROFILE_TYPE_KNOWLEDGE`, which
carries six keys:

| Key | Where it lands |
|---|---|
| `system_instruction` | The `{knowledge_instruction}` block of the system prompt |
| `conclusion_behaviour` | How the patient receives the doctor's assessment |
| `disclosure_rules` | Re-injected every turn as a reminder |
| `description` | Documentation and judge context |
| `knows_diagnosis`, `knows_treatment` | Booleans other components branch on |

The three arms:

- **`FULL`** — knows the diagnosis and the treatment plan. Leads with how it
  feels, but names the condition if asked directly. Hears the doctor's
  assessment as a second opinion and says whether it matches.
- **`NO_DIAGNOSIS`** — knows the symptoms and the medications, was never told
  what the condition is called. Will not echo a name back as though they had
  known it. Hears the diagnosis for the first time.
- **`NO_DIAGNOSIS_NO_TREATMENT`** — knows only the symptoms. Everything the
  doctor concludes is new. This is the fallback for an unrecognised
  `profile_type`, which is the fail-closed direction: an unknown arm produces
  the most ignorant patient, never the most informed one.

The `conclusion_behaviour` text differs across arms because *hearing a
diagnosis for the first time* and *hearing it confirmed* are different
conversations, and a prompt that describes both as "react naturally" produces
a patient who reacts to news they were already given.

## What must not change without a version bump

- **The knowledge blocks.** They are the manipulation the whole study rests
  on. Changing what `NO_DIAGNOSIS` means changes what every number under
  `NO_DIAGNOSIS` measures.
- **The grounding rules.** Rules 1–4 of "What you can and cannot say" are what
  make an ungrounded claim countable. Loosening them raises naturalness and
  destroys faithfulness, and the composite score will hide the trade.
- **The fallback to `NO_DIAGNOSIS_NO_TREATMENT`.** Reversing it to `FULL`
  would make an unknown arm silently the most permissive one.

Style guidance — sentence length, hesitation, varied openings — is the part
`PromptImprovementAgent` is allowed to touch.

## Failure semantics

A `ProviderError` from `respond` propagates (D-08). The old code substituted a
placeholder utterance; it would have been scored as speech. Structural
validity now also scans for provider-error sentinels, so any that survived
into an old transcript are caught rather than averaged in.
