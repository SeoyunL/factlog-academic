# SPDX-License-Identifier: Apache-2.0
"""Install instructions must not point at the wrong PyPI package (#228).

`factlog` on PyPI is an unrelated 2013 project ("File ACTivity LOGger", v0.0.1,
no extras). Telling a user to run `pip install 'factlog[zotero]'` installs THAT —
and because the extra does not exist there, pip only warns and exits 0, so the
user sees success, gets a package they never asked for, and none of pyzotero /
httpx / feedparser. The integration then keeps failing and its error message
hands them the same command again.

So: the distribution is named `factlog-academic` (a bare `pip install
factlog-academic[...]` fails loudly rather than resolving to a squatter), and no
instruction anywhere may spell the bare `factlog[...]` form.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# The command that installs the wrong package. Matched over WHITESPACE-NORMALISED
# text, not line by line: the first version of this guard was line-based and a
# prose line-wrap ("pip install\n'factlog[zotero]'") walked straight through it —
# the offender it was written to catch was still in README.ko.md and the test was
# green.
BAD = re.compile(r"pip install\s+['\"`]?factlog\[")


def _normalised(path: Path) -> str:
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


SEARCHED = [
    *(REPO / "factlog").rglob("*.py"),
    *(REPO / "tools").glob("*.py"),
    *REPO.glob("README*.md"),
    *(REPO / "docs").glob("*.md"),
    *(REPO / "skills").rglob("*.md"),
    *(REPO / "commands").rglob("*.md"),
    *(REPO / "hooks").rglob("*"),
    *(REPO / ".github").rglob("*.yml"),
    *REPO.glob("tests/*.sh"),
    REPO / "pyproject.toml",
    REPO / "requirements.txt",
]


def test_distribution_is_not_named_factlog():
    # The PyPI name `factlog` is taken. Ours must not collide, so a typo cannot
    # silently resolve to someone else's package.
    meta = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert meta["project"]["name"] == "factlog-academic"


def test_the_extras_still_exist():
    meta = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    extras = meta["project"]["optional-dependencies"]
    assert {"zotero", "openalex", "arxiv", "pubmed"} <= set(extras)


def test_nothing_tells_the_user_to_pip_install_factlog():
    offenders = [
        str(path.relative_to(REPO))
        for path in SEARCHED
        if path.is_file() and BAD.search(_normalised(path))
    ]
    assert not offenders, (
        "these files instruct `pip install factlog[...]`, which installs the unrelated PyPI "
        "package: " + ", ".join(sorted(set(offenders)))
    )


def test_integration_errors_name_the_right_package():
    # The runtime message a user hits when a dependency is missing is the one they
    # will copy-paste. It has to work.
    for module in [
        "integrations/zotero/api_client.py",
        "integrations/openalex/api_client.py",
        "integrations/arxiv/client.py",
        "integrations/pubmed/client.py",
    ]:
        text = (REPO / "factlog" / module).read_text(encoding="utf-8")
        if "pip install" in text:
            assert "factlog-academic[" in text, module
