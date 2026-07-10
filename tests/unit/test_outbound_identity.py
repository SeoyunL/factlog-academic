"""Every outbound request must identify this fork, not upstream factlog (#153).

The arxiv, openalex, and taxonomy-refresh call sites are the only three places
that reach a remote host. arXiv and OpenAlex both ask callers to identify
themselves so operators can reach them about traffic; those commands exist only
in this fork, so an upstream identity would route complaints to a project that
never sent the requests.

Zotero is deliberately absent: its client is built as
``zotero.Zotero("0", "user", local=True)`` and ``_connect()`` refuses any
non-local mode, so it carries no remote identity.
"""

import importlib.util
import re
from pathlib import Path

from factlog.integrations.openalex.api_client import _user_agent as openalex_user_agent
from factlog.integrations.openalex.config import OpenAlexConfig

_UPSTREAM = "semantic-reasoning"
_TOOLS_SCRIPT = (
    Path(__file__).resolve().parents[2] / "tools" / "refresh_arxiv_categories.py"
)


def _load_refresh_script():
    spec = importlib.util.spec_from_file_location("_refresh_arxiv", _TOOLS_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openalex_user_agent_names_this_fork():
    assert openalex_user_agent(OpenAlexConfig()) == "factlog-academic"


def test_openalex_user_agent_carries_the_polite_pool_contact():
    agent = openalex_user_agent(OpenAlexConfig(email="a@b.example"))
    assert agent.startswith("factlog-academic")
    assert "mailto:a@b.example" in agent


def test_openalex_user_agent_is_not_the_bare_upstream_token():
    # Reverting the token to "factlog" made the whole suite pass before this test
    # existed: the identity was assembled inside the transport closure, which the
    # injected-transport tests bypass entirely.
    for config in (OpenAlexConfig(), OpenAlexConfig(email="a@b.example")):
        agent = openalex_user_agent(config)
        assert not re.match(r"^factlog(\s|$)", agent)
        assert _UPSTREAM not in agent


def test_taxonomy_refresh_user_agent_points_at_this_repository():
    module = _load_refresh_script()
    assert module.USER_AGENT.startswith("factlog-academic")
    assert _UPSTREAM not in module.USER_AGENT
    assert "https://github.com/SeoyunL/factlog-academic" in module.USER_AGENT
