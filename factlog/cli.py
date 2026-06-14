"""factlog command-line entry point.

Currently a scaffold. The `install` command (copy the skill + bundled engine
into a target knowledge base) is implemented in a later milestone; see
the delivery plan (T2). For now it reports its own status so
`python3 -m factlog` is runnable end-to-end.
"""

from __future__ import annotations

import argparse
import sys

from factlog import __version__


def cmd_install(args: argparse.Namespace) -> int:
    print(
        "factlog install is not implemented yet (scaffold).\n"
        f"  target: {args.target}\n"
        "Next milestone wires this to copy skills/factlog/ into "
        "<target>/.claude/skills/factlog/ and bundle the deterministic engine.",
        file=sys.stderr,
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factlog", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"factlog {__version__}")
    sub = parser.add_subparsers(dest="command")

    install = sub.add_parser("install", help="install the factlog skill into a target knowledge base")
    install.add_argument("--target", default="~/wiki", help="knowledge base root to install into")
    install.set_defaults(func=cmd_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
