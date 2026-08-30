# MedDial

MedDial generates synthetic patient–physician dialogues from clinical notes
using a multi-agent LLM pipeline, and evaluates them with a judge that scores
naturalness, patient-profile compliance, and faithfulness.

This branch (`new`) is being rebuilt against the requirements in
[`MedDial_PRD.md`](MedDial_PRD.md) and
[`MedDial_Implementation_Plan.md`](MedDial_Implementation_Plan.md), which
document a set of defects found in the prior implementation and the plan to
close them before the results can support a publication. See those documents
for the full specification; this README describes only what exists in the
repository today.

**Status:** repository hygiene (W0), the provider layer (W1) and the knowledge
policy layer (W2) are complete. The evaluation rebuild (W3) is partly done:
claim extraction, score provenance and batched faithfulness are in place;
naturalness, boundary, structural and acceptance scoring are not. No MIMIC-III
derived data ships in this repository — see [Data](#data) below.

## What's here

| Path | Purpose |
|---|---|
| `Agents/` | LLM agents: `PatientAgent`, `DoctorAgent`, `DeepEvalJudgeAgent`, `EHRSummarizerAgent`, `PromptImprovementAgent` |
| `meddial/knowledge/` | Structured Clinical Reference, field paths, index-diagnosis redaction, knowledge policies, per-participant contexts |
| `meddial/evaluation/` | Claim extraction, batched claim verification, role-separated faithfulness, score provenance |
| `meddial/evaluation/templates/` | Evaluator prompts as versioned files; a score records the hash of the template that produced it |
| `configs/policies/` | Knowledge policies as data — one JSON file per disclosure arm, hash-locked by `POLICY_HASHES.json` |
| `Utils/` | Prompt templates, dialogue markdown I/O, repetition filtering |
| `agent_prompts/` | Prompt text files consumed by each agent |
| `dialogue_generation_framework.py` | Generates dialogues with iterative quality improvement |
| `gtmf_creation.py` | Extracts Ground Truth Medical Forms (GTMF) from clinical notes |
| `simulation.py` | Runs a single doctor–patient dialogue turn-by-turn |
| `scripts/check_repository_hygiene.py` | CI guard: fails the build if restricted or identifier-shaped paths are present |
| `tests/` | Test suite (currently: repository hygiene guard) |
| `.github/workflows/ci.yml` | CI: restricted-artifact guard, secret scan, test suite |

Nothing under `gtmf/`, `output_dialogue_framework/`, or `analysis/` ships in
this repository — those directories hold generated, MIMIC-derived output and
are excluded by `.gitignore` and enforced by the hygiene guard.

## Data

MedDial is designed to run against [MIMIC-III](https://physionet.org/content/mimiciii/),
a restricted-access clinical database available only under a signed
PhysioNet Data Use Agreement. This repository does not, and will not,
contain MIMIC-III notes, GTMFs derived from them, or dialogues generated from
them. Anyone running this pipeline against real clinical notes needs their
own PhysioNet-credentialed access.

## Installation

```bash
git clone https://github.com/alongott15/FinalProject-MedDial
cd FinalProject-MedDial
pip install -e ".[dev]"
```

## Model providers

Generation runs against a locally served OpenAI-compatible endpoint (Ollama
or vLLM). This is not a preference: MIMIC-III notes and everything derived
from them are classified `RESTRICTED_CLINICAL`, and the provider layer
refuses to send restricted payloads to a hosted API before it opens a
socket. `AzureProvider` exists for public and synthetic payloads only.

Point the generator and judge at their models. Use different model families
for the two — a judge scoring its own family's output is not independent
evidence:

```text
MEDDIAL_GENERATOR_BASE_URL=http://localhost:11434/v1
MEDDIAL_GENERATOR_MODEL=llama3.1:8b
MEDDIAL_JUDGE_BASE_URL=http://localhost:11434/v1
MEDDIAL_JUDGE_MODEL=qwen2.5:14b
```

The weight digest is resolved from the running server at startup, so a run
cannot begin against weights it cannot identify.

## Knowledge policies

What the simulated patient knows is decided by a *knowledge policy*, not by
prompt wording. A policy is a JSON file in `configs/policies/` naming which
reference fields the patient may see, which are withheld, and where index-
diagnosis terms are redacted from free text. Adding an experimental arm
means adding a file; the leak test in
`tests/unit/test_knowledge_policy.py` then covers it automatically, because
it enumerates the registry.

Three properties are enforced rather than assumed:

- **Fail closed.** A reference field no policy classifies is invisible, and
  a policy that leaves one unclassified is rejected at load time.
- **Versioned.** Each policy's body is hash-locked. Editing one in place
  without bumping its version fails loudly.
- **Retired arms stay replayable.** The thesis-era policies are kept at
  `v1.0` and marked deprecated: they can be replayed for comparison, but a
  confirmatory run refuses them.

What the *doctor* is told is a separate input (`doctor_guidance_id`),
defaulting to the patient's policy id so current behaviour is reproducible.
Set it explicitly to vary disclosure and physician guidance independently.

## Evaluation

A faithfulness number is only evidence if you know what produced it, so
`meddial/evaluation/` makes four things explicit that the prior
implementation left implicit:

- **Both speakers are scored.** Claims are extracted from every turn and
  tagged with a role and a turn index, so `patient_factuality` and
  `doctor_factuality` are separate numbers. A doctor who invents a lab
  result is now penalised; previously only patient turns were scored.
- **Hedged reasoning is not a hallucination.** Only `patient_fact` and
  `doctor_fact` claims are checked against the reference. A differential
  ("this could be heart failure") is classified as a hypothesis and left
  unscored.
- **The reference is a choice you record.** Scoring against the policy
  context makes the yardstick shrink as disclosure is restricted, which on
  its own can manufacture a rising trend. Both modes are runnable and the
  one used is recorded on every score.
- **Unmeasurable means `INCOMPLETE`, not zero.** A dialogue with no factual
  claims, or a judge whose verdicts cannot be aligned to the claims it was
  asked about, produces no value and a reason. Nothing defaults into a mean.

Verification is one call per dialogue returning a verdict array, validated
by count and by index; a mismatch is retried once and then reported as
incomplete. The per-claim path is retained only as the baseline that
batching is measured against.

## Running the hygiene guard and tests

```bash
python scripts/check_repository_hygiene.py
python -m pytest
```

## Contributing

1. Fork the repository.
2. Create a branch for your change.
3. Add tests for any new behaviour.
4. Open a pull request; CI must pass (hygiene guard, secret scan, tests).

## License

MIT — see [`LICENSE`](LICENSE). The licence covers the MedDial source code
only; it does not extend to MIMIC-III data or any data derived from it.

## Citation

See [`CITATION.cff`](CITATION.cff).
