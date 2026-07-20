# Slash command usage

> 🌐 **English** | [한국어](slash-commands.md)

> **Plugin vs skill.** What you install is the **plugin** (factlog-academic); the prompt
> it installs and runs is the `/factlog` **skill**. The `/factlog ...` commands below are
> slash commands that invoke that skill, while human gates like review and approval you
> run yourself in the terminal through the Python CLI (`python3 -m factlog ...`). Both
> entry points call the same deterministic engine — slash command · Python CLI ·
> verification engine are one tool.

In a Claude Code session inside your knowledge base (the plugin is active in every session):

```
/factlog sync      # read sources/, extract candidate facts, update pages & decisions
/factlog query     # translate policy/questions.md into facts/query.dl (Datalog query draft)
/factlog check     # compile accepted facts, run the logic check over accepted + query, show the report
/factlog repair    # attempt gated self-correction of review_required queries
/factlog ask       # answer one question: deterministically routed to the engine (verified) or wiki exploration (unverified)
```

Run `/factlog query` before `/factlog check`: the logic check evaluates the
query draft in `facts/query.dl`, which `/factlog query` produces from your
natural-language questions in `policy/questions.md`.
