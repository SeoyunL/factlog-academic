# Install

> 🌐 **English** | [한국어](install.md)

## Requirements

- Python **3.11+** (required by the engine dependency `pyrewire`)
- **pyrewire 1.0.1+** (`pip install -r requirements.txt`)
- Claude Code CLI

## Install

factlog is a **Claude Code plugin**. Install it from this repo's marketplace in a Claude Code session:

```
/plugin marketplace add https://github.com/semantic-reasoning/factlog
/plugin install factlog@semantic-reasoning
/reload-plugins
/factlog setup                     # one-shot: deps + doctor + init, in-session
```

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
/plugin marketplace add ~/git/semantic-reasoning/factlog
/plugin install factlog@semantic-reasoning
/reload-plugins
/factlog setup
```

### What `/factlog setup` does

`setup` collapses the previously-separate post-install steps into a single command. Equivalently, by hand:

```bash
pip install -r ~/git/semantic-reasoning/factlog/requirements.txt   # pyrewire>=1.0.1,<2.0
python3 -m factlog doctor          # checks Python 3.11+ and pyrewire
python3 -m factlog init --target ~/wiki   # scaffold the KB layout
```
