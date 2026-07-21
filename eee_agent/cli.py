"""Small compatibility CLI for non-interactive Runtime diagnostics.

The former ``selftest``, ``prompt`` and ``stdio`` commands launched the
unauthenticated rpyc/raw-write agent path.  They are deliberately gone.  The
production server is ``python -m eee_agent.runtime serve``; this module keeps
only the harmless dependency/version report used by support scripts.
"""
from __future__ import annotations

import argparse
import json
import sys


def print_versions() -> int:
    from eee_agent.core.versioning import runtime_version_report

    print(json.dumps(runtime_version_report(), indent=2, sort_keys=True), flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eee_agent")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("versions", help="print Runtime and dependency versions")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "versions":
        return print_versions()
    return 1


if __name__ == "__main__":
    sys.exit(main())
