# SPDX-License-Identifier: Apache-2.0
"""finalize resolves its KB root through the config tier (#529).

`tools/finalize.py` already took a KB-root flag, but its argparse default was
``os.environ.get("FACTLOG_ROOT", ".")`` — so with neither the flag nor the env var it
fell straight to cwd and skipped the active-KB config that every other command honours.
Run from anywhere outside a KB it refused with::

    finalize: /private/tmp is not a factlog KB (no sources/).

even though `factlog where` resolved a perfectly good KB from the config.

The tier a run actually picked is observed here through the refusal message, which names
the resolved root: pointing each tier at a *different* non-KB directory makes the message
a direct readout of which one won, without running the five-step chain. The one case that
must get PAST that gate (config → a real KB) stubs ``finalize._run`` instead, so the
assertion is on the ``FACTLOG_ROOT`` handed to the chained steps rather than on a
subprocess tree.
"""
from __future__ import annotations

import subprocess

import pytest
from factlog import config as factlog_config

import finalize


@pytest.fixture(autouse=True)
def clean_tiers(tmp_path, monkeypatch):
    """Start every test with all three non-cwd tiers empty and cwd a known scratch dir.

    tests/unit/conftest.py exports a throwaway ``FACTLOG_ROOT`` for the whole session, so
    the env tier is set unless a test removes it; without that removal every "no env"
    case here would silently be an env-tier case instead.
    """
    monkeypatch.delenv("FACTLOG_ROOT", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    cwd = tmp_path / "cwd-not-a-kb"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


def _plain_dir(tmp_path, name):
    """A directory that is NOT a factlog KB (no sources/), used as a tier marker."""
    path = tmp_path / name
    path.mkdir()
    return path


def _kb(tmp_path, name):
    """A directory finalize accepts as a KB root (the gate is `sources/` alone)."""
    path = tmp_path / name
    (path / "sources").mkdir(parents=True)
    return path


def _refused_root(capsys):
    """The root named in finalize's refusal, as written to stderr."""
    err = capsys.readouterr().err
    assert "is not a factlog KB (no sources/)." in err
    return err.split("finalize: ")[1].split(" is not a factlog KB")[0]


class TestConfigTierIsConsulted:
    def test_config_kb_is_adopted_with_no_flag_and_no_env(self, tmp_path, monkeypatch):
        # THE BUG (#529): no flag, no env, cwd is not a KB — finalize used to refuse.
        # It must now finalize the configured active KB, and hand it to every step.
        kb = _kb(tmp_path, "active-kb")
        factlog_config.write_root(kb)

        seen = {}

        def fake_run(script, *args, env):
            seen.setdefault("first", (script, args, env))
            return subprocess.CompletedProcess([script], 1, stdout="", stderr="")

        monkeypatch.setattr(finalize, "_run", fake_run)

        assert finalize.main([]) == 1  # stops at the stubbed merge, not at the KB gate
        script, args, env = seen["first"]
        assert script == "merge_candidates.py"
        assert args == ("--wiki", str(kb))
        assert env["FACTLOG_ROOT"] == str(kb)

    def test_config_non_kb_is_reported_instead_of_cwd(self, tmp_path, capsys):
        # The config tier is consulted even when it loses: a configured root that is not
        # a KB must be the one named, proving the refusal is no longer about cwd.
        configured = _plain_dir(tmp_path, "configured-not-a-kb")
        factlog_config.write_root(configured)

        assert finalize.main([]) == 1
        assert _refused_root(capsys) == str(configured)


class TestPrecedence:
    """--target/--wiki > $FACTLOG_ROOT > config > cwd (factlog/config.py's order)."""

    def test_flag_beats_env_and_config(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("FACTLOG_ROOT", str(_plain_dir(tmp_path, "env-dir")))
        factlog_config.write_root(_plain_dir(tmp_path, "config-dir"))
        flag = _plain_dir(tmp_path, "flag-dir")

        assert finalize.main(["--target", str(flag)]) == 1
        assert _refused_root(capsys) == str(flag)

    def test_env_beats_config(self, tmp_path, monkeypatch, capsys):
        # Backward compatibility: $FACTLOG_ROOT was the old default and still outranks
        # the newly consulted config tier.
        env_dir = _plain_dir(tmp_path, "env-dir")
        monkeypatch.setenv("FACTLOG_ROOT", str(env_dir))
        factlog_config.write_root(_plain_dir(tmp_path, "config-dir"))

        assert finalize.main([]) == 1
        assert _refused_root(capsys) == str(env_dir)

    def test_config_beats_cwd(self, tmp_path, capsys):
        configured = _plain_dir(tmp_path, "config-dir")
        factlog_config.write_root(configured)

        assert finalize.main([]) == 1
        assert _refused_root(capsys) == str(configured)

    def test_cwd_is_the_last_resort(self, clean_tiers, capsys):
        # No flag, no env, no config: cwd, exactly as before — and the KB gate still
        # refuses it, so running outside a KB with nothing configured is unchanged.
        assert finalize.main([]) == 1
        assert _refused_root(capsys) == str(clean_tiers.resolve())


class TestBothFlagSpellings:
    def test_wiki_and_target_resolve_identically(self, tmp_path):
        kb = _kb(tmp_path, "flagged-kb")
        assert finalize.resolve_kb_root(str(kb)) == kb.resolve()

    @pytest.mark.parametrize("flag", ["--target", "--wiki"])
    def test_either_spelling_is_accepted_by_main(self, tmp_path, capsys, flag):
        # Both spellings reach the same dest, so both override every other tier.
        target = _plain_dir(tmp_path, "flagged-not-a-kb")
        assert finalize.main([flag, str(target)]) == 1
        assert _refused_root(capsys) == str(target)

    def test_unknown_root_flag_is_still_a_usage_error(self, tmp_path, capsys):
        # The alias widens the KB-root spellings, not the parser: an unrelated flag
        # must still exit 2 rather than being silently ignored into a cwd run.
        with pytest.raises(SystemExit) as exc:
            finalize.main(["--kb", str(tmp_path)])
        assert exc.value.code == 2

    def test_help_describes_the_real_resolution_order(self, capsys):
        # The help text is part of the fix: it used to promise "$FACTLOG_ROOT or '.'",
        # which stopped being true the moment the config tier was consulted.
        with pytest.raises(SystemExit):
            finalize.main(["--help"])
        out = capsys.readouterr().out
        assert "--wiki" in out
        assert "$FACTLOG_ROOT" in out
        assert "factlog use" in out


class TestResolveKbRoot:
    """The resolver itself, independent of the KB gate."""

    def test_flag_value_is_expanded_and_absolutised(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "kb-in-home").mkdir()
        assert finalize.resolve_kb_root("~/kb-in-home") == (tmp_path / "kb-in-home").resolve()

    def test_relative_flag_is_resolved_against_cwd(self, clean_tiers):
        (clean_tiers / "nested").mkdir()
        assert finalize.resolve_kb_root("nested") == (clean_tiers / "nested").resolve()

    def test_empty_flag_value_falls_through_to_the_next_tier(self, tmp_path):
        # argparse can hand back "" (`--target ""`); it must not be mistaken for a
        # chosen root and shadow the config the user actually set.
        configured = _plain_dir(tmp_path, "config-dir")
        factlog_config.write_root(configured)
        assert finalize.resolve_kb_root("") == configured.resolve()

    def test_malformed_config_degrades_to_cwd(self, tmp_path, clean_tiers):
        cfg = factlog_config.config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("{not json", encoding="utf-8")
        assert finalize.resolve_kb_root(None) == clean_tiers.resolve()
