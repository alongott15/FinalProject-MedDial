# DoctorAgent

Conducts the consultation: opens broadly, explores the complaint the patient
leads with, then closes with an assessment and a plan.

```python
from Agents.DoctorAgent import DoctorAgent

agent = DoctorAgent(
    generator_provider,
    patient_profile=profile,
    guidance_id=None,          # None → brief the doctor to match the patient
)
reply = agent.respond(conversation_history)
```

| | |
|---|---|
| Constructed at | `dialogue_generation_framework.py:119` |
| Provider | generator (`MEDDIAL_GENERATOR_*`) |
| Classification | `RESTRICTED_CLINICAL` |
| Prompt | `DOCTOR_SYSTEM_PROMPT` + `DOCTOR_GUIDANCE` |
| Defaults | `temperature=0.5`, `max_tokens=300`, `seed=None` |

## `guidance_id` — the point of this agent

**The doctor's briefing is a separate experimental factor from the patient's
disclosure policy, and only the caller may choose it.**

`meddial/knowledge/` has always produced two ids — `contexts.patient.policy_id`
and `contexts.doctor.guidance_id` — and the run record at
`dialogue_generation_framework.py:283` has always written both. But this agent
used to read its briefing straight off `patient_profile["profile_type"]`,
which meant the two ids were equal by construction no matter what the caller
asked for. Two factors were varied as one, and no analysis of the resulting
corpus could separate them. That is defect D-05, and confound 4 of experiment
E0: the reported faithfulness trend across disclosure arms may be partly an
effect of re-briefing the doctor, not of restricting the patient.

So:

```python
self.guidance_id = guidance_id or profile_type
guidance = DOCTOR_GUIDANCE.get(self.guidance_id, DOCTOR_GUIDANCE["NO_DIAGNOSIS_NO_TREATMENT"])
```

`guidance_id=None` reproduces the uncrossed default. Passing a
`guidance_id` different from the patient's `profile_type` crosses the factors —
for example a patient under `NO_DIAGNOSIS_NO_TREATMENT` seen by a doctor
briefed as if for `FULL`. `tests/unit/test_doctor_guidance_independence.py`
asserts the briefing follows `guidance_id` and not `profile_type`.

The pipeline threads this from `DialogueGenerationPipeline(doctor_guidance_id=...)`.

## The three briefings

Each entry of `DOCTOR_GUIDANCE` tells the doctor what to expect and what not
to assume:

- **`FULL`** — the patient may name their condition; do not act surprised.
  Spend questions on how it is going now. The assessment confirms or refines
  rather than announcing.
- **`NO_DIAGNOSIS`** — the patient knows their medications but not what they
  are for. Reaching a diagnosis and stating it clearly is a goal of the visit.
- **`NO_DIAGNOSIS_NO_TREATMENT`** — a first consultation. Do not ask whether
  they are already being treated unless the symptoms make it relevant. Build
  the symptom picture, then close with both an assessment and a plan.

`DOCTOR_GUIDANCE` is `dict[str, str]`, keyed by guidance id. It is deliberately
a different mapping from `PATIENT_PROFILE_TYPE_KNOWLEDGE`, keyed differently,
so that selecting from it with a policy id is a visible mistake rather than an
invisible coincidence.

## Inputs

From `patient_profile`, only what a physician would plausibly have before the
consultation:

| Path | Used for |
|---|---|
| `Context_Fields.Patient_Demographics.{Age,Sex}` | `{demographics}` |
| `Core_Fields.Symptoms[].description` | Symptom tracking (not shown in the prompt) |
| presence of `Symptoms` / `Medical_History` / `Allergies` | `{data_available}` — *that* data exists, not its content |
| `profile_type` | Default `guidance_id` only |

The doctor is never handed the diagnosis. `{data_available}` names categories,
not values, so the doctor cannot read the answer out of the briefing.

## Per-turn state

`respond` is stateful across a dialogue:

- **Phase** by turn count — opening (≤3), exploration (≤8), synthesis (≤11),
  conclusion. Each phase supplies its own guidance, and exploration nudges
  toward a conclusion once ≥6 turns and ≥2 symptoms are covered.
- **Symptom tracking** — `key_symptoms` minus `discussed_symptoms` drives an
  unexplored-symptom hint with a concrete follow-up question.
- **Emotion detection** — keyword-matched from the last patient turn.
- **Repetition** — `RepetitionTracker` plus `detect_symptom_repetition` inject
  explicit warnings when openings or symptoms are being recycled.
- **Already-concluded detection** — if a previous doctor turn contains "based
  on" / "sounds like" / "recommend" / "my assessment", the conclusion phase
  switches to a short closing turn instead of restating the assessment.

## Output

`respond(conversation_history)` returns the reply text; `doctor` maps to
`assistant` and `patient` to `user`. `update_prompt(instructions)` stores
coach feedback for one turn.

## What must not change without a version bump

- **The `guidance_id` indirection.** Reading the briefing from
  `patient_profile["profile_type"]` inside this class reintroduces D-05.
- **The grounding block.** "Assume no symptom, result or history that has not
  been mentioned" is what makes `doctor_factuality` measurable. Both speakers
  are scored now; a doctor who invents a lab result is penalised.
- **The severity floor.** "Do not escalate a mild presentation" — without it
  the doctor drifts toward serious diagnoses the note never supported, and
  every faithfulness score inherits the drift.

## Failure semantics

A `ProviderError` propagates (D-08).
