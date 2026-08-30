"""Restricted-artifact guard.

Fails the build if any restricted or dead-under-D4 path is present in the
working tree. Run directly (``python scripts/check_repository_hygiene.py``)
or as a CI step. Exits non-zero and prints every offending path on failure.

Closes PRD GOV-1/GOV-2 (D-01): restricted MIMIC-derived data must never be
present in the working tree or committed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Directories that must never exist in this repository.
FORBIDDEN_DIRS = ("gtmf", "output_dialogue_framework")

# Files that must never exist (bundled external corpus, dead under D4).
FORBIDDEN_FILES = ("MTS-Dialog/MTS-Dialog.csv",)

# Any filename shaped like "<prefix>_<subject_id>_<hadm_id>.<ext>" is a
# restricted-data leak, regardless of which directory it turns up in.
IDENTIFIER_SHAPED_NAME = re.compile(r"_\d+_\d+(\.\w+)?$")


def find_violations(root: Path) -> list[str]:
    violations: list[str] = []

    for forbidden in FORBIDDEN_DIRS:
        candidate = root / forbidden
        if candidate.exists():
            violations.append(f"forbidden directory present: {candidate}")

    for forbidden in FORBIDDEN_FILES:
        candidate = root / forbidden
        if candidate.exists():
            violations.append(f"forbidden file present: {candidate}")

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.parts:
            continue
        if IDENTIFIER_SHAPED_NAME.search(path.stem + path.suffix):
            violations.append(f"identifier-shaped filename: {path}")

    return violations


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    violations = find_violations(root)

    if violations:
        print("Restricted-artifact guard FAILED:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print("Restricted-artifact guard passed: no forbidden paths found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
