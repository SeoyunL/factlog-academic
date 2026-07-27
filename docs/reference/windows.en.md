# Windows

> 🌐 **English** | [한국어](windows.md)

## Windows Python executable

On Windows, factlog's `.sh`/Bash tools (e.g. `factlog_python.sh`) run under Git
Bash. Installing **Git for Windows** provides Git Bash, and factlog's bundled
`.sh` scripts run on top of it.

On Windows, the `python3` command can point to the Microsoft Store stub instead
of a real Python executable. In that state, `python` or `py` may work while the
plugin's bundled scripts fail.

Check these first:

```powershell
python3 --version
python --version
py -0p
```

If `python3 --version` only prints `Python`, fails, or opens Microsoft Store,
tell factlog which Python to use. For a venv:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e <path-to-factlog-repo>
$env:FACTLOG_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
```

The plugin hooks and skill commands use
`${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh` to resolve a Python 3.11+
executable. When `$FACTLOG_PYTHON` is set it is the only candidate: if that
executable is not a Python 3.11+, the script **fails immediately rather than
falling back** to `python3`/`python`/`py` (exit code 127).

When `$FACTLOG_PYTHON` is unset, the selection order is:

1. the interpreter in `$VIRTUAL_ENV` — an explicit "I activated a venv" signal.
   Checked for **version only**; pyrewire is not required
2. the first PATH candidate (`python3`, `python`, `py -3.12`/`-3.11`/`-3`, `py`)
   that **carries pyrewire 1.0.3 or newer**
3. the interpreter in `~/.factlog-venv` — the fixed path the PEP 668 guidance
   below tells you to create. Also checked for **version only**, so someone who
   created the venv but has not installed into it yet can still run `setup` and
   have pyrewire land inside that venv
4. the first PATH candidate that meets the version requirement alone — `doctor`
   and `setup` must still run in a bootstrap state where no engine exists yet, so
   this is never a hard failure

Picking an interpreter from outside PATH prints one line to stderr naming it. No
venv other than those two fixed paths is ever searched for. The result is **not
cached**: every call re-executes its candidate, so a change to an interpreter
takes effect immediately.

If your Python is externally managed (PEP 668), pip will refuse to install into it; `setup` prints venv guidance instead of forcing the install. Create and activate a venv, then re-run `setup`:

```bash
python3 -m venv ~/.factlog-venv && source ~/.factlog-venv/bin/activate
python3 -m factlog setup --target ~/wiki
```
