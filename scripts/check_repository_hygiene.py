"""Fail CI if restricted or separately licensed generated artifacts reappear."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATTERNS = (
    "gtmf/gtmf_*.md",
    "output_dialogue_framework/dialogue_*.md",
    "MTS-Dialog/*.csv",
)


def main() -> int:
    forbidden = sorted(
        path.relative_to(ROOT)
        for pattern in FORBIDDEN_PATTERNS
        for path in ROOT.glob(pattern)
        if path.is_file()
    )
    if forbidden:
        print("Restricted or separately licensed artifacts must not be committed:")
        for path in forbidden:
            print(f"- {path}")
        return 1
    print("Repository artifact hygiene check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
