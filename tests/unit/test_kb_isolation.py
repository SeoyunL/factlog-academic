"""A KB's vocabulary must not depend on how you named it (#226).

entity_set took attribute relations from the target KB but read the alias map from
the AMBIENT root, so the same KB answered differently under --target than under
FACTLOG_ROOT -- the ambient KB's alias file leaked into the target's vocabulary, and
an ambient alias file with an error could even kill a query about an unrelated KB.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

FACTS = (
    "subject,relation,object,source,status,confidence,note\n"
    "갑,통합,을,sources/a.md,accepted,0.9,\n"
    "을,게재연도,2020,sources/a.md,accepted,0.9,\n"
)


def _kb(root: Path, *, with_aliases: bool) -> Path:
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(root)],
        capture_output=True,
        check=True,
        cwd=REPO,
    )
    (root / "sources" / "a.md").write_text("a\n")
    (root / "facts" / "candidates.csv").write_text(FACTS)
    # declares the CANONICAL while the facts carry the alias
    (root / "policy" / "attribute-relations.md").write_text("published_year\n")
    if with_aliases:
        (root / "policy" / "relation-aliases.md").write_text("- `게재연도` -> `published_year`\n")
    return root


def _vocab_line(cwd_root: Path, target: Path) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "factlog", "status", "--target", str(target)],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={"PATH": "/usr/bin:/bin", "FACTLOG_ROOT": str(cwd_root), "PYTHONPATH": str(REPO)},
    )
    if proc.returncode != 0:
        pytest.fail(f"status failed: {proc.stderr[-300:]}")
    return next(ln for ln in proc.stdout.splitlines() if "vocabulary:" in ln)


def test_target_and_ambient_agree(tmp_path):
    a = _kb(tmp_path / "a", with_aliases=True)
    ambient = _kb(tmp_path / "b", with_aliases=False)  # no alias file of its own
    assert _vocab_line(ambient, a) == _vocab_line(a, a)


def test_the_alias_is_resolved_from_the_target_kb(tmp_path):
    """The declared canonical must filter the aliased fact — 2020 is a literal."""
    a = _kb(tmp_path / "a", with_aliases=True)
    line = _vocab_line(a, a)
    assert "1 literal(s)" in line, line
