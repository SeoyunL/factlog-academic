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
falling back** to `python3`/`python`/`py` (exit code 127). The `python3`,
`python`, then `py` search applies only when `$FACTLOG_PYTHON` is unset.

If your Python is externally managed (PEP 668), pip will refuse to install into it; `setup` prints venv guidance instead of forcing the install. Create and activate a venv, then re-run `setup`:

```bash
python3 -m venv ~/.factlog-venv && source ~/.factlog-venv/bin/activate
python3 -m factlog setup --target ~/wiki
```
