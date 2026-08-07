# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the shared prepass resolver (#107)."""
from __future__ import annotations

import pytest

import factlog_config


class TestResolveRootFromArgv:
    def test_reads_wiki_flag(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.argv", ["tool", "--wiki", str(tmp_path), "extra", "args"])
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        assert factlog_config.resolve_root_from_argv("--wiki") == str(tmp_path.resolve())

    def test_reads_target_flag(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.argv", ["tool", "evaluate", "q?", "--target", str(tmp_path)])
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        assert factlog_config.resolve_root_from_argv("--target") == str(tmp_path.resolve())

    def test_falls_back_to_env_when_flag_absent(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.argv", ["tool", "somecmd"])
        monkeypatch.setenv("FACTLOG_ROOT", str(tmp_path))
        assert factlog_config.resolve_root_from_argv("--wiki") == str(tmp_path.resolve())

    def test_ignores_unrelated_args(self, monkeypatch, tmp_path):
        # parse_known_args must not choke on flags it doesn't define.
        monkeypatch.setattr("sys.argv", ["tool", "--strict", "--wiki", str(tmp_path), "--all"])
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)
        assert factlog_config.resolve_root_from_argv("--wiki") == str(tmp_path.resolve())


class TestLangConfig:
    """read_lang / write_lang and their interaction with root (#269).

    Every test isolates XDG_CONFIG_HOME to a throwaway dir so the developer's real
    ~/.config/factlog/config.json is never read or written.
    """

    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    def test_read_lang_none_when_no_config(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        assert factlog_config.read_lang() is None

    def test_write_then_read_lang(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        factlog_config.write_lang("ko")
        assert factlog_config.read_lang() == "ko"

    def test_write_lang_trims_whitespace(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        factlog_config.write_lang("  en  ")
        assert factlog_config.read_lang() == "en"

    def test_write_lang_none_clears(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        factlog_config.write_lang("ko")
        factlog_config.write_lang(None)
        assert factlog_config.read_lang() is None

    def test_write_lang_empty_clears(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        factlog_config.write_lang("ko")
        factlog_config.write_lang("   ")
        assert factlog_config.read_lang() is None

    def test_write_lang_preserves_root(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        kb = tmp_path / "kb"
        kb.mkdir()
        factlog_config.write_root(kb)
        factlog_config.write_lang("ko")
        assert factlog_config.read_root() == str(kb.resolve())
        assert factlog_config.read_lang() == "ko"

    def test_write_root_preserves_lang(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        factlog_config.write_lang("ko")
        kb = tmp_path / "kb"
        kb.mkdir()
        factlog_config.write_root(kb)
        # Re-pointing the active KB must not drop the configured language.
        assert factlog_config.read_lang() == "ko"
        assert factlog_config.read_root() == str(kb.resolve())

    def test_root_only_config_backward_compat(self, monkeypatch, tmp_path):
        # A pre-#269 config ({"root": ...}, no lang) reads back as lang=None with
        # root intact — no regression for KBs set up before this feature.
        self._isolate(monkeypatch, tmp_path)
        cfg = factlog_config.config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        kb = tmp_path / "kb"
        kb.mkdir()
        cfg.write_text('{"root": "%s"}\n' % kb.resolve(), encoding="utf-8")
        assert factlog_config.read_lang() is None
        assert factlog_config.read_root() == str(kb.resolve())

    def test_broken_config_reads_none(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        cfg = factlog_config.config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("{ this is not json", encoding="utf-8")
        assert factlog_config.read_lang() is None
        assert factlog_config.read_root() is None

    def test_non_string_lang_reads_none(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        cfg = factlog_config.config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('{"root": "/x", "lang": 42}\n', encoding="utf-8")
        assert factlog_config.read_lang() is None


class TestNormalizeLang:
    """The shared `--lang` contract used by lang / use --lang / setup --lang (#269).

    A single validator means every entry point accepts/rejects identically, so
    these pin the boundary the three commands share.
    """

    def test_trims_and_accepts(self):
        from factlog.cli import _normalize_lang

        assert _normalize_lang("  ko ") == ("ko", None)

    def test_empty_means_clear_not_error(self):
        from factlog.cli import _normalize_lang

        # Empty / whitespace normalises to "" (clear), never an error.
        assert _normalize_lang("") == ("", None)
        assert _normalize_lang("   ") == ("", None)

    def test_at_limit_accepts(self):
        from factlog.cli import _normalize_lang

        code = "x" * 32
        assert _normalize_lang(code) == (code, None)

    def test_over_limit_rejects(self):
        from factlog.cli import _normalize_lang

        normalized, error = _normalize_lang("x" * 33)
        assert normalized is None
        assert error is not None and "too long" in error


class TestConfigStatus:
    """Readers may fold every failure into None; a writer may not (#356).

    ``read_root`` returns None for a missing file, bad JSON, a non-object, and an
    unreadable file alike — right for resolution, which must degrade to cwd, and
    wrong for ``init``/``setup``, which used to read that None as "nothing is
    recorded, safe to write" and so destroyed the bytes of a config it could not
    parse. ``config_status`` is the distinction those callers need.
    """

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        # The repo-root conftest pins FACTLOG_ROOT for every test process, and it
        # outranks the config — leaving it set would make the fallback assertion
        # below measure the environment instead of the malformed file.
        monkeypatch.delenv("FACTLOG_ROOT", raising=False)

    def _write(self, text, *, encoding="utf-8"):
        path = factlog_config.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        return path

    def test_missing_file(self):
        assert factlog_config.config_status() == factlog_config.MISSING

    def test_object_is_readable(self):
        self._write('{"root": "/kb"}')
        assert factlog_config.config_status() == factlog_config.READABLE

    def test_object_without_a_root_is_still_readable(self):
        """Understood, and holding no path to lose — so not "damaged"."""
        self._write('{"lang": "ko"}')
        assert factlog_config.config_status() == factlog_config.READABLE
        self._write('{"root": ""}')
        assert factlog_config.config_status() == factlog_config.READABLE

    @pytest.mark.parametrize("content", ['{"root": "/kb", ', "", "[1, 2, 3]", "root = /kb"])
    def test_unparseable_is_unreadable(self, content):
        self._write(content)
        assert factlog_config.config_status() == factlog_config.UNREADABLE

    def test_unopenable_is_unreadable(self):
        path = self._write('{"root": "/kb"}')
        path.chmod(0o000)
        try:
            assert factlog_config.config_status() == factlog_config.UNREADABLE
        finally:
            path.chmod(0o644)

    def test_invalid_utf8_does_not_crash_the_readers(self):
        """``read_text`` raises UnicodeDecodeError — a ValueError, not an OSError.

        It escaped the old ``(json.JSONDecodeError, OSError)`` handler, so a
        config with a stray byte crashed every command that resolves a root
        rather than degrading like any other malformed file.
        """
        path = factlog_config.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"root": "\xff\xfe"}')

        assert factlog_config.config_status() == factlog_config.UNREADABLE
        assert factlog_config.read_root() is None
        assert factlog_config.read_lang() is None
        assert factlog_config.resolve_root()[1] == "cwd"
