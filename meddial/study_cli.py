"""Generate a frozen CMPB study plan without making model calls."""

from __future__ import annotations

import argparse

from meddial.experiments.study import write_recommended_plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Write the recommended MedDial study plan")
    parser.add_argument("--cohort-manifest", required=True)
    parser.add_argument("--generation-model", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    write_recommended_plan(args.output, args.cohort_manifest, args.generation_model)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
