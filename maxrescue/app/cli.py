"""MaxRescue command line.

`xray`, `plan` and `verify` run anywhere and never open 3ds Max. `rescue` needs
Max, so on Windows it drives `3dsmaxbatch` and elsewhere it says so plainly
rather than pretending.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from maxrescue.app.settings import Settings
from maxrescue.core.governor import Governor, candidates_from
from maxrescue.core.verify import Tolerance, compare
from maxrescue.xray.ole import MaxFileError
from maxrescue.xray.report import Verdict, xray

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_MALWARE = 3
EXIT_DIFFERENT = 4


# ---------------------------------------------------------------------------
# xray
# ---------------------------------------------------------------------------


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

    return EXIT_MALWARE if report.verdict == Verdict.MALWARE else EXIT_OK


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def _cmd_plan(args: argparse.Namespace) -> int:
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
    for index, batch in enumerate(batches, 1):
        flag = "  ⚠ OVER BUDGET" if batch.isolated else ""
        print(f"  batch {index:>3}  {len(batch.names):>6,} objects{flag}")
        if batch.note:
            print(f"           {batch.note}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _cmd_verify(args: argparse.Namespace) -> int:
    tolerance = (
        Tolerance.filtering_change() if args.allow_filtering else Tolerance.bit_exact()
    )
    result = compare(str(args.before), str(args.after), tolerance=tolerance)

    print(result.describe())
    if result.command:
        print(f"  command: {' '.join(result.command)}")
    if result.output:
        print(f"  {result.output.splitlines()[0]}")
    if not args.allow_filtering and not result.identical:
        print(
            "\n  Every automatic stage claims bit-identical renders. A difference "
            "here means a stage was mis-specified — do not ship this result."
        )
    return EXIT_OK if result.passed else EXIT_DIFFERENT


# ---------------------------------------------------------------------------
# rescue
# ---------------------------------------------------------------------------


def _cmd_rescue(args: argparse.Namespace) -> int:
    repo = Path(__file__).resolve().parent.parent.parent
    launcher = repo / "scripts" / "run_rescue.ps1"

    if os.name != "nt":
        print(
            "maxrescue: rescue needs 3ds Max, which only runs on Windows.\n"
            f"On the box:\n\n"
            f"  pwsh {launcher.name} -Target \"{args.file}\" "
            f"-CeilingGB {args.ceiling_gb}\n\n"
            "`xray`, `plan` and `verify` run here without Max.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    command = [
        "pwsh",
        str(launcher),
        "-Target",
        str(args.file),
        "-CeilingGB",
        str(args.ceiling_gb),
        "-MaxVersion",
        args.max_version,
    ]
    if args.output:
        command += ["-Output", str(args.output)]
    if args.convert_bitmaps:
        command += ["-ConvertBitmaps"]

    print(" ".join(command))
    return subprocess.call(command)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    defaults = Settings.load()
    parser = argparse.ArgumentParser(
        prog="maxrescue",
        description=(
            "Inspect and reduce oversized 3ds Max scenes. `xray`, `plan` and "
            "`verify` never open 3ds Max."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    x = sub.add_parser("xray", help="report what a .max contains, without opening Max")
    x.add_argument("file", type=Path)
    x.add_argument("--json", metavar="PATH", help="machine-readable report ('-' for stdout)")
    x.set_defaults(func=_cmd_xray)

    p = sub.add_parser("plan", help="show the merge batches a rescue would use")
    p.add_argument("file", type=Path)
    p.add_argument("--ceiling-gb", type=float, default=defaults.ram_budget_gb)
    p.set_defaults(func=_cmd_plan)

    r = sub.add_parser("rescue", help="rebuild a scene in batches (needs 3ds Max)")
    r.add_argument("file", type=Path)
    r.add_argument("--output", type=Path, default=None)
    r.add_argument("--ceiling-gb", type=float, default=defaults.ram_budget_gb)
    r.add_argument("--max-version", default="2026", choices=["2024", "2025", "2026", "2027"])
    r.add_argument(
        "--convert-bitmaps",
        action="store_true",
        help="also convert bitmap loaders — NOT render-identical; verify the result",
    )
    r.set_defaults(func=_cmd_rescue)

    v = sub.add_parser("verify", help="compare two rendered frames")
    v.add_argument("before", type=Path)
    v.add_argument("after", type=Path)
    v.add_argument(
        "--allow-filtering",
        action="store_true",
        help="tolerate the filtering change from bitmap conversion (needs review)",
    )
    v.set_defaults(func=_cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
