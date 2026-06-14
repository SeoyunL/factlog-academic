# Example Source: Claude Code Overview

## What is Claude Code

Claude Code is a command-line tool developed by Anthropic that embeds an AI
assistant directly in the developer's terminal. It reads the local codebase,
runs shell commands, edits files, and reports results without requiring the
developer to copy-paste context into a chat window.

## Key capabilities

Claude Code uses the Claude model family — including Sonnet and Opus — to
understand code, answer questions, and carry out multi-step tasks. It supports
tool use (reading files, running tests, making edits) and can invoke external
plugins such as factlog.

## Plugin system

The Claude Code plugin system lets teams distribute reusable skills as
installable packages. A plugin ships with a `plugin.json` manifest, one or more
skill prompt files under `skills/`, optional deterministic helper scripts under
`tools/`, and optional hooks. factlog is distributed as a Claude Code plugin.

## factlog skill

The factlog skill extends Claude Code with a structured workflow for maintaining
a source-backed knowledge base. Fact extraction, query translation, and
self-correction are performed by the Claude model in-session; compilation,
logic checking, policy compilation, and validation are performed by deterministic
Python scripts bundled with the plugin.
