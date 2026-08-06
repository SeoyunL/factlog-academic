# Sync-ignore list
#
# Source files matching these glob patterns are SKIPPED by `/factlog sync`
# (re-extraction), `factlog ingest --scan`, coverage gap reporting, and
# `/factlog ask` wiki exploration — even when modified. Their already-merged
# facts are KEPT (use `factlog eject` to remove those). Manage with
# `factlog ignore [--remove] <pattern>`.
#
# One pattern per line; '#' comments and '-' bullets allowed; quote a pattern
# with spaces (or one starting with '#') in `backticks`. A pattern matches a
# source by its full ref (sources/... or runs/sources/...) OR its path within
# the source root, so `drafts/*.md` matches `sources/drafts/x.md`.
#
# Glob: '*' and '?' stay within one path segment (do NOT cross '/'); '**'
# crosses segments; a trailing '/' means the whole subtree. So:
#   drafts/*.md   -> drafts/x.md      (not drafts/sub/x.md)
#   drafts/**     -> everything under drafts/
#   **/*.md       -> any .md at any depth
#
# Example (remove the leading '# ' to activate):
# - drafts/*.md
# - sources/wip-notes.md
