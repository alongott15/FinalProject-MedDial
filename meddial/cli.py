"""Console entry points, declared in ``[project.scripts]``.

``meddial-cohort``
    Apply E1-E10 to a MIMIC-III extract and write the auditable private
    manifest: the selected cases, the exclusion flow, and the hashes that make
    the selection reproducible.

``meddial-scr``
    Extract one Structured Clinical Reference per selected admission. It reads
    a cohort manifest and will not select cases itself, so extraction cannot
    quietly widen the cohort the manifest describes.

``meddial-run``
    Execute one experimental cell -- one variant, one policy, one seed -- over
    a case file, writing immutable attempt records.

``meddial-tables``
    Rebuild every manuscript table and the primary figure from those records.
    M4 requires that no number in the write-up is transcribed by hand, which
    means exactly one command has to reproduce all of them.

The order is fixed and each step consumes the previous one's output:
``meddial-cohort`` -> ``meddial-scr`` -> ``meddial-run`` -> ``meddial-tables``.

``meddial-cohort`` and ``meddial-scr`` read MIMIC-III either from a CSV
directory (``--csv-dir``) or from ``physionet-data`` on BigQuery
(``--bigquery``), which is what lets the pipeline run in a hosted GPU runtime
that cannot hold the CSV distribution. The two backends select identically but
hash differently, so a cohort and its references must come from the same one.

Both inherit the constraints that shape ``scripts/run_e0.py``. **Decision D2 /
GOV-3:** generation and scoring send MIMIC-derived text to a model, so only a
local provider is offered and there is no flag that routes a case to a hosted
API -- a rule about where notes are *sent*, which reading MIMIC-III from its
credentialed archive does not touch. **Constraint C2:** everything these
commands write is derived from restricted data, so the output directory must
sit outside the repository unless the operator overrides that deliberately.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from meddial.analysis.tables import read_attempt_records, regenerate_tables
from meddial.cohort import (
    DEFAULT_COHORT_SIZE,
    DEFAULT_SAMPLING_SEED,
    CohortSelectionError,
)
from meddial.cohort.mimic_bigquery import (
    DEFAULT_CLINICAL_DATASET,
    DEFAULT_MAX_GIB_BILLED,
    DEFAULT_NOTES_DATASET,
)
from meddial.cohort.mimic_source import MimicSource
from meddial.experiments import (
    ExperimentRunner,
    MedDialBackend,
    RunConfigError,
    load_run_config,
)
from meddial.llm import (
    LocalOpenAICompatibleProvider,
    ProviderError,
    resolve_ollama_digest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_GENERATOR = "mistral-small3.2:24b"
DEFAULT_JUDGE = "qwen3.5:9b"
DEFAULT_EXTRACTOR = "qwen3.5:35b"
"""Implementation Plan §12.4: use the largest extractor the hardware allows.

Extraction errors propagate into every downstream metric, so this defaults
higher than the judge rather than matching it. The plan names ``qwen3.5:32b``,
which Ollama does not publish; ``35b`` is the tag that exists.

At Q4 this needs roughly 24 GB of weights before any KV cache, so on a smaller
machine pass ``--extractor`` with the largest tag that fits. Swapping is not a
slow run, it is a stalled one.
"""


MIMIC_CSV_DIR_ENV = "MIMIC_CSV_DIR"
MIMIC_BIGQUERY_PROJECT_ENV = "MIMIC_BIGQUERY_PROJECT"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # python-dotenv is a declared dependency; tolerate absence
        pass


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    """Where MIMIC-III is read from: a CSV directory, or BigQuery.

    Both backends produce the same candidates and the same exclusion flow --
    they share :class:`~meddial.cohort.mimic_source.MimicSource` -- but not the
    same snapshot hash, because one identifies bytes on disk and the other
    identifies tables in a warehouse. Sampling is salted with that hash, so the
    same seed draws a different sample from each. Select a cohort and extract
    its references from the same backend; ``meddial-scr`` refuses the mixture.
    """
    parser.add_argument(
        "--csv-dir",
        type=Path,
        default=None,
        help=f"MIMIC-III CSV directory [${MIMIC_CSV_DIR_ENV}]",
    )
    parser.add_argument(
        "--bigquery",
        action="store_true",
        help=(
            "read MIMIC-III from BigQuery (physionet-data) instead of local CSVs. "
            "Requires PhysioNet credentialing linked to a Google account, and a "
            "billing project of your own -- BigQuery bills the reader."
        ),
    )
    parser.add_argument(
        "--bq-project",
        default=None,
        help=f"Google Cloud project billed for the queries [${MIMIC_BIGQUERY_PROJECT_ENV}]",
    )
    parser.add_argument(
        "--bq-clinical-dataset",
        default=DEFAULT_CLINICAL_DATASET,
        help=f"structured tables [{DEFAULT_CLINICAL_DATASET}]",
    )
    parser.add_argument(
        "--bq-notes-dataset",
        default=DEFAULT_NOTES_DATASET,
        help=f"NOTEEVENTS [{DEFAULT_NOTES_DATASET}]",
    )
    parser.add_argument(
        "--bq-max-gib",
        type=float,
        default=DEFAULT_MAX_GIB_BILLED,
        help=f"refuse a query scanning more than this many GiB [{DEFAULT_MAX_GIB_BILLED}]",
    )


def _open_source(args: argparse.Namespace) -> MimicSource:
    """The MIMIC-III source these flags name, opened and validated.

    Every caller downstream holds it as a
    :class:`~meddial.cohort.mimic_source.MimicSource` and cannot tell which
    backend it got, which is the point: the cohort must not depend on where the
    extract was stored.
    """
    from meddial.cohort.mimic_csv import MimicCsvError, MimicCsvSource

    _load_env()
    if args.bigquery:
        from meddial.cohort.mimic_bigquery import MimicBigQueryError, MimicBigQuerySource

        project = args.bq_project or os.environ.get(MIMIC_BIGQUERY_PROJECT_ENV)
        if not project:
            raise SystemExit(
                "No BigQuery billing project: pass --bq-project, or set "
                f"{MIMIC_BIGQUERY_PROJECT_ENV} in the environment or a .env file. "
                "BigQuery bills the project that runs the query, so there is no default."
            )
        args.bq_project = project
        try:
            return MimicBigQuerySource(
                project,
                clinical_dataset=args.bq_clinical_dataset,
                notes_dataset=args.bq_notes_dataset,
                max_gib_billed=args.bq_max_gib,
            )
        except MimicBigQueryError as exc:
            raise SystemExit(f"Source error: {exc}") from exc

    # The extract lives outside the repository under C2, so its path is a
    # per-machine setting rather than something to retype on every command. An
    # explicit flag always wins, so a second extract can be pointed at without
    # editing the environment.
    from_env = os.environ.get(MIMIC_CSV_DIR_ENV)
    csv_dir = args.csv_dir or (Path(from_env) if from_env else None)
    if csv_dir is None:
        raise SystemExit(
            f"No MIMIC-III source: pass --csv-dir, set {MIMIC_CSV_DIR_ENV} in the "
            "environment or a .env file, or pass --bigquery to read from "
            "physionet-data on BigQuery."
        )
    args.csv_dir = csv_dir
    try:
        return MimicCsvSource(csv_dir)
    except MimicCsvError as exc:
        raise SystemExit(f"Source error: {exc}") from exc


def _source_flags(args: argparse.Namespace) -> str:
    """The flags that would reopen this same source, for a printed next step."""
    if args.bigquery:
        flags = "--bigquery"
        if args.bq_project:
            flags += f" --bq-project {args.bq_project}"
        return flags
    return f"--csv-dir {args.csv_dir}"


def check_output_location(out: Path, *, allowed: bool) -> None:
    """Keep output derived from restricted data out of the working tree (C2)."""
    resolved = out.resolve()
    if allowed or not (resolved == REPO_ROOT or REPO_ROOT in resolved.parents):
        return
    raise SystemExit(
        f"Refusing to write output to {resolved}: it is inside the repository, and every "
        "file this run produces is derived from restricted data (constraint C2). Choose a "
        "path outside the repository, or pass --allow-in-repo deliberately."
    )


def _local_provider(
    base_url: str,
    model_id: str,
    *,
    family: str,
    quantisation: str,
    reasoning_effort: str | None = None,
    timeout_s: float | None = None,
) -> LocalOpenAICompatibleProvider:
    """Build a local provider carrying the digest of the weights actually served.

    The digest, not the tag, is what a result is attributed to: tags are
    mutable, so a run pinned only by name cannot be reproduced (C8).
    """
    digest = resolve_ollama_digest(base_url, model_id)
    return LocalOpenAICompatibleProvider(
        base_url,
        model_id,
        model_digest=digest,
        model_family=family,
        quantisation=quantisation,
        reasoning_effort=reasoning_effort,
        **({} if timeout_s is None else {"timeout_s": timeout_s}),
    )


def _read_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no}: not valid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise SystemExit(f"{path}:{line_no}: expected a JSON object")
            cases.append(value)
    if not cases:
        raise SystemExit(f"{path} holds no cases")
    return cases


# ---------------------------------------------------------------------------
# meddial-cohort
# ---------------------------------------------------------------------------


def _cohort_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meddial-cohort",
        description="Apply E1-E10 to a MIMIC-III extract and write the private manifest.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_source_arguments(parser)
    parser.add_argument("--out", type=Path, required=True, help="output directory, outside the repo")
    parser.add_argument("--n", type=int, default=DEFAULT_COHORT_SIZE, help="cases to select")
    parser.add_argument("--seed", type=int, default=DEFAULT_SAMPLING_SEED, help="sampling seed")
    parser.add_argument(
        "--allow-in-repo",
        action="store_true",
        help="permit an output directory inside the repository (C2: normally refused)",
    )
    return parser


def cohort_main(argv: list[str] | None = None) -> int:
    from meddial.cohort import create_private_manifest, select_cohort

    args = _cohort_parser().parse_args(argv)
    check_output_location(args.out, allowed=args.allow_in_repo)

    source = _open_source(args)

    print("Identifying the source extract (once; NOTEEVENTS is large)...")
    snapshot = source.snapshot_hash()
    print(f"Source snapshot: {snapshot}")

    print("Reading candidates...")
    records = list(source.admission_records())
    print(f"Candidates: {len(records)}")

    try:
        selection = select_cohort(
            records, source_snapshot_hash=snapshot, n_cases=args.n, seed=args.seed
        )
    except CohortSelectionError as exc:
        raise SystemExit(f"Selection refused: {exc}") from exc

    manifest = create_private_manifest(selection)
    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / "cohort_private_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print()
    print("Exclusion flow:")
    for stage in manifest.get("exclusion_flow", []):
        label = stage.get("label", stage.get("criterion", "?"))
        print(f"  {stage.get('criterion', ''):>4}  {label}: -{stage.get('excluded', 0)}")
    if selection.malformed:
        print()
        print(f"Unevaluable candidates (excluded, reported): {len(selection.malformed)}")
        reasons: dict[str, int] = {}
        for row in selection.malformed:
            reasons[row.reason] = reasons.get(row.reason, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>6}  {reason}")

    print()
    print(f"Eligible pool: {selection.eligible_pool_size}")
    print(f"Selected:      {len(selection.selected)}")
    print(f"Cohort hash:   {selection.cohort_hash}")
    print(f"Wrote {manifest_path}")
    print(f"Next: meddial-scr {_source_flags(args)} --cohort {manifest_path} --out <dir>")
    return 0


# ---------------------------------------------------------------------------
# meddial-scr
# ---------------------------------------------------------------------------


def _scr_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meddial-scr",
        description="Extract a Structured Clinical Reference per admission in a cohort manifest.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_source_arguments(parser)
    parser.add_argument(
        "--cohort", type=Path, required=True, help="cohort_private_manifest.json"
    )
    parser.add_argument("--out", type=Path, required=True, help="output directory, outside the repo")
    parser.add_argument("--extractor", default=DEFAULT_EXTRACTOR, help=f"[{DEFAULT_EXTRACTOR}]")
    parser.add_argument("--extractor-family", default="qwen")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--quantisation", default="Q4_K_M")
    parser.add_argument("--limit", type=int, default=None, help="extract only the first N cases")
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        help=(
            "seconds to wait for one extraction call [1800]. The provider's 300s "
            "default is a chat timeout: a whole-note extraction on modest hardware "
            "exceeds it, and because a timeout is retried, each case then burns "
            "three full timeouts and produces nothing."
        ),
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=4096,
        help=(
            "completion budget per chunk [4096]. The GTMF schema alone is ~4.8k "
            "characters and a truncated response is unparseable, so this is sized "
            "for the answer rather than for the prompt."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        default="none",
        help=(
            "reasoning budget for the extractor [none]. Extraction emits a fixed "
            "schema, so reasoning tokens are cost without benefit -- and on a "
            "reasoning model they consume the whole max_tokens budget and leave "
            "the message empty. Pass 'default' to restore the server's setting."
        ),
    )
    parser.add_argument(
        "--allow-in-repo",
        action="store_true",
        help="permit an output directory inside the repository (C2: normally refused)",
    )
    return parser


def scr_main(argv: list[str] | None = None) -> int:
    from gtmf_creation import extract_gtmf
    from meddial.knowledge import Demographics
    from Utils.markdown_gtmf import save_gtmf_markdown

    args = _scr_parser().parse_args(argv)
    check_output_location(args.out, allowed=args.allow_in_repo)
    if args.reasoning_effort == "default":
        args.reasoning_effort = None

    try:
        manifest = json.loads(args.cohort.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cohort manifest error: {exc}") from exc
    selected = manifest.get("selected")
    if not isinstance(selected, list) or not selected:
        raise SystemExit(f"{args.cohort} names no selected cases")
    wanted = {(int(c["subject_id"]), int(c["hadm_id"])) for c in selected}
    print(f"Cohort: {len(wanted)} case(s), cohort_hash={manifest.get('cohort_hash')}")

    source = _open_source(args)

    snapshot = source.snapshot_hash()
    if snapshot != manifest.get("source_snapshot_hash"):
        # Extracting from a different extract than the cohort was selected from
        # would silently break the link between the manifest and the references.
        # A CSV hash and a BigQuery hash never compare equal, so this also
        # catches selecting from one backend and extracting from the other.
        raise SystemExit(
            "Refusing to extract: this MIMIC-III source does not match the one the cohort "
            f"was selected from.\n  manifest: {manifest.get('source_snapshot_hash')}\n  "
            f"this source: {snapshot}"
        )

    try:
        provider = _local_provider(
            args.base_url,
            args.extractor,
            family=args.extractor_family,
            quantisation=args.quantisation,
            reasoning_effort=args.reasoning_effort,
            timeout_s=args.timeout,
        )
    except ProviderError as exc:
        raise SystemExit(f"Provider error: {exc}") from exc

    demographics = source.demographics()
    args.out.mkdir(parents=True, exist_ok=True)

    todo = [r for r in source.admission_records() if (r.subject_id, r.hadm_id) in wanted]
    todo.sort(key=lambda r: (r.subject_id, r.hadm_id))
    if args.limit is not None:
        todo = todo[: args.limit]

    written = skipped = failed = 0
    for index, record in enumerate(todo, start=1):
        case_id = f"{record.subject_id}_{record.hadm_id}"
        out_path = args.out / f"scr_{case_id}.md"
        if out_path.exists():
            skipped += 1
            continue
        print(f"[{index}/{len(todo)}] {case_id} ({len(record.note_text)} chars)")
        try:
            reference = extract_gtmf(
                record.note_text,
                provider,
                note_id=case_id,
                max_tokens=args.max_tokens,
            )
        except ProviderError:
            # A provider outage is a run failure, not a case to skip: skipping
            # would leave a cohort silently short of the manifest it claims.
            raise
        except Exception as exc:  # noqa: BLE001 - one bad note must not end the run
            failed += 1
            print(f"    extraction failed: {type(exc).__name__}: {exc}")
            continue

        extra = demographics.get((record.subject_id, record.hadm_id), {})
        reference = reference.model_copy(
            update={
                "row_id": record.row_id or 0,
                "subject_id": record.subject_id,
                "hadm_id": record.hadm_id,
                "context": reference.context.model_copy(
                    update={
                        "demographics": Demographics.model_validate(
                            {
                                "Age": int(record.age_years),
                                "Admission_Date": record.admittime.isoformat(),
                                "Discharge_Date": record.dischtime.isoformat(),
                                **extra,
                            }
                        )
                    }
                ),
            }
        )
        save_gtmf_markdown(reference, str(out_path))
        written += 1

    print()
    print(f"Written: {written} · already present: {skipped} · failed: {failed}")
    print(f"References in {args.out}")
    if failed:
        print("Re-run to retry the failed cases; completed ones are skipped.")
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# meddial-run
# ---------------------------------------------------------------------------


def _run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meddial-run",
        description="Run one experimental cell over a case file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, required=True, help="run config JSON")
    parser.add_argument("--cases", type=Path, required=True, help="JSONL, one case per line")
    parser.add_argument("--out", type=Path, required=True, help="output root, outside the repo")
    parser.add_argument("--run-id", default=None, help="resume this run id instead of deriving one")
    parser.add_argument("--generator", default=DEFAULT_GENERATOR, help=f"[{DEFAULT_GENERATOR}]")
    parser.add_argument("--generator-family", default="mistral")
    parser.add_argument("--judge", default=DEFAULT_JUDGE, help=f"[{DEFAULT_JUDGE}]")
    parser.add_argument("--judge-family", default="qwen")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--quantisation", default="Q4_K_M")
    parser.add_argument(
        "--confirmatory",
        action="store_true",
        help="require a frozen config with pinned digests and manifest (M4)",
    )
    parser.add_argument(
        "--allow-in-repo",
        action="store_true",
        help="permit an output directory inside the repository (C2: normally refused)",
    )
    return parser


def run_main(argv: list[str] | None = None) -> int:
    args = _run_parser().parse_args(argv)
    check_output_location(args.out, allowed=args.allow_in_repo)

    try:
        config = load_run_config(args.config)
    except RunConfigError as exc:
        raise SystemExit(f"Config error: {exc}") from exc

    cases = _read_cases(args.cases)
    print(f"Config: {config.name} · variant={config.variant} · policy={config.patient_policy_ref}")
    print(f"Cases: {len(cases)}")

    try:
        generator = _local_provider(
            args.base_url,
            args.generator,
            family=args.generator_family,
            quantisation=args.quantisation,
        )
        judge = _local_provider(
            args.base_url,
            args.judge,
            family=args.judge_family,
            quantisation=args.quantisation,
        )
    except ProviderError as exc:
        raise SystemExit(f"Provider error: {exc}") from exc

    if generator.model_family == judge.model_family:
        # Judge independence: a judge drawn from the generator's family
        # inherits its blind spots, so the score stops being a check.
        print(
            f"WARNING: generator and judge are both family {generator.model_family!r}; "
            "judge independence is not satisfied."
        )

    runner = ExperimentRunner(args.out)
    try:
        result = runner.run(
            config,
            cases,
            MedDialBackend(generator=generator, judge=judge),
            requested_run_id=args.run_id,
            confirmatory=args.confirmatory,
        )
    except (RunConfigError, ProviderError) as exc:
        raise SystemExit(f"Run refused: {exc}") from exc

    context = result.context
    print(f"Run id: {context.run_id}")
    print(f"Attempts recorded: {len(result.attempts)}")
    print(f"Records: {context.attempts_path}")
    print(f"Regenerate tables with: meddial-tables --attempts {context.attempts_path} --out <dir>")
    return 0


# ---------------------------------------------------------------------------
# meddial-tables
# ---------------------------------------------------------------------------


def _tables_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meddial-tables",
        description="Regenerate every manuscript table and the primary figure.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--attempts",
        type=Path,
        nargs="+",
        required=True,
        help="one or more attempts.jsonl files, pooled across runs",
    )
    parser.add_argument("--out", type=Path, required=True, help="output root, outside the repo")
    parser.add_argument("--run-id", default=None, help="label for this analysis pass")
    parser.add_argument(
        "--resamples", type=int, default=2000, help="bootstrap replicates, >=2000"
    )
    parser.add_argument("--seed", type=int, default=0, help="resampling seed, recorded")
    parser.add_argument(
        "--primary-metric",
        default="patient_factuality",
        help="metric plotted in the primary figure",
    )
    parser.add_argument(
        "--allow-in-repo",
        action="store_true",
        help="permit an output directory inside the repository (C2: normally refused)",
    )
    return parser


def tables_main(argv: list[str] | None = None) -> int:
    args = _tables_parser().parse_args(argv)
    check_output_location(args.out, allowed=args.allow_in_repo)

    records = read_attempt_records(args.attempts)
    print(f"Read {len(records)} attempt record(s) from {len(args.attempts)} file(s).")

    outputs = regenerate_tables(
        records,
        output_root=args.out,
        run_id=args.run_id,
        resamples=args.resamples,
        seed=args.seed,
        primary_metric=args.primary_metric,
    )

    print(f"Analysis id: {outputs.run_id}")
    for path in outputs.files:
        print(f"  {path}")
    print(f"Wrote {len(outputs.files)} file(s) to {outputs.output_dir}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - reached via the console scripts
    raise SystemExit(run_main())
