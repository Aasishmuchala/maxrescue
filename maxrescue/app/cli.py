"""MaxRescue command line.

`xray` is the only subcommand that works today, and it is the one that needs no
3ds Max: it reads the `.max` container directly. `rescue` and `verify` arrive
with phases 3-5 (see tasks/plan.md) and are deliberately absent rather than
stubbed — a subcommand that exists but does nothing is worse than one that does
not exist.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from maxrescue.xray.ole import MaxFileError
from maxrescue.xray.report import Verdict, xray

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_MALWARE = 3


def _cmd_xray(args: argparse.Namespace) -> int:
    try:
        report = xray(str(args.file))
    except MaxFileError as exc:
        print(f"maxrescue: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        text = report.to_json()
        if args.json == "-":
            print(text)
        else:
            Path(args.json).write_text(text, encoding="utf-8")
            print(f"wrote {args.json}", file=sys.stderr)
    else:
        print(report.to_text())

    # A known payload is the one result worth failing a script over.
    return EXIT_MALWARE if report.verdict == Verdict.MALWARE else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maxrescue",
        description=(
            "Inspect and reduce oversized 3ds Max scenes. `xray` runs anywhere; "
            "it never opens 3ds Max."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    x = sub.add_parser(
        "xray",
        help="report what a .max file contains, without opening 3ds Max",
    )
    x.add_argument("file", type=Path, help="path to a .max file")
    x.add_argument(
        "--json",
        metavar="PATH",
        help="write the machine-readable report here ('-' for stdout)",
    )
    x.set_defaults(func=_cmd_xray)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
