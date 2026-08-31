"""Console entry points: run an experiment, regenerate every table.

Two commands, declared in ``[project.scripts]``:

``meddial-run``
    Execute one experimental cell -- one variant, one policy, one seed -- over
    a case file, writing immutable attempt records.

``meddial-tables``
    Rebuild every manuscript table and the primary figure from those records.
    M4 requires that no number in the write-up is transcribed by hand, which
    means exactly one command has to reproduce all of them.

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
