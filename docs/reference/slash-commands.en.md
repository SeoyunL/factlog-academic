# Slash command usage

> 🌐 **English** | [한국어](slash-commands.md)

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
