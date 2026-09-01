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

**Status.** Built and unit-tested: repository hygiene (W0), the provider layer
(W1), the knowledge policy layer (W2), the evaluation rebuild (W3), the
benchmarks (W4), the cohort rebuild (W5), reference grounding (W6), the
experiment harness (W7) and the analysis layer (W8). The evaluator ensemble is
deferred until the E0 measurement-confound experiment reports. Downstream
signal and privacy (W9) and manuscript scope enforcement (W10) have not been
started.

Two things are built but have never run against real data, and the difference
matters when reading any result:

- **E0** needs the existing dialogue corpus, which is not part of this
  repository and is not on the development machine.
- **The cohort** (`meddial/cohort/`) is tested against synthetic admission
  records only; reproducing it needs credentialed MIMIC-III access.

No MIMIC-III derived data ships here — see [Data](#data) below.

## What's here

| Path | Purpose |
|---|---|
| `Agents/` | LLM agents: `PatientAgent`, `DoctorAgent`, `DeepEvalJudgeAgent`, `EHRSummarizerAgent`, `PromptImprovementAgent` — one `.md` per agent, indexed by [`Agents/README.md`](Agents/README.md) |
| `meddial/knowledge/` | Structured Clinical Reference, field paths, index-diagnosis redaction, knowledge policies, per-participant contexts |
| `meddial/evaluation/` | Claim extraction, batched claim verification, role-separated faithfulness, naturalness, knowledge-boundary leakage, deterministic structural validity, acceptance gates, score provenance |
| `meddial/evaluation/templates/` | Evaluator prompts as versioned files; a score records the hash of the template that produced it |
| `meddial/stats/` | Case-clustered bootstrap, paired within-case comparison, Wilson intervals — the resampling unit is the case, not the dialogue |
| `meddial/experiments/` | E0 (re-scores an existing corpus under both reference modes and both roles) and the W7 run harness: versioned configs, immutable attempt records, five distinct variants, targeted repair, pure aggregation |
| `meddial/experiments/backend.py` | The composition layer that makes the five variants executable — wires each architecture to injected providers and records what every call cost |
| `meddial/grounding/` | The frozen entity matcher: normalisation rules as data, hash-locked specs, and the matcher's own error rate measured on a fixture |
| `meddial/cohort/` | Deterministic cohort selection from structured fields only, with per-step exclusion counts and a hash manifest |
| `meddial/cohort/mimic_csv.py` | Reads the MIMIC-III CSV distribution into cohort candidates, reproducing `configs/cohort/criteria_v1.sql` without a Postgres instance |
| `meddial/benchmarks/` | Synthetic-only benchmarks: fault injection, per-class detector evaluation with localisation, retention across policies, policy discriminability |
| `meddial/analysis/` | Paired case-clustered statistics, pre-registered power derivation, and one-command regeneration of every table and the primary figure |
| `meddial/cli.py` | The `meddial-run` and `meddial-tables` console entry points |
| `configs/policies/` | Knowledge policies as data — one JSON file per disclosure arm, hash-locked by `POLICY_HASHES.json` |
| `configs/matchers/` | Matcher specifications and their fixtures, hash-locked by `MATCHER_HASHES.json` |
| `configs/experiments/` | One run config per variant. Deliberately unfrozen: a confirmatory run must freeze them first |
| `configs/cohort/` | The cohort selection criteria as SQL |
| `uv.lock` | The resolved dependency tree. `pyproject.toml` pins ranges; this pins the resolution |
| `Utils/bias_aware_prompts.py` | Every prompt the pipeline sends to a model, in one file |
| `Utils/` | Dialogue markdown I/O, conversation variety, repetition filtering |
| `dialogue_generation_framework.py` | Generates dialogues with iterative quality improvement |
| `gtmf_creation.py` | Extracts Ground Truth Medical Forms (GTMF) from clinical notes |
| `simulation.py` | Runs a single doctor–patient dialogue turn-by-turn |
| `scripts/check_repository_hygiene.py` | CI guard: fails the build if restricted or identifier-shaped paths are present |
| `scripts/run_e0.py` | Runs E0 tests 1–2 against a local judge; resumable, and refuses to write its output inside the repository |
| `tests/` | Test suite — 204 unit tests, all model calls mocked |
| `.github/workflows/ci.yml` | CI: restricted-artifact guard, secret scan, test suite |

Nothing under the top-level `gtmf/`, `output_dialogue_framework/` or
`analysis/` output directories ships in this repository — they hold generated,
MIMIC-derived output, and are excluded by `.gitignore` and enforced by the
hygiene guard. (The `analysis/` output directory is unrelated to the
`meddial/analysis/` package, which is code and does ship.)

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

`pyproject.toml` pins version ranges, which is what the package publishes.
`uv.lock` pins the exact resolution the results were produced against; install
that instead with `uv sync --extra dev` when reproducing a run rather than
developing.

Install `".[bigquery]"` as well to read MIMIC-III from BigQuery rather than
from local CSV files. It is an optional extra so that the CSV path keeps
working on a machine with no cloud account at all.

## Model providers

Generation runs against a locally served OpenAI-compatible endpoint (Ollama
or vLLM). This is not a preference: MIMIC-III notes and everything derived
from them are classified `RESTRICTED_CLINICAL`, and the provider layer
refuses to send restricted payloads to a provider not approved for them
before it opens a socket. No hosted-API provider ships in this repository.

Point the generator and judge at their models. Use different model families
for the two — a judge scoring its own family's output is not independent
evidence:

```text
MEDDIAL_GENERATOR_BASE_URL=http://localhost:11434/v1
MEDDIAL_GENERATOR_MODEL=llama3.1:8b
MEDDIAL_JUDGE_BASE_URL=http://localhost:11434/v1
MEDDIAL_JUDGE_MODEL=qwen3.5:9b
MEDDIAL_GTMF_BASE_URL=http://localhost:11434/v1
MEDDIAL_GTMF_MODEL=qwen3.5:9b
```

`MEDDIAL_GTMF_*` drives `gtmf_creation.py`, which reads MIMIC-III discharge
summaries. Each prefix also accepts `_FAMILY` and `_QUANT` to record what a
digest alone does not say.

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

Five dimensions are scored: `patient_factuality`, `doctor_factuality`,
`knowledge_boundary`, `naturalness` and `structural_validity`. A dialogue is
accepted only if it passes every one of them; the thesis composite
(0.4 naturalness + 0.3 compliance + 0.3 faithfulness) is still computed and
reported, but it is excluded from the decision, so a strong average can no
longer offset a leaked diagnosis.

A faithfulness number is only evidence if you know what produced it, so
`meddial/evaluation/` makes six things explicit that the prior
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
  There is no fallback scorer: the naturalness scorer that used to catch any
  exception and switch to an unrecorded second scorer has been deleted.
- **Leakage is a located event, not a rate.** Each event names the turn, the
  reference field path it revealed and a verbatim excerpt, and the detector
  cannot cite a path outside the schema or count a field the speaker was
  permitted to know. Under the `FULL` policy nothing is left to leak, so its
  zero-leakage result is definitional; every score records this as
  `permissible_is_total`.
- **Structural validity consults no model.** Role alternation, empty turns,
  turn bounds, duplicate turns, symptom repetition and provider-error
  sentinels are checked deterministically, so structural validity can never
  be the reason two runs of the same cohort disagree.

Verification is one call per dialogue returning a verdict array, validated
by count and by index; a mismatch is retried once and then reported as
incomplete. The per-claim path is retained only as the baseline that
batching is measured against.

## The E0 experiment

E0 asks whether the reported rise in patient faithfulness under increasing
disclosure restriction is a property of the dialogues or of the way they were
measured. Two of its four confounds can be tested without regenerating
anything, by re-scoring an existing corpus:

1. **Reference scope** — the same dialogues scored against the policy context
   and against the full reference.
2. **Turn scope** — patient and doctor claims scored separately.

```bash
python scripts/run_e0.py \
    --dialogues /path/outside/repo/dialogues.jsonl \
    --references /path/outside/repo/references.jsonl \
    --out /path/outside/repo/e0-run
```

Both inputs are JSONL. A dialogue line is
`{"case_id", "dialogue_id", "policy", "dialogue": [{"role", "content"}, ...]}`;
a reference line is `{"case_id", "reference": {...}}`, keyed by case so that
the three policy arms of a case are scored against one identical reference.
The output directory must sit outside the repository — every file the run
writes is MIMIC-derived.

Claims are extracted once per dialogue and reused across both reference modes,
so the two modes cannot disagree because they were shown different claims. The
run appends results as it goes and skips completed work on restart. Every
figure in the report carries a 95% interval from a bootstrap that resamples
**cases**, not dialogues: a case's three arms are correlated, and resampling
them independently would understate every interval.

The report decomposes; it does not conclude. Tests 3 and 4 require
regeneration, and the manuscript framing is a decision for after all four.

## Building the cohort and the references

The pipeline is four commands, each consuming the previous one's output. It
starts from MIMIC-III's `ADMISSIONS`, `PATIENTS`, `NOTEEVENTS`, `ICUSTAYS`,
`PROCEDURES_ICD` and `DIAGNOSES_ICD` tables, read either from a CSV extract or
from BigQuery.

```bash
meddial-cohort \
    --csv-dir /path/outside/repo/mimic-iii \
    --out /path/outside/repo/cohort \
    --n 200

meddial-scr \
    --csv-dir /path/outside/repo/mimic-iii \
    --cohort /path/outside/repo/cohort/cohort_private_manifest.json \
    --out /path/outside/repo/references
```

`meddial-cohort` applies criteria E1–E10 to the **structured** fields only —
admission type, length of stay, ICD-9 code sets, a Quan et al. 2005 Charlson
index — and never to note vocabulary. Eligibility decided by reading the note
would make the cohort a function of the same text the study then measures
extraction against. It prints the exclusion count at each criterion and writes
them to the manifest, so the flow diagram is a by-product of selection rather
than something reconstructed afterwards.

The manifest also records a SHA-256 over the source CSVs. `meddial-scr`
refuses to run if the extract it is pointed at does not hash to the same
value, so a set of references cannot silently describe different data from the
cohort that claims them. Extraction is resumable: a case whose reference file
already exists is skipped, so an interrupted run continues where it stopped.

The same `--seed` and the same extract reproduce the same cohort hash. Change
either and you have a different cohort, which is the point.

### Reading MIMIC-III from BigQuery

PhysioNet publishes MIMIC-III on BigQuery as `physionet-data`. Pass
`--bigquery` to both commands to read it there instead of from local files:

```bash
export MIMIC_BIGQUERY_PROJECT=your-gcp-project-id   # BigQuery bills the reader

meddial-cohort --bigquery --out /path/outside/repo/cohort --n 200

meddial-scr --bigquery \
    --cohort /path/outside/repo/cohort/cohort_private_manifest.json \
    --out /path/outside/repo/references
```

It needs PhysioNet credentialing with BigQuery access approved for the same
Google account, and a Google Cloud project of your own; the six tables cost a
few GB of scan against the monthly free tier, and `--bq-max-gib` refuses a
query that would scan more than 64 GiB so a mistyped dataset name fails rather
than bills. Both backends share one reader
(`meddial/cohort/mimic_source.py`), so they yield the same candidates and the
same exclusion flow.

They do not yield the same `source_snapshot_hash`: the CSV backend hashes the
bytes of the files, and BigQuery hashes each table's id, row count and
last-modified time, prefixed `bigquery-sha256:`. Sampling is salted with that
value, so **the same seed draws a different sample from each backend** — a
cohort is a claim about one identified artefact, not about MIMIC-III in the
abstract. `meddial-scr` compares the hashes exactly and refuses to extract
against the backend the cohort did not come from. Pick one and build the whole
pipeline on it.

This does not touch decision D2 / GOV-3, which governs where MIMIC-derived text
is *sent*. Reading from BigQuery is the same direction as downloading the CSVs;
no note goes to a hosted model either way.

### Running it on Colab

`notebooks/meddial_colab.ipynb` runs the cohort and extraction steps on a Colab
GPU. BigQuery is what makes that possible — the CSV distribution does not fit in
a hosted runtime — and the GPU is what makes extraction finish, since it is
bound by how fast a local model reads a discharge summary. The notebook
installs Ollama into the runtime and serves the extractor on `localhost`, which
is the only shape of provider the restricted-clinical check accepts, and picks
the largest extractor tag the attached card's VRAM holds. Its output is
restricted-derived and a Colab runtime is ephemeral, so it packages the
directory and leaves the decision of where that archive goes to you.

Note that `gtmf_creation.main()` is withdrawn. It selected cases with
`is_light_common_case`, a keyword scan over the note body, which the criteria
forbid and which cannot reproduce by hash. Its extraction internals are
unchanged and are what `meddial-scr` calls.

`notebooks/gtmf_creation_colab.ipynb` drives those internals directly, for
looking at extraction rather than running the pipeline. It imports
`extract_gtmf`, `process_notes` and `is_light_common_case` from the module
rather than copying them, extracts one note under inspection — unresolved
evidence spans and unevidenced entities printed per case — and then runs the
batch. Cases come from a cohort manifest, or, for exploration only, from the
structured tables in a fixed order; note vocabulary never chooses them.

## Running an experiment

One command runs one experimental cell — one variant, one policy, one seed —
and a second regenerates every table from the records it wrote.

```bash
meddial-run \
    --config configs/experiments/full_meddial.json \
    --cases /path/outside/repo/cases.jsonl \
    --out /path/outside/repo/runs

meddial-tables \
    --attempts /path/outside/repo/runs/runs/<run_id>/attempts/attempts.jsonl \
    --out /path/outside/repo/tables
```

A case line is `{"case_id", "note_text", "reference": {...}}`. `note_text` is
the unstructured note and is read only by `direct_llm`, which exists to show
what the pipeline produces without a structured reference; every other variant
needs `reference`. References are extracted upstream, not per attempt — two
attempts on one case must be scored against one identical reference.

The five configs in `configs/experiments/` are deliberately unfrozen: their
model digests and input manifest read `UNPINNED:development` and `frozen_at`
is null, so `--confirmatory` refuses them. Freezing thresholds, prompts and
digests with a timestamp is a separate, recorded act that happens once, before
the confirmatory run.

Runs are resumable and attempts are append-only: re-running the same config
skips completed cases and cannot overwrite an existing record. Changing a
threshold or a prompt version changes the run identity, so a resumed run
refuses to mix incomparable attempts rather than appending to the old one.

`meddial-run` warns when the generator and judge share a model family. A judge
from the generator's family inherits its blind spots, so the score stops being
an independent check.

Both commands refuse an output directory inside the repository: everything
they write is derived from restricted data. Pass `--allow-in-repo` to override
that deliberately.

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
