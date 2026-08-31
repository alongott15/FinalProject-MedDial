"""Console entry points, declared in ``[project.scripts]``.

``meddial-cohort``
    Apply E1-E10 to a MIMIC-III CSV extract and write the auditable private
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

Both inherit the constraints that shape ``scripts/run_e0.py``. **Decision D2 /
GOV-3:** generation and scoring send MIMIC-derived text to a model, so only a
local provider is offered and there is no flag that routes a case to a hosted
API. **Constraint C2:** everything these commands write is derived from
restricted data, so the output directory must sit outside the repository
unless the operator overrides that deliberately.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from meddial.analysis.tables import read_attempt_records, regenerate_tables
from meddial.cohort import (
    DEFAULT_COHORT_SIZE,
    DEFAULT_SAMPLING_SEED,
    CohortSelectionError,
)
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
DEFAULT_EXTRACTOR = "qwen3.5:32b"
"""Implementation Plan §12.4: use the largest extractor the hardware allows.

Extraction errors propagate into every downstream metric, so this defaults
higher than the judge rather than matching it.
"""


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
    base_url: str, model_id: str, *, family: str, quantisation: str
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
        description="Apply E1-E10 to a MIMIC-III CSV extract and write the private manifest.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--csv-dir", type=Path, required=True, help="MIMIC-III CSV directory")
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
    from meddial.cohort.mimic_csv import MimicCsvError, MimicCsvSource

    args = _cohort_parser().parse_args(argv)
    check_output_location(args.out, allowed=args.allow_in_repo)

    try:
        source = MimicCsvSource(args.csv_dir)
    except MimicCsvError as exc:
        raise SystemExit(f"Source error: {exc}") from exc

    print("Hashing the source extract (once; NOTEEVENTS is large)...")
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
    print()
    print(f"Eligible pool: {selection.eligible_pool_size}")
    print(f"Selected:      {len(selection.selected)}")
    print(f"Cohort hash:   {selection.cohort_hash}")
    print(f"Wrote {manifest_path}")
    print(f"Next: meddial-scr --csv-dir {args.csv_dir} --cohort {manifest_path} --out <dir>")
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
    parser.add_argument("--csv-dir", type=Path, required=True, help="MIMIC-III CSV directory")
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
        "--allow-in-repo",
        action="store_true",
        help="permit an output directory inside the repository (C2: normally refused)",
    )
    return parser


def scr_main(argv: list[str] | None = None) -> int:
    from gtmf_creation import extract_gtmf_chunked
    from meddial.cohort.mimic_csv import MimicCsvError, MimicCsvSource
    from meddial.knowledge import Demographics
    from Utils.markdown_gtmf import save_gtmf_markdown

    args = _scr_parser().parse_args(argv)
    check_output_location(args.out, allowed=args.allow_in_repo)

    try:
        manifest = json.loads(args.cohort.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cohort manifest error: {exc}") from exc
    selected = manifest.get("selected")
    if not isinstance(selected, list) or not selected:
        raise SystemExit(f"{args.cohort} names no selected cases")
    wanted = {(int(c["subject_id"]), int(c["hadm_id"])) for c in selected}
    print(f"Cohort: {len(wanted)} case(s), cohort_hash={manifest.get('cohort_hash')}")

    try:
        source = MimicCsvSource(args.csv_dir)
    except MimicCsvError as exc:
        raise SystemExit(f"Source error: {exc}") from exc

    snapshot = source.snapshot_hash()
    if snapshot != manifest.get("source_snapshot_hash"):
        # Extracting from a different extract than the cohort was selected from
        # would silently break the link between the manifest and the references.
        raise SystemExit(
            "Refusing to extract: this CSV extract does not match the one the cohort was "
            f"selected from.\n  manifest: {manifest.get('source_snapshot_hash')}\n  "
            f"this dir: {snapshot}"
        )

    try:
        provider = _local_provider(
            args.base_url,
            args.extractor,
            family=args.extractor_family,
            quantisation=args.quantisation,
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
            reference = extract_gtmf_chunked(
                record.note_text, provider, note_id=case_id
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
