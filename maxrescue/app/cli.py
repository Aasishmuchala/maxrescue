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

from maxrescue.core.governor import Governor, candidates_from
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


def _cmd_plan(args: argparse.Namespace) -> int:
    """Show the merge batches a rescue would use. Reads only; runs no Max."""
    try:
        report = xray(str(args.file))
    except MaxFileError as exc:
        print(f"maxrescue: {exc}", file=sys.stderr)
        return EXIT_ERROR

    candidates = candidates_from(report.nodes.nodes)
    governor = Governor(ram_budget=int(args.ceiling_gb * (1 << 30)))
    batches = governor.plan_all(candidates)

    unnamed = report.nodes.total_nodes - len(candidates)
    print(f"MaxRescue plan — {args.file}")
    print(governor.describe_plan(batches))
    if unnamed:
        print(
            f"  {unnamed:,} node(s) have no resolvable name and cannot be merged "
            "individually — they are NOT covered by this plan."
        )
    print()
    for i, batch in enumerate(batches, 1):
        flag = "  ⚠ OVER BUDGET" if batch.isolated else ""
        print(f"  batch {i:>3}  {len(batch.names):>6,} objects{flag}")
        if batch.note:
            print(f"           {batch.note}")
    return EXIT_OK


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

    p = sub.add_parser(
        "plan",
        help="show the merge batches a rescue would use (reads only, no Max)",
    )
    p.add_argument("file", type=Path, help="path to a .max file")
    p.add_argument(
        "--ceiling-gb",
        type=float,
        default=70.0,
        help="resident-memory ceiling to plan against (default: 70)",
    )
    p.set_defaults(func=_cmd_plan)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
