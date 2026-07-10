# SPDX-License-Identifier: Apache-2.0
"""Unit tests for PubMed integration client settings (#161).

The loader must: return built-in defaults when nothing is configured, read a
well-formed TOML file, fall back gracefully on wrong-typed value fields, but
fail loudly on a non-string email, a malformed file, or an explicit missing
path. Precedence: explicit path > KB policy file > user XDG file > defaults,
with ``NCBI_API_KEY`` env overriding any file's ``api_key``.

Secrets boundary mirrors Zotero, not OpenAlex: the ``api_key`` credential is
dropped when read from a KB-scoped policy file, but honored from the user XDG
file, an explicit path, or the environment.
"""
from __future__ import annotations

import pytest

from factlog.integrations.pubmed.config import (
    API_KEY_ENV,
    DEFAULT_TOOL,
    PubMedConfig,
    PubMedConfigError,
    default_config_paths,
    from_mapping,
    load_config,
    xdg_config_path,
)

FULL_TOML = """\
[client]
email = "researcher@example.edu"
api_key = "secret-key"
tool = "factlog-lab"
"""


@pytest.fixture(autouse=True)
def _no_ambient_api_key(monkeypatch):
    # A real NCBI_API_KEY in the developer's shell must not leak into tests that
    # do not exercise the env override.
    monkeypatch.delenv(API_KEY_ENV, raising=False)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


class TestDefaults:
    def test_no_file_returns_builtin_defaults(self, tmp_path, monkeypatch):
        # Point XDG at an empty dir so no real user config leaks in.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        cfg = load_config(kb_root=tmp_path)
        assert cfg == PubMedConfig()
        assert cfg.email == ""
        assert cfg.api_key == ""
        assert cfg.tool == DEFAULT_TOOL

    def test_dataclass_is_frozen(self):
        cfg = PubMedConfig()
        with pytest.raises(Exception):
            cfg.email = "x@y.z"  # type: ignore[misc]


class TestLoadFromFile:
    def test_reads_all_fields(self, tmp_path):
        f = _write(tmp_path / "pubmed.toml", FULL_TOML)
        cfg = load_config(path=f)
        assert cfg.email == "researcher@example.edu"
        assert cfg.api_key == "secret-key"
        assert cfg.tool == "factlog-lab"

    def test_email_is_stripped(self):
        cfg = from_mapping({"client": {"email": "  a@b.c  "}})
        assert cfg.email == "a@b.c"

    def test_partial_file_keeps_defaults_for_missing(self, tmp_path):
        f = _write(tmp_path / "p.toml", '[client]\nemail = "a@b.c"\n')
        cfg = load_config(path=f)
        assert cfg.email == "a@b.c"
        assert cfg.api_key == ""
        assert cfg.tool == DEFAULT_TOOL

    def test_empty_file_is_defaults(self, tmp_path):
        f = _write(tmp_path / "p.toml", "")
        assert load_config(path=f) == PubMedConfig()


class TestGracefulTypeFallback:
    def test_wrong_typed_value_fields_fall_back(self):
        cfg = from_mapping({"client": {"email": "a@b.c", "api_key": 42, "tool": 7}})
        assert cfg.api_key == ""
        assert cfg.tool == DEFAULT_TOOL

    def test_non_table_client_section_ignored(self):
        cfg = from_mapping({"client": "oops"})
        assert cfg == PubMedConfig()


class TestEmailRequired:
    def test_non_string_email_fails_loud(self):
        with pytest.raises(PubMedConfigError, match="email must be a string"):
            from_mapping({"client": {"email": 42}})

    @pytest.mark.parametrize("bad", [True, ["a@b.c"], {"x": 1}])
    def test_non_string_email_types_all_raise(self, bad):
        with pytest.raises(PubMedConfigError, match="email must be a string"):
            from_mapping({"client": {"email": bad}})


class TestSecretsBoundary:
    _KEYED = '[client]\nemail = "a@b.c"\napi_key = "k"\n'

    def test_kb_policy_api_key_is_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        kb = tmp_path / "kb"
        (kb / "policy").mkdir(parents=True)
        _write(kb / "policy" / "pubmed-config.toml", self._KEYED)
        cfg = load_config(kb_root=kb)
        assert cfg.email == "a@b.c"  # non-secret settings still honored
        assert cfg.api_key == ""  # credential stripped from a KB policy file

    def test_user_file_api_key_is_kept(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        (xdg / "factlog").mkdir(parents=True)
        _write(xdg / "factlog" / "pubmed.toml", self._KEYED)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        cfg = load_config()
        assert cfg.api_key == "k"

    def test_explicit_path_api_key_is_kept(self, tmp_path):
        f = _write(tmp_path / "p.toml", self._KEYED)
        assert load_config(path=f).api_key == "k"

    def test_same_key_dropped_from_kb_but_kept_from_xdg(self, tmp_path, monkeypatch):
        # The load-bearing asymmetry: identical [client] content, but the
        # credential survives only from the user XDG file, never from KB policy.
        xdg = tmp_path / "xdg"
        (xdg / "factlog").mkdir(parents=True)
        _write(xdg / "factlog" / "pubmed.toml", self._KEYED)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        assert load_config().api_key == "k"

        kb = tmp_path / "kb"
        (kb / "policy").mkdir(parents=True)
        _write(kb / "policy" / "pubmed-config.toml", self._KEYED)
        # KB policy wins for non-secrets but its api_key is never loaded; with no
        # env override, the resolved key is empty.
        assert load_config(kb_root=kb).api_key == ""

    def test_from_mapping_allow_secrets_false_drops_key(self):
        cfg = from_mapping({"client": {"email": "a@b.c", "api_key": "k"}}, allow_secrets=False)
        assert cfg.api_key == ""
        assert cfg.email == "a@b.c"


class TestEnvOverride:
    _KEYED = '[client]\nemail = "a@b.c"\napi_key = "file-key"\n'

    def test_env_overrides_file_api_key(self, tmp_path, monkeypatch):
        f = _write(tmp_path / "p.toml", self._KEYED)
        monkeypatch.setenv(API_KEY_ENV, "env-key")
        assert load_config(path=f).api_key == "env-key"

    def test_env_supplies_key_when_kb_policy_dropped_it(self, tmp_path, monkeypatch):
        # CI shape: credential lives only in the env, KB policy never carries it.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        kb = tmp_path / "kb"
        (kb / "policy").mkdir(parents=True)
        _write(kb / "policy" / "pubmed-config.toml", self._KEYED)
        monkeypatch.setenv(API_KEY_ENV, "env-key")
        cfg = load_config(kb_root=kb)
        assert cfg.api_key == "env-key"

    def test_env_overrides_defaults_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.setenv(API_KEY_ENV, "env-key")
        assert load_config(kb_root=tmp_path).api_key == "env-key"

    def test_empty_env_does_not_wipe_file_key(self, tmp_path, monkeypatch):
        f = _write(tmp_path / "p.toml", self._KEYED)
        monkeypatch.setenv(API_KEY_ENV, "")
        assert load_config(path=f).api_key == "file-key"


class TestExplicitPathPrecedence:
    def test_explicit_path_wins_over_kb_and_user(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        (xdg / "factlog").mkdir(parents=True)
        _write(xdg / "factlog" / "pubmed.toml", '[client]\ntool = "xdg"\n')
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        kb = tmp_path / "kb"
        (kb / "policy").mkdir(parents=True)
        _write(kb / "policy" / "pubmed-config.toml", '[client]\ntool = "kb"\n')
        explicit = _write(tmp_path / "explicit.toml", '[client]\ntool = "explicit"\n')
        cfg = load_config(path=explicit, kb_root=kb)
        assert cfg.tool == "explicit"


class TestPrecedence:
    def test_kb_policy_wins_over_user_file(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        (xdg / "factlog").mkdir(parents=True)
        _write(xdg / "factlog" / "pubmed.toml", '[client]\ntool = "xdg"\n')
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        kb = tmp_path / "kb"
        (kb / "policy").mkdir(parents=True)
        _write(kb / "policy" / "pubmed-config.toml", '[client]\ntool = "kb"\n')
        assert load_config(kb_root=kb).tool == "kb"

    def test_user_file_used_when_no_kb_policy(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        (xdg / "factlog").mkdir(parents=True)
        _write(xdg / "factlog" / "pubmed.toml", '[client]\ntool = "xdg"\n')
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        kb = tmp_path / "kb"
        (kb / "policy").mkdir(parents=True)  # no pubmed-config.toml here
        assert load_config(kb_root=kb).tool == "xdg"

    def test_search_order_lists_kb_before_user(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        paths = default_config_paths(kb_root=tmp_path / "kb")
        assert paths[0] == tmp_path / "kb" / "policy" / "pubmed-config.toml"
        assert paths[1] == xdg_config_path()

    def test_search_order_without_kb_is_user_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert default_config_paths() == [xdg_config_path()]


class TestFailLoud:
    def test_malformed_toml_raises(self, tmp_path):
        f = _write(tmp_path / "bad.toml", "this is = = not toml")
        with pytest.raises(PubMedConfigError, match="invalid TOML"):
            load_config(path=f)

    def test_explicit_missing_path_raises(self, tmp_path):
        with pytest.raises(PubMedConfigError, match="pubmed config not found"):
            load_config(path=tmp_path / "nope.toml")


class TestImportLightness:
    def test_importing_config_does_not_pull_httpx(self):
        # The extra dependency must stay lazy: importing the config module must
        # not drag httpx into the interpreter.
        import subprocess
        import sys

        code = (
            "import factlog, factlog.integrations.pubmed.config as c; "
            "import sys; c.load_config; "
            "assert 'httpx' not in sys.modules, 'httpx imported eagerly'; "
            "print('ok')"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "ok"
