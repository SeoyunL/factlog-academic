# Install

> 🌐 **English** | [한국어](install.md)

## Requirements

- Python **3.11+** (required by the engine dependency `pyrewire`)
- **pyrewire 1.0.3+** (`pip install -r requirements.txt`)
- Claude Code CLI
- **git** — the marketplace install uses `git clone` under the hood. On Windows, install **Git for Windows**.

## Install

factlog-academic is a **Claude Code plugin**. Install it from this repo's marketplace in a Claude Code session:

```
/plugin marketplace add https://github.com/SeoyunL/factlog-academic
/plugin install factlog@seoyunl
/reload-plugins
/factlog setup                     # one-shot: deps + doctor + init, in-session
```

> Install from **this** repository, not from upstream `semantic-reasoning/factlog`.
> The upstream plugin has none of the bibliography commands — `factlog zotero-import`,
> `factlog openalex-*`, `factlog arxiv-*`, and `factlog pubmed-*` exist only here.
> The install handle `factlog@seoyunl` comes from `.claude-plugin/marketplace.json`:
> the marketplace name is `seoyunl` and the plugin name is `factlog`.

Run these commands **one line at a time**. If you paste multiple plugin commands
at once, Claude Code may try to process the marketplace registration and install
out of order.

After a successful install, the new `/factlog ...` commands may not be loaded in
the current session yet. Run `/reload-plugins` after `/plugin install`, then run
`/factlog setup`.

`setup` runs `doctor`, installs the engine dependency (`pyrewire`), scaffolds the KB, and re-checks the environment — in one command.

### Local install (development)

To develop against a local clone, register the working tree as the marketplace instead:

```
/plugin marketplace add ~/git/factlog-academic
/plugin install factlog@seoyunl
/reload-plugins
/factlog setup
```

### What `/factlog setup` does

`setup` collapses the previously-separate post-install steps into a single command. Equivalently, by hand:

```bash
pip install -r ~/git/factlog-academic/requirements.txt   # pyrewire>=1.0.3,<2.0
python3 -m factlog doctor          # checks Python 3.11+ and pyrewire
python3 -m factlog init --target ~/wiki   # scaffold the KB layout
```

## Optional dependencies for the bibliography integrations

Each bibliography integration needs its own extra. Install it **from this repository**.

> **`factlog` on PyPI is not this project.** That name belongs to an unrelated 2013
> project ("File ACTivity LOGger", v0.0.1). Ask pip for it and you get **that package** —
> and because it has no such extra, pip only warns and exits 0, so you are left with a
> success message, a package you never asked for, and none of pyzotero / httpx /
> feedparser. The integration then keeps failing. The distribution is named
> **`factlog-academic`** — always write the extras spec as `factlog-academic[...]`.
> Install it one of these ways:

```bash
# if you cloned this repository (the common case)
pip install -e '.[zotero]'

# if you installed it as a Claude Code plugin without cloning. This variable is set only
# inside a Claude Code session — from an ordinary terminal, run `factlog where` in a
# /factlog session to find the plugin path and use that path literally
pip install -e "${CLAUDE_PLUGIN_ROOT}[zotero]"

# from anywhere, without a checkout
pip install 'factlog-academic[zotero] @ git+https://github.com/SeoyunL/factlog-academic'
```

Substitute `openalex`, `arxiv`, or `pubmed` for `zotero`. Several at once also works:
`pip install -e '.[zotero,arxiv]'`.

> **Upgrading from an older version?** The distribution used to be named `factlog`. Both
> names own the same `factlog` module and the same `factlog` command, so pip installs
> them side by side, and removing only the old one takes that shared command with it.
> After `pip uninstall factlog`, **reinstall**. `factlog doctor` detects and reports this
> state.

For how to use each integration, see [Zotero](../zotero-import.en.md) ·
[OpenAlex](../openalex.en.md) · [arXiv](../arxiv.en.md) · [PubMed](../pubmed.en.md).

## Installation failure modes — symptom → cause → fix

`factlog doctor` already diagnoses most installation problems. doctor prints each
check as one of `OK` / `INFO` / `WARN` / `FAIL`, and when something is wrong it
attaches a fix hint on a following line starting with `→`. The table below
organizes that output by symptom.

> **doctor prints its checks in Korean, whatever your locale.** The titles and
> hints quoted below are reproduced **verbatim as the program prints them**, with
> an English gloss in parentheses — grep for the literal string. `factlog lang en`
> sets the *assistant's narration language only* (its prose in-session) and does
> not translate CLI output.

| Symptom | Cause | Fix |
|---------|-------|-----|
| `/factlog …` commands still missing after installing | the new commands are not loaded in the current session yet | run `/reload-plugins`, then `/factlog setup` |
| doctor: `FAIL  Python 3.x < 3.11 필요` (*Python 3.x < 3.11 required*) | Python is below the minimum version | install Python 3.11 or newer, then re-run |
| doctor: `FAIL  pyrewire not installed` or `FAIL  pyrewire X < 1.0.3` | the engine dependency is missing or too old | `pip install -r requirements.txt` (or re-run `/factlog setup`) |
| `setup`: pip refuses, with `factlog setup: this Python is externally managed (PEP 668), so pip refused to install into it.` | a distro-managed system Python (PEP 668) | create and activate a venv, then re-run `setup`. factlog does **not** force the install with `--break-system-packages` |
| doctor: `WARN  Python 3.x (Store stub: …\WindowsApps\…)` | a Microsoft Store Python stub on Windows | install the official python.org distribution. Or name the Python to use via `$FACTLOG_PYTHON` |
| doctor: `FAIL  git이 없습니다` (*git is not present*) | git is not installed — the marketplace install uses `git clone` | macOS: `xcode-select --install`; otherwise your package manager (e.g. `apt install git`); Windows: **Git for Windows** |
| doctor: `WARN  FACTLOG_PYTHON = … (경로 없음)` (*path does not exist*) | `$FACTLOG_PYTHON` points at a path that does not exist | fix the path, or `unset FACTLOG_PYTHON` |
| doctor: `WARN  이 폴더에 factlog/ 폴더가 있어 패키지를 가릴 수 있습니다` (*a `factlog/` folder here may shadow the package*) | a `factlog/` folder in the current directory shadows the installed package | run from another location, or rename that folder |
| `[factlog] FACTLOG_PYTHON is set but is not a usable Python 3.11+` (exit code 127) | the executable `$FACTLOG_PYTHON` points at is not a Python 3.11+ | fix the path, or `unset FACTLOG_PYTHON`. Once `$FACTLOG_PYTHON` is set, factlog **fails immediately rather than falling back** to `python3`/`python`/`py` |

> **A git FAIL does not block `setup`.** doctor run on its own fails (exit code 1)
> when git is missing, but `setup`'s actual work (pip install + KB scaffolding)
> does not use git, so a git FAIL does not flip `setup`'s exit code. The
> marketplace install itself does use `git clone`, so that step still needs git.

`WARN` and `INFO` do not affect the exit code — only `FAIL` is counted. doctor's
summary line ends as `결과: 이상 없음` (*result: no problems*) or
`결과: FAIL N개.` (*result: N FAIL(s).*).

For Windows Python executable problems, the full procedure is in the
[Windows Python executable](../reference/windows.en.md#windows-python-executable)
section of the detailed reference.
