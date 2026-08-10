# SPDX-License-Identifier: Apache-2.0
"""facts/logic_report.txt must record a run the engine could not complete (#338).

The tool used to write the report only after ``run_wirelog`` returned, so an
engine that could not start left the file untouched. Whatever report was already
there stayed on disk and read as this run's result — the report is not
timestamped in its own text, and neither ``/factlog check``'s output nor the
freshness gate could tell the two apart.

Every case here runs the REAL tool as a subprocess and asserts on the file it
leaves behind. That matters more than usual: the bug is about a file NOT being
written, and any test that builds the report by calling a helper directly would
be asserting about a code path the failing run never reaches.

Two failure causes are covered, because they fail through different exception
types and only one of them is a FactlogError:

- ``facts/accepted.dl`` absent -> ``FactlogError`` raised by ``run_wirelog``;
- the engine package unimportable -> also a ``FactlogError``, from
  ``require_pyrewire_version``, but reached without touching the KB at all. This
  is the "broken engine environment" the issue describes, and the case where no
  amount of fixing the KB helps.
"""
from __future__ import annotations

import csv
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "run_logic_check.py"
SAMPLE_KB = REPO_ROOT / "examples" / "sample-kb"

MARKER = "status: engine-did-not-run"


def _kb(tmp_path: Path) -> Path:
    """A KB with facts already compiled.

    ``examples/sample-kb`` ships with ``facts/accepted.dl`` compiled, which is
    what the engine reads; ``finalize`` rebuilds candidates.csv from runs/, so
    hand-writing candidates would not put facts in front of the engine.
    """
    kb = tmp_path / "kb"
    shutil.copytree(SAMPLE_KB, kb)
    return kb


def _run(kb: Path, extra_pythonpath: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if extra_pythonpath is not None:
        env["PYTHONPATH"] = os.pathsep.join(
            [str(extra_pythonpath), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, str(TOOL), "--wiki", str(kb)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


def _report(kb: Path) -> Path:
    return kb / "facts" / "logic_report.txt"


def _break_engine_import(tmp_path: Path) -> Path:
    """A sys.path entry whose ``pyrewire`` cannot be imported.

    ``common`` guards its ``import pyrewire`` with ``except ImportError`` and
    turns the miss into a FactlogError at call time, so this reproduces an
    uninstalled/broken engine without uninstalling anything.
    """
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "pyrewire.py").write_text(
        'raise ImportError("simulated broken engine install")\n', encoding="utf-8"
    )
    return shim


class TestReportIsWrittenWhenTheEngineCannotRun:
    def test_missing_accepted_dl_still_writes_a_report(self, tmp_path):
        kb = _kb(tmp_path)
        (kb / "facts" / "accepted.dl").unlink()
        # The report from the copied KB is deleted first, so the file this test
        # finds can only have been written by THIS run. Without that, the
        # shipped report passes every assertion below except the marker.
        _report(kb).unlink()

        result = _run(kb)

        assert result.returncode != 0, result.stdout
        assert _report(kb).is_file(), (
            f"no report written; stderr={result.stderr!r}"
        )
        assert MARKER in _report(kb).read_text(encoding="utf-8")

    def test_unimportable_engine_still_writes_a_report(self, tmp_path):
        kb = _kb(tmp_path)
        _report(kb).unlink()

        result = _run(kb, extra_pythonpath=_break_engine_import(tmp_path))

        assert result.returncode != 0, result.stdout
        assert _report(kb).is_file(), (
            f"no report written; stderr={result.stderr!r}"
        )
        assert MARKER in _report(kb).read_text(encoding="utf-8")

    def test_program_the_engine_refuses_still_writes_a_report(self, tmp_path):
        """A cause that is NOT a FactlogError.

        A policy program pyrewire cannot parse raises its own ParseError from
        inside ``run_wirelog``, with no factlog exception type involved. Catching
        only FactlogError would satisfy every other case here and still leave the
        previous report standing for this one, which is why this case exists
        rather than being folded into the two above.
        """
        kb = _kb(tmp_path)
        _report(kb).unlink()
        with (kb / "policy" / "logic-policy.dl").open("a", encoding="utf-8") as fh:
            fh.write("this is not a datalog program (((\n")

        result = _run(kb)

        assert result.returncode != 0, result.stdout
        assert _report(kb).is_file(), (
            f"no report written; stderr={result.stderr!r}"
        )
        text = _report(kb).read_text(encoding="utf-8")
        assert MARKER in text
        # The traceback still reaches stderr — the report does not replace it.
        assert "ParseError" in result.stderr
        assert "reason type: ParseError" in text


class TestTheFailureReportDoesNotReadAsAResult:
    """The distinction the report has to carry: "the engine could not run" is
    not "the engine ran and found nothing"."""

    def test_previous_report_is_replaced_not_left_standing(self, tmp_path):
        # Deliberately NOT deleted: a successful report is left in place, which
        # is the state the bug produced — yesterday's answer presented as
        # today's.
        kb = _kb(tmp_path)
        before = _report(kb).read_text(encoding="utf-8")
        assert "engine facts: 7" in before  # the shipped report is a real result
        (kb / "facts" / "accepted.dl").unlink()

        result = _run(kb)

        after = _report(kb).read_text(encoding="utf-8")
        assert result.returncode != 0
        assert after != before
        assert "engine facts: 7" not in after

    def test_counts_are_absent_rather_than_zero(self, tmp_path):
        kb = _kb(tmp_path)
        _report(kb).unlink()
        (kb / "facts" / "accepted.dl").unlink()

        _run(kb)
        text = _report(kb).read_text(encoding="utf-8")

        # "engine facts: 0" would be a claim that the engine ran over an empty
        # KB. Nothing may render as a count.
        for field in ("engine facts:", "policy findings:", "errors:", "warnings:"):
            assert field not in text, f"{field!r} states a result the run never obtained"

    def test_report_names_the_cause(self, tmp_path):
        kb = _kb(tmp_path)
        _report(kb).unlink()
        (kb / "facts" / "accepted.dl").unlink()

        _run(kb)
        text = _report(kb).read_text(encoding="utf-8")

        assert "reason: missing facts/accepted.dl" in text

    def test_marker_is_a_whole_line(self, tmp_path):
        """The gate matches it with ``grep -qxF``; a marker buried in prose or
        given a trailing comment would silently stop being recognised."""
        kb = _kb(tmp_path)
        _report(kb).unlink()
        (kb / "facts" / "accepted.dl").unlink()

        _run(kb)

        assert MARKER in _report(kb).read_text(encoding="utf-8").splitlines()


class TestKbContentCannotForgeTheMarker:
    """A SUCCESSFUL run's report must never carry the marker, whatever the KB says.

    The marker is negative — absence of it means "the engine ran" — so it is only
    sound while nothing but the failure path can produce that line. The report
    interpolates KB-derived text, and a quoted CSV field may legally contain a
    newline, so a hand-edited status of ``odd\\nstatus: engine-did-not-run`` used
    to open a line of its own inside a report whose engine had run fine. The run
    exited 0 with real counts, and both readers then called it an engine failure
    with ``reason: (not recorded)`` — #338's deadlock rebuilt out of KB content,
    and pointing at a cause that does not exist.

    `finalize` tells users to hand-edit candidates.csv and #332's recovery has
    them seed it, so an unexpected status column is the ordinary case this
    warning line exists for, not an attack.
    """

    def _kb_with_status(self, tmp_path, status: str) -> Path:
        kb = _kb(tmp_path)
        _report(kb).unlink()
        candidates = kb / "facts" / "candidates.csv"
        rows = list(csv.DictReader(candidates.open(encoding="utf-8")))
        rows[0]["status"] = status
        with candidates.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return kb

    def test_status_column_cannot_open_a_marker_line(self, tmp_path):
        kb = self._kb_with_status(tmp_path, "odd\nstatus: engine-did-not-run")

        result = _run(kb)
        text = _report(kb).read_text(encoding="utf-8")

        assert result.returncode == 0, result.stderr
        # The engine ran: the report carries counts, which a failure report never does.
        assert "engine facts:" in text
        assert MARKER not in text.split("\n"), (
            "KB content forged the failure marker in a successful report"
        )
        # The offending value is still reported, escaped onto one line, so the
        # fix does not silently drop the diagnostic the row deserves.
        assert "unknown status treated as non-engine input:" in text

    def test_query_dl_json_escape_cannot_open_a_marker_line(self, tmp_path):
        """The second carrier, through a different decoder.

        ``arg_value`` is ``json.loads``, so ``"a\\nstatus: engine-did-not-run"``
        written as ONE physical line — the escape being two ordinary characters
        on disk — decodes to a value containing a real newline. Splitting
        facts/query.dl into lines cannot see it, so the csv-side escaping did
        nothing here and this forged the marker in a report whose engine had run.

        facts/query.dl is the worse carrier of the two: it is an engine input
        this gate guards, ``/factlog query`` writes it, and it needs one line
        rather than a hand-crafted multi-line CSV cell.

        Three predicates, because each renders decoded values through a
        different path — the policy branch's entity warning, ``path``'s endpoint
        rendering, and the generic constant warning.
        """
        kb = _kb(tmp_path)
        _report(kb).unlink()
        forged = "a\\nstatus: engine-did-not-run"
        (kb / "facts" / "query.dl").write_text(
            f'requires_review("{forged}", X)?\n'
            f'path("{forged}", "Anthropic")?\n'
            f'relation("{forged}", "r", O)?\n',
            encoding="utf-8",
            newline="\n",
        )

        result = _run(kb)
        text = _report(kb).read_text(encoding="utf-8")

        assert result.returncode == 0, result.stderr
        assert "engine facts:" in text  # the engine ran; this is a real result
        assert MARKER not in text.split("\n"), (
            "a JSON escape in facts/query.dl forged the failure marker"
        )

    def test_display_value_escape_is_what_stops_the_forgery(self, tmp_path):
        """The pin for `one_line` INSIDE display_value, at a site where the value
        ENDS the line.

        test_query_dl_json_escape_cannot_open_a_marker_line above does not hold
        this: its payloads land mid-line (``path 'x' -> Anthropic: ...``), so a
        newline inside them splits a line that still does not equal the marker,
        and stripping the escape from display_value leaves it green. The
        falsifying site is validate_query's path-endpoint warning, which ends
        with the value — there a decoded newline opens a line whose whole content
        is the marker.

        Reaching it needs an endpoint that is a KB VALUE but not an ENTITY, i.e.
        the object of a declared attribute relation; that is the branch that
        warns rather than silently answering (#329).
        """
        kb = _kb(tmp_path)
        _report(kb).unlink()
        forged = "a\\nstatus: engine-did-not-run"
        (kb / "policy" / "attribute-relations.md").write_text(
            "- `spec`\n", encoding="utf-8"
        )
        with (kb / "facts" / "accepted.dl").open("a", encoding="utf-8") as fh:
            fh.write(f'relation("factlog", "spec", "{forged}").\n')
        (kb / "facts" / "query.dl").write_text(
            f'path("{forged}", "Anthropic")?\n', encoding="utf-8", newline="\n"
        )

        result = _run(kb)
        text = _report(kb).read_text(encoding="utf-8")

        assert result.returncode == 0, result.stderr
        assert "engine facts:" in text
        # The warning that ends with the value must have fired — without it the
        # test would pass for the wrong reason, having never reached the site.
        assert "query path argument is not an accepted entity:" in text
        assert MARKER not in text.split("\n"), (
            "a decoded newline ending a warning line forged the failure marker"
        )

    def test_ordinary_unknown_status_is_unescaped(self, tmp_path):
        """The escape must fire only on values that would break the line.

        Escaping every value would change the report's text for the ordinary
        case — the one the golden fixture pins — so this is what keeps the fix
        from being a format change.
        """
        kb = self._kb_with_status(tmp_path, "weird")

        _run(kb)

        assert "unknown status treated as non-engine input: weird" in _report(
            kb
        ).read_text(encoding="utf-8")


class TestControlCharactersCannotReachAReportLine:
    """A report line is judged by tools that are not all Python.

    A NUL made BSD sed abort mid-pipeline, and because the gate read the
    pipeline's status as "no marker", one NUL byte turned a DENY into an ALLOW —
    the forgery in reverse: not fabricating the marker but erasing the reader's
    ability to see it. The gate no longer uses sed, and the writer no longer
    emits the byte; these pin the writer's half.
    """

    def test_reason_line_carries_no_control_characters(self, tmp_path):
        """The `reason:` line is the carrier the enumeration missed.

        It is very nearly the only place KB text enters a FAILURE report: a line
        of facts/accepted.dl the engine refuses goes into the exception message
        verbatim. `" ".join(str(exc).split())` collapses Python whitespace only,
        so a NUL rode it into the report intact.
        """
        kb = _kb(tmp_path)
        _report(kb).unlink()
        # A line the engine refuses, carrying a NUL, reaches the reason verbatim.
        with (kb / "facts" / "accepted.dl").open("a", encoding="utf-8") as fh:
            fh.write('this is not datalog \x00 (((\n')

        result = _run(kb)
        raw = _report(kb).read_bytes()

        assert result.returncode != 0
        assert MARKER.encode() in [ln.rstrip(b"\r") for ln in raw.split(b"\n")]
        assert b"\x00" not in raw, "a NUL reached the report and blinds its readers"

    def test_status_column_control_character_is_escaped(self, tmp_path):
        kb = _kb(tmp_path)
        _report(kb).unlink()
        candidates = kb / "facts" / "candidates.csv"
        rows = list(csv.DictReader(candidates.open(encoding="utf-8")))
        rows[0]["status"] = "odd\x00status"
        with candidates.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        _run(kb)

        assert b"\x00" not in _report(kb).read_bytes()

    def _report_with_status(self, tmp_path, status: str) -> str:
        kb = _kb(tmp_path)
        _report(kb).unlink()
        candidates = kb / "facts" / "candidates.csv"
        rows = list(csv.DictReader(candidates.open(encoding="utf-8")))
        rows[0]["status"] = status
        with candidates.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        _run(kb)
        return _report(kb).read_text(encoding="utf-8")

    def test_ordinary_values_still_unescaped(self, tmp_path):
        """The widened set must not start escaping ordinary text — that is what
        keeps the report byte-identical to the golden fixture."""
        text = self._report_with_status(tmp_path, "갑봇-weird")

        assert "unknown status treated as non-engine input: 갑봇-weird" in text

    def test_tab_is_not_escaped(self, tmp_path):
        """TAB is the one C0 character deliberately left out of the set.

        It cannot break a line, end a reader, or drive a terminal, and it is the
        control character people actually type. Escaping it would change the
        report's text against origin/main for input that threatens nothing —
        measured, which is why the exclusion is here rather than assumed.
        """
        text = self._report_with_status(tmp_path, "od\tstatus")

        assert "unknown status treated as non-engine input: od\tstatus" in text

    def test_escape_sequence_is_escaped(self, tmp_path):
        """ESC is in the set: this report is printed to a terminal, so a value
        carrying a CSI sequence could colour or erase what the operator reads."""
        text = self._report_with_status(tmp_path, "od\x1b[31mstatus")

        assert "\x1b" not in text
        assert "od\\x1b[31mstatus" in text

    def _report_with_query_line(self, tmp_path, line: str) -> Path:
        kb = _kb(tmp_path)
        _report(kb).unlink()
        with (kb / "facts" / "query.dl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _run(kb)
        return _report(kb)

    def test_raw_query_line_cannot_rewrite_the_error_count_on_screen(self, tmp_path):
        """The carrier the two decoders do not cover: the RAW query line.

        Every error and echo appends the offending line of facts/query.dl
        verbatim, and ``query_lines`` splits that file with ``str.splitlines()``,
        which eats line breaks and nothing else — NUL, ESC, DEL and C1 ride
        through inside one physical line. No marker can be forged that way (a
        forged line needs a break), so the victim is the human instead: this line
        moves the cursor up one row and erases it, and the row above the error is
        the ``Errors:`` header. Measured before the fix: the report's own header
        said ``errors: 1`` while the terminal SKILL.md Step 3 sends the operator
        to showed ``errors: 0 (all clear)``.
        """
        report = self._report_with_query_line(
            tmp_path, "zz\x1b[1A\x1b[2Kerrors: 0 (all clear)(X)?"
        )
        raw = report.read_bytes()
        text = report.read_text(encoding="utf-8")

        assert b"\x1b" not in raw, "an ESC reached the report and rewrites what it shows"
        assert "errors: 1" in text.splitlines()
        assert (
            "- query unknown predicate: 'zz\\x1b[1A\\x1b[2Kerrors: 0 (all clear)(X)?'"
            in text.splitlines()
        )

    def test_raw_query_line_carries_no_nul(self, tmp_path):
        """Same carrier, the byte that blinded the gate's reader rather than the
        operator's terminal."""
        report = self._report_with_query_line(tmp_path, "zz\x00(X)?")

        assert b"\x00" not in report.read_bytes()

    def test_an_ordinary_query_line_is_echoed_unchanged(self, tmp_path):
        """Wrapping the raw line must not quote the lines a real KB holds — that
        is what keeps the report byte-identical to origin/main.

        A guard, not evidence: it passes before the fix too. What it stops is the
        fix widening ``one_line`` or reaching for a blanket quote.
        """
        report = self._report_with_query_line(tmp_path, "nosuchpredicate(X, Y)?")

        assert (
            "- query unknown predicate: nosuchpredicate(X, Y)?"
            in report.read_text(encoding="utf-8").splitlines()
        )


class TestReportFileMode:
    """The atomic write must not narrow facts/logic_report.txt.

    ``tempfile.mkstemp`` creates at 0600 and ``os.replace`` carries the source's
    mode onto the destination, so switching from ``write_text`` to temp+replace
    silently changed the report from 0644 to 0600 on the SUCCESS path, every run.
    That collides with the rest of #338: an unreadable report used to fall
    through to the mtime branch and be allowed, and hooks/gate_check.sh now hard
    denies it. Where the check and the gate run as different UIDs — a
    devcontainer running the check as root, a CI stage that switches user, a
    group-shared KB — the mode alone becomes a blanket refusal of every
    Write/Edit to the engine inputs.

    Measured against origin/main across umasks 022/077/002 and pre-existing modes
    0644/0664/0600; these pin the same nine cells' rule in three.
    """

    def _mode(self, path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    def test_a_new_report_gets_the_mode_an_ordinary_write_would(self, tmp_path):
        """0666 masked by the umask — what ``open(..., "w")`` produces, which is
        what origin/main's ``write_text`` did."""
        kb = _kb(tmp_path)
        _report(kb).unlink()
        umask = os.umask(0o022)
        os.umask(umask)

        result = _run(kb)

        assert result.returncode == 0, result.stderr
        assert _report(kb).is_file()
        assert self._mode(_report(kb)) == 0o666 & ~umask

    def test_an_existing_reports_mode_is_kept(self, tmp_path):
        """A 0664 report on a group-shared KB was chmod'd deliberately, and
        ``write_text`` never disturbed it."""
        kb = _kb(tmp_path)
        os.chmod(_report(kb), 0o664)

        result = _run(kb)

        assert result.returncode == 0, result.stderr
        assert self._mode(_report(kb)) == 0o664

    def test_a_narrow_existing_mode_is_not_widened(self, tmp_path):
        """The rule is "keep what is there", not "force 0644": an operator who
        restricted the report keeps that too.

        A guard, not evidence — 0600 is what the unfixed writer produced anyway.
        It is here so the fix cannot be written as "always widen to 0644", which
        would pass the other three and hand out a report the operator closed.
        """
        kb = _kb(tmp_path)
        os.chmod(_report(kb), 0o600)

        result = _run(kb)

        assert result.returncode == 0, result.stderr
        assert self._mode(_report(kb)) == 0o600

    def test_the_failure_path_keeps_the_mode_too(self, tmp_path):
        """The failure report replaces the same file through the same writer, so
        a broken engine must not lock the report away from the gate either."""
        kb = _kb(tmp_path)
        os.chmod(_report(kb), 0o664)
        (kb / "facts" / "accepted.dl").unlink()

        result = _run(kb)

        assert result.returncode == 1
        assert MARKER in _report(kb).read_text(encoding="utf-8").splitlines()
        assert self._mode(_report(kb)) == 0o664


class TestFailingStillFails:
    """Guards, not evidence: these hold before the fix too. They are what stops
    the fix from turning a failed check into a passing one."""

    def test_exit_code_and_stderr_are_unchanged(self, tmp_path):
        kb = _kb(tmp_path)
        (kb / "facts" / "accepted.dl").unlink()

        result = _run(kb)

        assert result.returncode == 1
        assert "missing facts/accepted.dl" in result.stderr

    def test_report_is_written_with_lf_endings(self, tmp_path):
        """The gate matches whole lines split on "\\n".

        Text mode translates "\\n" to os.linesep, so on Windows this report would
        be CRLF throughout and the gate's match would stop matching — which fails
        OPEN, handing out edit rights on engine inputs exactly when the engine is
        broken. This asserts the bytes rather than the platform: on a machine
        where os.linesep is already "\\n" it cannot fail, so it is a pin against
        the code changing, not a proof about Windows. Neither this lane nor the
        review could run Windows; that text mode would produce CRLF there is read
        off the io.TextIOWrapper contract.
        """
        kb = _kb(tmp_path)
        _report(kb).unlink()
        (kb / "facts" / "accepted.dl").unlink()

        _run(kb)

        raw = _report(kb).read_bytes()
        assert b"\r\n" not in raw
        assert MARKER.encode() + b"\n" in raw

    def test_write_failure_does_not_mask_the_original_error(self, tmp_path):
        """Reporting the failure must not REPLACE it.

        With facts/ read-only the write raises PermissionError from inside the
        handler; unguarded, that traceback became the program's output and the
        operator lost the one clean line naming the actual cause — which is what
        origin/main gave them. The report is best effort; the diagnosis is not.
        """
        kb = _kb(tmp_path)
        _report(kb).unlink()
        (kb / "facts" / "accepted.dl").unlink()
        facts = kb / "facts"
        mode = facts.stat().st_mode
        facts.chmod(0o555)
        try:
            result = _run(kb)
        finally:
            facts.chmod(mode)

        assert result.returncode == 1
        assert "missing facts/accepted.dl" in result.stderr
        assert "PermissionError" not in result.stderr
        assert "could not write facts/logic_report.txt" in result.stderr

    def test_no_report_outside_a_kb_root(self, tmp_path):
        """``ensure_dirs`` fails before the engine is in the picture, and "this
        is not a factlog KB" is not a statement about the engine. Writing a
        report here would also mean creating the facts/ directory the check just
        refused to accept."""
        not_a_kb = tmp_path / "not-a-kb"
        not_a_kb.mkdir()

        result = _run(not_a_kb)

        assert result.returncode == 1
        assert not (not_a_kb / "facts").exists()


class TestSuccessPathUnchanged:
    def test_successful_report_carries_no_failure_marker(self, tmp_path):
        kb = _kb(tmp_path)
        _report(kb).unlink()

        result = _run(kb)

        assert result.returncode == 0, result.stderr
        text = _report(kb).read_text(encoding="utf-8")
        assert MARKER not in text
        assert "engine facts: 7" in text

    def test_successful_report_still_matches_the_golden_file(self, tmp_path):
        golden = REPO_ROOT / "tests" / "golden" / "logic_report.txt"
        if not golden.is_file():  # pragma: no cover - the file is committed
            pytest.skip("golden report not present")
        kb = _kb(tmp_path)
        _report(kb).unlink()

        _run(kb)

        assert _report(kb).read_text(encoding="utf-8") == golden.read_text(encoding="utf-8")
