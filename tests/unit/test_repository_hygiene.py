"""Regression tests for the restricted-artifact guard (GOV-1, GOV-2).

Named test per PRD defect D-01: restricted MIMIC-derived data must not be
present in the working tree, and a deliberate restricted-path addition must
be rejected.
"""

from pathlib import Path

from scripts.check_repository_hygiene import find_violations


def test_ci_guard_rejects_restricted_paths(tmp_path: Path) -> None:
    """A deliberate restricted-artifact commit is rejected by the guard."""
    (tmp_path / "gtmf").mkdir()
    (tmp_path / "gtmf" / "gtmf_10096_182988.md").write_text("restricted")

    violations = find_violations(tmp_path)

    assert violations, "guard must flag a restricted gtmf/ path"
    assert any("gtmf" in v for v in violations)


def test_ci_guard_rejects_identifier_shaped_filenames(tmp_path: Path) -> None:
    """A subject_id/hadm_id-shaped filename is flagged anywhere in the tree."""
    (tmp_path / "somewhere").mkdir()
    (tmp_path / "somewhere" / "dialogue_10096_182988.md").write_text("x")

    violations = find_violations(tmp_path)

    assert violations
    assert any("identifier-shaped" in v for v in violations)


def test_ci_guard_passes_on_clean_tree(tmp_path: Path) -> None:
    """A tree with none of the forbidden paths produces no violations."""
    (tmp_path / "meddial").mkdir()
    (tmp_path / "meddial" / "__init__.py").write_text("")

    assert find_violations(tmp_path) == []


def test_current_repository_passes_the_guard() -> None:
    """The actual repository, as checked out, currently has no violations.

    This is the regression test for D-01: it fails on the pre-cleanup state
    of branch `new` (498 files under gtmf/, 1,494 under
    output_dialogue_framework/) and passes once W0 removes them.
    """
    repo_root = Path(__file__).resolve().parents[2]

    assert find_violations(repo_root) == []
