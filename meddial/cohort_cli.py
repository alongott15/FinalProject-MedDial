"""Command-line helpers for private clinical review and safe release manifests."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from meddial.clinical_review import create_review_template
from meddial.cohort import create_release_manifest


def _template(args: argparse.Namespace) -> int:
    from Utils.csv_data_loader import CSVDataLoader

    loader = CSVDataLoader(args.mimic_dir)
    candidates = loader.fetch_notes_with_light_case_filter(
        limit=args.candidate_limit,
        seed=args.seed,
    )
    create_review_template(candidates, args.output)
    return 0


def _release(args: argparse.Namespace) -> int:
    salt = os.getenv(args.salt_env)
    if not salt:
        raise ValueError(f"{args.salt_env} must contain the private publication salt")
    with Path(args.private_manifest).open(encoding="utf-8") as handle:
        private_manifest = json.load(handle)
    release = create_release_manifest(private_manifest, publication_salt=salt)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as handle:
        json.dump(release, handle, indent=2, ensure_ascii=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MedDial cohort-review utilities")
    subparsers = parser.add_subparsers(required=True)
    template = subparsers.add_parser("review-template")
    template.add_argument("--mimic-dir", required=True)
    template.add_argument("--output", required=True)
    template.add_argument("--candidate-limit", type=int, default=1000)
    template.add_argument("--seed", type=int, default=42)
    template.set_defaults(handler=_template)

    release = subparsers.add_parser("release-manifest")
    release.add_argument("--private-manifest", required=True)
    release.add_argument("--output", required=True)
    release.add_argument("--salt-env", default="MEDDIAL_PUBLICATION_SALT")
    release.set_defaults(handler=_release)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
