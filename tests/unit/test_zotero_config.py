# SPDX-License-Identifier: Apache-2.0
"""Unit tests for Zotero integration settings (phase 1, scaffold).

The loader must: return built-in defaults when nothing is configured, read a
well-formed TOML file, fall back gracefully on wrong-typed fields, but fail
loudly on a malformed file, an explicit missing path, or an unsupported mode.
Precedence: explicit path > KB policy file > user XDG file > defaults.
"""
from __future__ import annotations

import pytest

from factlog.integrations.zotero.config import (
    DEFAULT_LOCAL_PORT,
    ZoteroConfig,
    ZoteroConfigError,
    default_config_paths,
    from_mapping,
    load_config,
    xdg_config_path,
)

FULL_TOML = """\
[connection]
mode = "local"
local_port = 24000
web_user_id = "12345"
web_api_key = "secret"

[import]
default_target = "~/wiki"
skip_duplicates = false
include_abstract = false
"""


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


class TestDefaults:
    def test_no_file_returns_builtin_defaults(self, tmp_path, monkeypatch):
        # Point XDG at an empty dir so no real user config leaks in.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        cfg = load_config(kb_root=tmp_path)
        assert cfg == ZoteroConfig()
        assert cfg.mode == "local"
        assert cfg.local_port == DEFAULT_LOCAL_PORT
        assert cfg.skip_duplicates is True
        assert cfg.include_abstract is True

    def test_dataclass_is_frozen(self):
        cfg = ZoteroConfig()
        with pytest.raises(Exception):
            cfg.mode = "web"  # type: ignore[misc]


class TestLoadFromFile:
    def test_reads_all_fields(self, tmp_path):
        f = _write(tmp_path / "zotero.toml", FULL_TOML)
        cfg = load_config(path=f)
        assert cfg.mode == "local"
        assert cfg.local_port == 24000
        assert cfg.web_user_id == "12345"
        assert cfg.web_api_key == "secret"
        assert cfg.default_target == "~/wiki"
        assert cfg.skip_duplicates is False
        assert cfg.include_abstract is False

    def test_partial_file_keeps_defaults_for_missing(self, tmp_path):
        f = _write(tmp_path / "z.toml", '[connection]\nmode = "web"\nweb_user_id = "7"\n')
        cfg = load_config(path=f)
        assert cfg.mode == "web"
        assert cfg.web_user_id == "7"
        assert cfg.local_port == DEFAULT_LOCAL_PORT
        assert cfg.skip_duplicates is True

    def test_empty_file_is_defaults(self, tmp_path):
        f = _write(tmp_path / "z.toml", "")
        assert load_config(path=f) == ZoteroConfig()


class TestGracefulTypeFallback:
    def test_wrong_typed_fields_fall_back(self):
        cfg = from_mapping(
            {
                "connection": {"local_port": "not-an-int"},
                "import": {"skip_duplicates": "yes", "default_target": 42},
            }
        )
        assert cfg.local_port == DEFAULT_LOCAL_PORT
        assert cfg.skip_duplicates is True
        assert cfg.default_target == ""

    def test_boolean_is_not_read_as_port(self):
        # bool is an int subclass; `local_port = true` must not become port 1.
        cfg = from_mapping({"connection": {"local_port": True}})
        assert cfg.local_port == DEFAULT_LOCAL_PORT

    def test_non_table_sections_ignored(self):
        cfg = from_mapping({"connection": "oops", "import": 5})
        assert cfg == ZoteroConfig()


class TestPortRange:
    @pytest.mark.parametrize("bad", [0, -5, 70000, 65536])
    def test_out_of_range_port_falls_back(self, bad):
        cfg = from_mapping({"connection": {"local_port": bad}})
        assert cfg.local_port == DEFAULT_LOCAL_PORT

    @pytest.mark.parametrize("ok", [1, 23119, 65535])
    def test_in_range_port_kept(self, ok):
        assert from_mapping({"connection": {"local_port": ok}}).local_port == ok


class TestSecretsBoundary:
    _WEB = (
        '[connection]\nmode = "web"\nweb_user_id = "u"\nweb_api_key = "k"\n'
    )

    def test_kb_policy_secrets_are_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        kb = tmp_path / "kb"
        (kb / "policy").mkdir(parents=True)
        _write(kb / "policy" / "zotero-config.toml", self._WEB)
        cfg = load_config(kb_root=kb)
        assert cfg.mode == "web"  # non-secret settings still honored
        assert cfg.web_user_id == ""  # secrets stripped from a KB policy file
        assert cfg.web_api_key == ""

    def test_user_file_secrets_are_kept(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        (xdg / "factlog").mkdir(parents=True)
        _write(xdg / "factlog" / "zotero.toml", self._WEB)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        cfg = load_config()
        assert cfg.web_user_id == "u"
        assert cfg.web_api_key == "k"

    def test_explicit_path_secrets_are_kept(self, tmp_path):
        f = _write(tmp_path / "z.toml", self._WEB)
        cfg = load_config(path=f)
        assert cfg.web_api_key == "k"


class TestExplicitPathPrecedence:
    def test_explicit_path_wins_over_kb_and_user(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        (xdg / "factlog").mkdir(parents=True)
        _write(xdg / "factlog" / "zotero.toml", "[connection]\nlocal_port = 100\n")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        kb = tmp_path / "kb"
        (kb / "policy").mkdir(parents=True)
        _write(kb / "policy" / "zotero-config.toml", "[connection]\nlocal_port = 200\n")
        explicit = _write(tmp_path / "explicit.toml", "[connection]\nlocal_port = 300\n")
        cfg = load_config(path=explicit, kb_root=kb)
        assert cfg.local_port == 300


class TestImportLightness:
    def test_importing_factlog_does_not_pull_pyzotero(self):
        # The extra dependency must stay lazy: `import factlog` (and the config
        # module) must not drag pyzotero into the interpreter.
        import subprocess
        import sys

        code = (
            "import factlog, factlog.cli, factlog.integrations.zotero.config as c; "
            "import sys; c.load_config; "
            "assert 'pyzotero' not in sys.modules, 'pyzotero imported eagerly'; "
            "assert 'zotero' not in sys.modules, 'zotero imported eagerly'; "
            "print('ok')"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "ok"


class TestFailLoud:
    def test_unsupported_mode_raises(self):
        with pytest.raises(ZoteroConfigError, match="unsupported connection mode"):
            from_mapping({"connection": {"mode": "carrier-pigeon"}})

    @pytest.mark.parametrize("bad_mode", [42, True, ["local"]])
    def test_non_string_mode_raises(self, bad_mode):
        with pytest.raises(ZoteroConfigError, match="must be a string"):
            from_mapping({"connection": {"mode": bad_mode}})

    def test_malformed_toml_raises(self, tmp_path):
        f = _write(tmp_path / "bad.toml", "this is = = not toml")
        with pytest.raises(ZoteroConfigError, match="invalid TOML"):
            load_config(path=f)

    def test_explicit_missing_path_raises(self, tmp_path):
        with pytest.raises(ZoteroConfigError, match="not found"):
            load_config(path=tmp_path / "nope.toml")


class TestPrecedence:
    def test_kb_policy_wins_over_user_file(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        (xdg / "factlog").mkdir(parents=True)
        _write(xdg / "factlog" / "zotero.toml", '[connection]\nlocal_port = 100\n')
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

        kb = tmp_path / "kb"
        (kb / "policy").mkdir(parents=True)
        _write(kb / "policy" / "zotero-config.toml", '[connection]\nlocal_port = 200\n')

        cfg = load_config(kb_root=kb)
        assert cfg.local_port == 200

    def test_user_file_used_when_no_kb_policy(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        (xdg / "factlog").mkdir(parents=True)
        _write(xdg / "factlog" / "zotero.toml", '[connection]\nlocal_port = 100\n')
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

        kb = tmp_path / "kb"
        (kb / "policy").mkdir(parents=True)  # no zotero-config.toml here
        cfg = load_config(kb_root=kb)
        assert cfg.local_port == 100

    def test_search_order_lists_kb_before_user(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        paths = default_config_paths(kb_root=tmp_path / "kb")
        assert paths[0] == tmp_path / "kb" / "policy" / "zotero-config.toml"
        assert paths[1] == xdg_config_path()

    def test_search_order_without_kb_is_user_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert default_config_paths() == [xdg_config_path()]
