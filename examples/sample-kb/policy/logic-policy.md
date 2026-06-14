# Logic policy

This file describes the Datalog rules used to reason over the knowledge base.
Rules are expressed as controlled natural-language bullets in the format
`- [reason] sentence with \`relation\`` and compiled to Datalog by
`tools/generate_logic_policy.py`.

## Rules

- [bidirectional_check] Facts with the `develops` relation require review when a matching `developed_by` relation also exists.
- [capability_check] Facts with the `performs` relation require review to confirm the capability is documented in sources.
