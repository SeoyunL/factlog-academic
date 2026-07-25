# SPDX-License-Identifier: Apache-2.0
"""finalize resolves its KB root through the config tier (#529).

`tools/finalize.py` already took a KB-root flag, but its argparse default was
``os.environ.get("FACTLOG_ROOT", ".")`` — so with neither the flag nor the env var it
fell straight to cwd and skipped the active-KB config that every other command honours.
Run from anywhere outside a KB it refused with::

    finalize: /private/tmp is not a factlog KB (no sources/).

even though `factlog where` resolved a perfectly good KB from the config.

Consulting that tier is also what makes an *unaimed* run possible, so it comes with a
guard: a config-tier root the caller is not standing inside is refused before any write
(``implicit_target_refusal``, the same criterion as tools/merge_candidates.py's #532
guard). The config tier still earns its keep — it is what lets a run from ``<kb>/pages/``
finalize ``<kb>`` — and every explicit spelling (flag, env, cwd) is untouched.

The tier a run actually picked is observed here through the refusal message, which names
the resolved root: pointing each tier at a *different* non-KB directory makes the message
a direct readout of which one won, without running the five-step chain. The KB gate is
checked before the implicit-target guard, so that readout survives the guard. The cases
that must get PAST both gates (config → a real KB) stub ``finalize._run`` instead, so the
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


def _flowed(text):
    """*text* with every run of whitespace collapsed to one space.

    argparse wraps help to ``shutil.get_terminal_size()``, which reads ``$COLUMNS``
    first — so where a line breaks is a property of the environment the suite runs in,
    not of the help text. Asserting on the raw output made a multi-word phrase fail
    whenever the wrap landed inside it: measured, ``COLUMNS=60`` split "factlog use"
    across two lines and this file's help test failed while the default width passed.
    Collapsing first asks the question that was meant — does the help SAY this — at any
    width.
    """
    return " ".join(text.split())


def _stub_chain(monkeypatch):
    """Record the first chained step and stop the run there.

    Returns a dict that gets a "first" key of (script, args, env) once finalize reaches
    the chain. Its absence is how a test says the run never got past the gates; the
    non-zero rc from this stub is not, since the gates return 1 as well.
    """
    seen = {}

    def fake_run(script, *args, env):
        seen.setdefault("first", (script, args, env))
        return subprocess.CompletedProcess([script], 1, stdout="", stderr="")

    monkeypatch.setattr(finalize, "_run", fake_run)
    return seen


class TestConfigTierIsConsulted:
    def test_config_kb_is_adopted_from_inside_it_with_no_flag_and_no_env(
        self, tmp_path, monkeypatch
    ):
        # THE BUG (#529): no flag, no env — finalize used to resolve to cwd and refuse.
        # Standing in a SUBDIRECTORY of the KB is the case the config tier still has to
        # its name after the implicit-target guard: cwd would resolve to <kb>/pages,
        # which is not a KB, so only the config tier can name <kb> here.
        kb = _kb(tmp_path, "active-kb")
        (kb / "pages").mkdir()
        factlog_config.write_root(kb)
        monkeypatch.chdir(kb / "pages")

        seen = _stub_chain(monkeypatch)

        assert finalize.main([]) == 1  # stops at the stubbed merge, not at a gate
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


class TestImplicitTargetGuard:
    """A config-tier KB the caller is not inside is refused before any write (#529/#532).

    Consulting the config tier is what lets finalize run against a KB nobody named on
    this command line: from an unrelated directory it chains merge_candidates, which
    rewrites candidates.csv, pages/ and decisions/open-questions.md, and then recompiles
    accepted.dl. The guard is deliberately narrow — it fires on the 'config' tier alone —
    so every explicit spelling below keeps working.

    finalize needs its own copy of merge_candidates' guard because it hands the child
    ``--wiki <root>``: the child then resolves 'flag' and its own guard passes, which
    would make finalize the way around #532.
    """

    def _config_kb(self, tmp_path):
        kb = _kb(tmp_path, "active-kb")
        factlog_config.write_root(kb)
        return kb

    def test_config_kb_from_an_unrelated_cwd_is_refused(self, tmp_path, monkeypatch, capsys):
        kb = self._config_kb(tmp_path)
        seen = _stub_chain(monkeypatch)

        assert finalize.main([]) == 1
        assert "first" not in seen, "refused runs must not reach merge_candidates"
        err = capsys.readouterr().err
        assert "REFUSING to finalize" in err
        assert str(kb) in err

    def test_the_refusal_names_both_explicit_ways_to_aim_the_run(
        self, tmp_path, monkeypatch, capsys
    ):
        # A refusal that does not say how to proceed just moves the surprise later.
        kb = self._config_kb(tmp_path)
        _stub_chain(monkeypatch)

        assert finalize.main([]) == 1
        err = capsys.readouterr().err
        assert f"--target {kb}" in err
        assert f"FACTLOG_ROOT={kb}" in err

    def test_standing_in_the_config_kb_is_allowed(self, tmp_path, monkeypatch):
        # The documented workflow: cd into the KB, run finalize with no flag.
        kb = self._config_kb(tmp_path)
        monkeypatch.chdir(kb)
        seen = _stub_chain(monkeypatch)

        assert finalize.main([]) == 1
        assert seen["first"][2]["FACTLOG_ROOT"] == str(kb)

    def test_standing_in_a_subdirectory_of_the_config_kb_is_allowed(
        self, tmp_path, monkeypatch
    ):
        kb = self._config_kb(tmp_path)
        (kb / "runs" / "sources").mkdir(parents=True)
        monkeypatch.chdir(kb / "runs" / "sources")
        seen = _stub_chain(monkeypatch)

        assert finalize.main([]) == 1
        assert seen["first"][2]["FACTLOG_ROOT"] == str(kb)

    @pytest.mark.parametrize("flag", ["--target", "--wiki"])
    def test_naming_the_same_kb_on_the_command_line_is_allowed(
        self, tmp_path, monkeypatch, flag
    ):
        # Same root, same unrelated cwd, same config — only the provenance differs, and
        # that is the whole criterion.
        kb = self._config_kb(tmp_path)
        seen = _stub_chain(monkeypatch)

        assert finalize.main([flag, str(kb)]) == 1
        assert seen["first"][2]["FACTLOG_ROOT"] == str(kb)

    def test_naming_the_same_kb_in_the_environment_is_allowed(self, tmp_path, monkeypatch):
        kb = self._config_kb(tmp_path)
        monkeypatch.setenv("FACTLOG_ROOT", str(kb))
        seen = _stub_chain(monkeypatch)

        assert finalize.main([]) == 1
        assert seen["first"][2]["FACTLOG_ROOT"] == str(kb)

    def test_a_cwd_tier_kb_is_allowed_with_nothing_configured(self, tmp_path, monkeypatch):
        # 'cwd' needs no guard: the root IS the cwd, so there is nothing implicit about
        # it and a non-KB there is caught by the sources/ gate exactly as before.
        kb = _kb(tmp_path, "kb-underfoot")
        monkeypatch.chdir(kb)
        seen = _stub_chain(monkeypatch)

        assert finalize.main([]) == 1
        assert seen["first"][2]["FACTLOG_ROOT"] == str(kb)

    def test_a_sibling_directory_sharing_a_name_prefix_is_not_inside(
        self, tmp_path, monkeypatch, capsys
    ):
        # "inside" is a path-component question, not a string-prefix one: /tmp/active-kb2
        # starts with /tmp/active-kb and is not in it.
        kb = self._config_kb(tmp_path)
        sibling = _plain_dir(tmp_path, kb.name + "2")
        monkeypatch.chdir(sibling)
        seen = _stub_chain(monkeypatch)

        assert finalize.main([]) == 1
        assert "first" not in seen
        assert "REFUSING to finalize" in capsys.readouterr().err


class TestTargetIsAnnounced:
    """Every run says which KB it is about to touch and where that came from.

    Same line the CLI prints (`factlog ingest`: "target KB {target} (from {source})"), and
    it is printed BEFORE the gates so a refused run names its target too. Announcing is
    not the guard above and cannot replace it — merge_candidates already printed
    ``wiki=<root>`` before every write and the incident happened anyway — but it is what
    makes an allowed run auditable.
    """

    @pytest.mark.parametrize("tier", ["flag", "env", "config", "cwd"])
    def test_the_line_names_the_root_and_its_tier(self, tmp_path, monkeypatch, capsys, tier):
        kb = _kb(tmp_path, "announced-kb")
        argv = []
        if tier == "flag":
            argv = ["--target", str(kb)]
        elif tier == "env":
            monkeypatch.setenv("FACTLOG_ROOT", str(kb))
        elif tier == "config":
            factlog_config.write_root(kb)
            monkeypatch.chdir(kb)  # else the implicit-target guard refuses
        else:
            monkeypatch.chdir(kb)
        _stub_chain(monkeypatch)

        finalize.main(argv)
        assert f"finalize: target KB {kb} (from {tier})" in capsys.readouterr().out

    def test_a_refused_run_still_announces_its_target(self, tmp_path, monkeypatch, capsys):
        kb = _kb(tmp_path, "active-kb")
        factlog_config.write_root(kb)
        _stub_chain(monkeypatch)

        assert finalize.main([]) == 1
        assert f"finalize: target KB {kb} (from config)" in capsys.readouterr().out

    def test_the_line_precedes_the_first_chained_step(self, tmp_path, monkeypatch, capsys):
        # Order is the point: an announcement after the write is a receipt, not a notice.
        kb = _kb(tmp_path, "announced-kb")
        printed_when_called = {}

        def fake_run(script, *args, env):
            printed_when_called.setdefault("out", capsys.readouterr().out)
            return subprocess.CompletedProcess([script], 1, stdout="", stderr="")

        monkeypatch.setattr(finalize, "_run", fake_run)

        finalize.main(["--target", str(kb)])
        assert f"finalize: target KB {kb} (from flag)" in printed_when_called["out"]


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
        assert finalize.resolve_kb_root(str(kb)) == (kb.resolve(), "flag")

    @pytest.mark.parametrize("flag", ["--target", "--wiki"])
    def test_either_spelling_is_accepted_by_main(self, tmp_path, capsys, flag):
        # Both spellings reach the same dest, so both override every other tier.
        target = _plain_dir(tmp_path, "flagged-not-a-kb")
        assert finalize.main([flag, str(target)]) == 1
        assert _refused_root(capsys) == str(target)

    @pytest.mark.parametrize(
        "argv_order, winner",
        [(("--target", "--wiki"), "second-dir"), (("--wiki", "--target"), "second-dir")],
    )
    def test_given_both_spellings_the_last_one_on_the_command_line_wins(
        self, tmp_path, capsys, argv_order, winner
    ):
        # One dest, so argparse does not treat this as a conflict: each occurrence just
        # overwrites the previous. Pinned in both orders (and stated in --help) because
        # nothing else in the toolchain says which of the two a caller that appends a
        # second spelling is actually finalizing.
        first = _plain_dir(tmp_path, "first-dir")
        second = _plain_dir(tmp_path, "second-dir")
        first_flag, second_flag = argv_order

        assert finalize.main([first_flag, str(first), second_flag, str(second)]) == 1
        assert _refused_root(capsys) == str(tmp_path / winner)

    def test_the_help_says_the_last_spelling_wins(self, capsys):
        with pytest.raises(SystemExit):
            finalize.main(["--help"])
        assert "the last one wins" in _flowed(capsys.readouterr().out)

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
        out = _flowed(capsys.readouterr().out)
        assert "--wiki" in out
        assert "$FACTLOG_ROOT" in out
        assert "factlog use" in out


class TestResolveKbRoot:
    """The resolver itself, independent of the KB gate.

    It answers with a PAIR — the root and the tier it came from — because the tier is
    what ``implicit_target_refusal`` decides on and what the announcement line reports,
    and nothing downstream can recover it: every chained step is handed an explicit
    ``FACTLOG_ROOT`` and would call any root 'env'.
    """

    def test_flag_value_is_expanded_and_absolutised(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "kb-in-home").mkdir()
        assert finalize.resolve_kb_root("~/kb-in-home") == (
            (tmp_path / "kb-in-home").resolve(),
            "flag",
        )

    def test_relative_flag_is_resolved_against_cwd(self, clean_tiers):
        (clean_tiers / "nested").mkdir()
        assert finalize.resolve_kb_root("nested") == ((clean_tiers / "nested").resolve(), "flag")

    def test_empty_flag_value_is_reported_as_the_tier_that_answered(self, tmp_path):
        # An OBSERVATION of factlog/config.py's `resolve_root` contract, not a decision
        # this file makes: `if cli_value:` treats "" as "no flag given", so an empty
        # --target falls through to env → config → cwd. Every tool that resolves a root
        # inherits it, which is why it is not changed here.
        #
        # It matters more for finalize than the falsiness alone suggests. SKILL.md's
        # Step 3 invokes `finalize.py --target "$FACTLOG_ROOT"`; with $FACTLOG_ROOT unset
        # in the shell that expands to a literal `--target ""`, so a call that LOOKS
        # explicit resolves through the config tier instead. The tier it reports is
        # therefore load-bearing, not cosmetic: it is what makes
        # implicit_target_refusal treat this run as unaimed (it says 'config', not
        # 'flag') rather than letting an empty flag value wave the guard through.
        #
        # Whether resolve_root should reject "" outright is open and belongs to that
        # shared function, not to finalize.
        configured = _plain_dir(tmp_path, "config-dir")
        factlog_config.write_root(configured)
        assert finalize.resolve_kb_root("") == (configured.resolve(), "config")

    def test_an_empty_flag_value_does_not_exempt_a_run_from_the_guard(
        self, tmp_path, monkeypatch, capsys
    ):
        # The consequence of the tier above, at the gate: `--target ""` from an unrelated
        # cwd is the SKILL.md shape with $FACTLOG_ROOT unset, and it is refused exactly
        # like a bare run rather than passing as an explicit target.
        kb = _kb(tmp_path, "active-kb")
        factlog_config.write_root(kb)
        seen = _stub_chain(monkeypatch)

        assert finalize.main(["--target", ""]) == 1
        assert "first" not in seen
        assert "REFUSING to finalize" in capsys.readouterr().err

    def test_malformed_config_degrades_to_cwd(self, tmp_path, clean_tiers):
        cfg = factlog_config.config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("{not json", encoding="utf-8")
        assert finalize.resolve_kb_root(None) == (clean_tiers.resolve(), "cwd")
