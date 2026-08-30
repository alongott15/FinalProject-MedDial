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

**Status:** repository hygiene pass (workstream W0) in progress. No MIMIC-III
derived data ships in this repository — see [Data](#data) below.

## What's here

| Path | Purpose |
|---|---|
| `Agents/` | LLM agents: `PatientAgent`, `DoctorAgent`, `DeepEvalJudgeAgent`, `EHRSummarizerAgent`, `PromptImprovementAgent` |
| `Models/classes.py` | Data classes for extracted medical entities (GTMF) |
| `Utils/` | Prompt templates, dialogue markdown I/O, LLM client helpers, partial-profile masking, repetition filtering |
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
