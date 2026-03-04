from __future__ import annotations

import argparse
import json
import logging
import sys


def _cmd_cc(args: argparse.Namespace) -> None:
    from quoteit.integrations.claude_code import fetch_usage
    result = fetch_usage()
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        result.print_summary(title="Claude Code")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="quoteit",
        description="Check AI tool usage quotas.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show progress messages",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="INTEGRATION")
    subparsers.required = True

    cc = subparsers.add_parser("cc", help="Claude Code usage")
    cc.add_argument("--json", action="store_true", help="Output as JSON")
    cc.set_defaults(func=_cmd_cc)

    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",
    )

    try:
        args.func(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
