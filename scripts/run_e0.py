#!/usr/bin/env python3
"""Run E0 tests 1 and 2 — re-score an existing corpus, no regeneration.

    python scripts/run_e0.py \\
        --dialogues /path/to/dialogues.jsonl \\
        --references /path/to/references.jsonl \\
        --out /path/to/e0-run

Two constraints shape this script.

**GOV-3 / decision D2.** Re-scoring sends MIMIC-derived text to a model, so
only a local provider is offered. There is no flag that routes this corpus to
a hosted API.

**Constraint C2.** Everything written here is derived from restricted data,
so the output directory must sit outside the repository. The script refuses
otherwise unless the operator overrides it deliberately.

The run is resumable: results are appended per dialogue per reference mode,
and completed work is skipped on the next invocation. 450 dialogues is hours
of local inference and will be interrupted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from meddial.experiments import (
    CorpusError,
    analyse,
    load_corpus,
    read_results,
    render_report,
    score_corpus,
)
from meddial.llm import (
    LocalOpenAICompatibleProvider,
    ProviderError,
    resolve_ollama_digest,
)
from meddial.stats import StatsError

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_JUDGE = "qwen2.5:14b"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dialogues", type=Path, required=True, help="JSONL of existing dialogues")
    parser.add_argument("--references", type=Path, required=True, help="JSONL of SCRs by case_id")
    parser.add_argument("--out", type=Path, required=True, help="output directory, outside the repo")
    parser.add_argument("--judge", default=DEFAULT_JUDGE, help=f"judge model [{DEFAULT_JUDGE}]")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model-family", default="qwen")
    parser.add_argument("--quantisation", default="Q4_K_M")
    parser.add_argument("--seed", type=int, default=20260914, help="sampling seed, recorded")
    parser.add_argument("--resamples", type=int, default=2000, help="bootstrap replicates, >=2000")
    parser.add_argument("--limit", type=int, default=None, help="score only the first N dialogues")
    parser.add_argument(
        "--analyse-only",
        action="store_true",
        help="skip scoring and rebuild the report from an existing results.jsonl",
    )
    parser.add_argument(
        "--allow-in-repo",
        action="store_true",
        help="permit an output directory inside the repository (C2: normally refused)",
    )
    return parser


def check_output_location(out: Path, *, allowed: bool) -> None:
    """Keep MIMIC-derived output out of the working tree (C2)."""
    resolved = out.resolve()
    if allowed or not (resolved == REPO_ROOT or REPO_ROOT in resolved.parents):
        return
    raise SystemExit(
        f"Refusing to write E0 output to {resolved}: it is inside the repository, and every "
        "file this run produces is MIMIC-derived (constraint C2). Choose a path outside the "
        "repository, or pass --allow-in-repo deliberately."
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    check_output_location(args.out, allowed=args.allow_in_repo)

    args.out.mkdir(parents=True, exist_ok=True)
    results_path = args.out / "results.jsonl"

    try:
        records = load_corpus(args.dialogues, args.references)
    except CorpusError as exc:
        raise SystemExit(f"Corpus error: {exc}") from exc

    if args.limit is not None:
        records = records[: args.limit]
    cases = {record.case_id for record in records}
    print(f"Corpus: {len(records)} dialogues over {len(cases)} cases.")

    if not args.analyse_only:
        try:
            digest = resolve_ollama_digest(args.base_url, args.judge)
            provider = LocalOpenAICompatibleProvider(
                args.base_url,
                args.judge,
                model_digest=digest,
                model_family=args.model_family,
                quantisation=args.quantisation,
            )
        except ProviderError as exc:
            raise SystemExit(f"Provider error: {exc}") from exc

        print(f"Judge: {args.judge} @ {digest}")
        print(f"Appending to {results_path} (resumable; rerun to continue).")
        fresh = score_corpus(records, provider=provider, results_path=results_path, seed=args.seed)
        print(f"Scored {len(fresh)} dialogue/reference-mode pairs this run.")

    try:
        scored = read_results(results_path)
    except CorpusError as exc:
        raise SystemExit(f"Results error: {exc}") from exc

    try:
        report = analyse(scored, resamples=args.resamples)
    except StatsError as exc:
        raise SystemExit(f"Nothing to report: {exc}") from exc

    (args.out / "report.json").write_text(
        json.dumps(report.as_record(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    markdown = render_report(report)
    (args.out / "report.md").write_text(markdown, encoding="utf-8")

    print()
    print(markdown)
    print(f"Wrote {args.out / 'report.json'} and {args.out / 'report.md'}.")
    print(
        "Tests 3 and 4 need regeneration. Plan §6 step 5 — the manuscript framing — "
        "is not settled by this report."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
