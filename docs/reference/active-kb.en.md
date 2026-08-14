# Active KB (target the set-up KB from anywhere)

> 🌐 **English** | [한국어](active-kb.md)

`factlog use <kb>` — or the first `factlog init`/`setup` while no active KB is
set yet — records the chosen KB as the **active KB**, so `ingest`/`ask`/`sync`
and the tools target it from any working directory — no `--target`/`--wiki`
needed:

```bash
factlog use ~/wiki        # make ~/wiki the active KB (recorded in config)
factlog where             # show the active KB and how it was resolved
factlog sources           # list registered sources (original, conversion, fact count)
factlog status            # KB state: facts by status, vocabulary, conflicts, logic freshness, engine
cd /anywhere && factlog ingest report.pdf   # → ~/wiki/runs/sources/report.txt
factlog eject report.pdf  # inverse of ingest: remove the conversion + retire its facts
factlog ignore drafts/*.md   # exclude sources from sync (re-extraction)
factlog provenance Acme uses FastAPI   # trace a fact to its source(s)
```

Resolution precedence: `--target`/`--wiki` flag > `$FACTLOG_ROOT` > active-KB
config (`${XDG_CONFIG_HOME:-~/.config}/factlog/config.json`) > current directory.
With no config set, behavior is unchanged (uses the current directory).

## Resolution precedence table

The four candidates are walked from the top and the **first one with a value**
wins. Which one won is printed on `factlog where`'s `resolved from:` line.

| Rank | Source | How to set it | `resolved from:` in `factlog where` |
|------|--------|---------------|-------------------------------------|
| 1 | command-line flag | `--target <path>` (`--wiki <path>` on some tools) | (not shown — see below) |
| 2 | environment variable | `export FACTLOG_ROOT=<path>` | `env ($FACTLOG_ROOT)` |
| 3 | active-KB config | `factlog use <path>` (or the first `init`/`setup`, while no active KB is set) | `config file` |
| 4 | current directory | (the fallback when nothing else is set) | `current directory` |

Rank 1 never appears in `factlog where`'s output because `where` itself does not
take `--target`. A flag applies only to the **single command** it was given to, so
`where` always reports a result resolved from ranks 2–4.

Whichever way a path arrives, it goes through `~` expansion and absolute-path
normalization. If the config file is missing, its JSON is corrupt, or its `root`
field is empty, resolution **falls through to the next rank instead of crashing** —
ultimately to the current directory.

## Creating a KB and choosing the active one are separate acts

`init`/`setup` **create** a KB; `use` **chooses** the active one. So there are
only four ways `init`/`setup` treat the active-KB setting:

| Situation | Active-KB config |
|-----------|------------------|
| No config file yet | records the KB just created (the first-run experience, unchanged) |
| The target is already recorded | unchanged (the file is not even rewritten) |
| Another KB is recorded | **left alone**. The new KB is not recorded, and the output says how to switch |
| The file cannot be read | **not touched**. See "A damaged config file" below |

```text
factlog init: created /tmp/scratch
factlog init: active-KB root unchanged: /Users/me/wiki — /tmp/scratch is not recorded in the config
  to record it in the config: factlog use /tmp/scratch   (or re-run with --activate)
```

The wording says active-KB **config**, not "active KB", for accuracy: this
decision is about the config file, while which KB is actually in force also
depends on `$FACTLOG_ROOT`. Two separate lines cover that.

When `$FACTLOG_ROOT` names something **other than what the config records** — i.e.
it genuinely overrides it — one line says so. The comparison is against the
**config**, not against the KB just created: if the environment and the config
agree, nothing is being overridden and no line appears.

```text
  note: $FACTLOG_ROOT=/tmp/envkb outranks the config in this session (factlog where)
```

And when a flagless command would **not reach** the KB just created, another line
says where it would go instead and where that came from. This one follows the
same resolution `factlog where` reports (`$FACTLOG_ROOT` > config > cwd), not the
config alone.

```text
  a flagless command would target /Users/me/wiki (from $FACTLOG_ROOT), not /tmp/scratch — pass --target /tmp/scratch, or point $FACTLOG_ROOT at /tmp/scratch
```

`setup`'s closing line asks the same question, because "where does my next
`/factlog sync` go" is what the user is actually asking — not what the config
happens to record.

The point is that creating one throwaway KB must not cost you the KB you were
working in. Pass `--activate` to create and switch in one step, or
`--no-activate` to leave the setting untouched even when none is set yet (scripts,
scratch KBs). Passing both is a usage error (exit code 2).

If you relied on `init`/`setup` moving the active KB for you, add `--activate` or
follow with `factlog use <path>`. The changed behaviour is printed where it
happens, so it never changes silently.

The target of a bare `init`/`setup` changed too. It used to be `~/wiki` always;
now `$FACTLOG_ROOT` and the active-KB config come first. A script that ran a
flagless `init`/`setup` in an environment where either is set now targets that,
not `~/wiki`. Pass `--target ~/wiki` for the old destination. Either way, the
chosen target and where it came from are printed on the first line of every run.

A configured active KB whose path does not currently exist (an unmounted volume,
say) is still your choice: `init` will not take the setting over. Moving it is
always something you do on purpose.

### A damaged config file

If the config file **exists but cannot be read** — truncated JSON, not an object,
no permission — `init`/`setup` do not write to it. The bytes they cannot read may
*be* the user's KB root, and a write would replace both that root and any `lang`
with nothing: a strictly worse loss than a root pointing at an unmounted volume,
which at least survives as text.

```text
factlog init: active-KB config at /Users/me/.config/factlog/config.json could not be read — leaving its bytes untouched; /tmp/scratch is not recorded in it
  repair that file, or overwrite it deliberately: factlog use /tmp/scratch
```

`setup --lang` is withheld for the same reason: recording a language rebuilds the
whole config file, so it would erase the root bytes that could not be read — the
command would be touching the file one line after saying it left it alone. The
`--lang` you asked for was not applied, so `setup` **exits 1**. Exiting 0 would
hand a script three agreeing signals — the exit code, a completion line, and an
empty `factlog lang`.

A config file that is a **broken symlink** — typically a link into a dotfiles
volume you have not mounted — is classified as unreadable too, so the same
activation refusal and the same `--lang` deferral happen. What is at stake is a
**pointer** rather than bytes worth preserving, though, so the wording differs.
Writing at the link replaces it with a regular file, and remounting the volume no
longer brings the setting back; the remedy is therefore mounting it or re-pointing
the link, not repairing a file. The KB itself is still created; what is refused
is the write to the config file — both the activation and the `--lang`.

```text
factlog init: active-KB config at /Users/me/.config/factlog/config.json is a symlink whose target is not reachable right now — leaving the link in place; /tmp/scratch is not recorded in it
  mount it or re-point the link, or overwrite it deliberately: factlog use /tmp/scratch
```

When the final environment check passes, the line that `setup --lang` closes with
over such a config uses those same words, too.

```text
factlog setup: the KB at /tmp/scratch is ready, but --lang was not applied because /Users/me/.config/factlog/config.json is a symlink whose target is not reachable right now (see above). Mount it or re-point the link, then set the language with `factlog lang`; or record this KB and the language together: factlog use /tmp/scratch --lang ko.
```

If that check fails, this line is replaced by the one reporting the environment
problem, and the only remaining record that `--lang` was declined is the
`→ narration language NOT set: …` note in the summary. The exit code is 1 either
way.

Once you have repaired it — or decided it is expendable — `--activate` overwrites
it and reports what it replaced (the config is sound again at that point, so a
`--lang` in the same run applies normally). A file that **does** parse but records no usable
root (`{"lang": "ko"}`, `{"root": ""}`) is not damaged: there is no path in it to
lose, so it is recorded exactly like a first run, and `lang` is preserved.

With `--target` omitted, `init`/`setup` pick their target in the same order every
other command uses: `$FACTLOG_ROOT` > active-KB config > `~/wiki`. The current
directory is deliberately not in that chain — an `init` run from anywhere
scattering a KB layout into that spot would be the worse default. A target you
did not spell out is printed with where it came from.

Leaving it out of the chain is not enough on its own. `factlog where --porcelain`
prints the current directory when nothing is configured, and the skill exports
that value as `$FACTLOG_ROOT` — so cwd can come back in through rank 2. When the
target was **not** named on the command line, resolves to the current directory,
that directory already holds something, and it is not already a factlog KB,
`init` stops instead of creating anything. Name it with `--target <path>` if that
really is what you want. An empty directory and an existing KB have nothing to
lose, so both proceed.

```text
factlog init: no --target given; using /Users/me/wiki (from the active-KB config)
```

A target that is an existing **regular file** stops `init`/`setup` as well (exit
code 1). It used to raise a `NotADirectoryError` traceback on the way to creating
`<target>/sources`.

```text
factlog init: refusing to scaffold a KB at /Users/me/notes.md, which is an existing file, not a directory. Pass --target with a directory path.
```

Both commands share the step that picks the target, so `setup` says the same
sentence under its own name.

```text
factlog setup: refusing to scaffold a KB at /Users/me/notes.md, which is an existing file, not a directory. Pass --target with a directory path.
```

The check applies whether the target was named with the flag or came implicitly
from `$FACTLOG_ROOT`, the active-KB config, or `~/wiki`. A target you did not
name on the command line is reported **with the rank it came from** — the
traceback used to fire before the "no --target given" line, leaving no way to
tell whether the environment variable or the config file had chosen that path.

```text
factlog init: refusing to scaffold a KB at /Users/me/notes.md, which is an existing file, not a directory. It was chosen implicitly (from the active-KB config), not named on the command line — point that at a directory, or pass --target with a directory path.
```

## Checking which KB won

*Type in Claude Code:*

```bash
factlog where
```

```text
active KB: /Users/me/wiki
resolved from: config file (precedence: --flag > $FACTLOG_ROOT > config > cwd)
config file: /Users/me/.config/factlog/config.json
```

If you have set a narration language with `factlog lang`, a `narration language:`
line is printed as well (it applies to the assistant's prose only and has no
effect on engine output).

For scripting, `--porcelain` prints **only the active KB's absolute path, on one
line** — no label, no other lines.

*Run in the terminal:*

```bash
export FACTLOG_ROOT="$(factlog where --porcelain)"
```

A KB-targeting command like `ingest`, when run without a flag, tells you on its
first line which KB it picked and where that came from, so you can notice a write
to an unintended KB.

```text
factlog ingest: target KB /Users/me/wiki (from config)
```
