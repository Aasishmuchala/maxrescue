"""MaxRescue — run INSIDE 3ds Max via `3dsmaxbatch.exe`.

Rebuilds a scene in batches under a memory governor, reducing each batch while
only that batch is resident, and writes a new file. The source is never touched.

Reports to a result file, not stdout: `3dsmaxbatch` exit codes are unreliable,
and a `logging.StreamHandler` on stdout is by itself enough to make Max return
exit -130.

Environment:
    MAXRESCUE_REPO      repo root (added to sys.path)
    MAXRESCUE_RESULT    verdict file the harness reads
    MAXRESCUE_TARGET    the .max file to rescue
    MAXRESCUE_OUTPUT    where to write the rescued scene
    MAXRESCUE_OUT       directory for the JSON report
    MAXRESCUE_CEILING_GB     resident-memory ceiling (default 70)
    MAXRESCUE_CONVERT_BITMAPS  set to 1 to enable the NON-identical bitmap stage
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback

REPO = os.environ.get("MAXRESCUE_REPO", "")
RESULT = os.environ.get("MAXRESCUE_RESULT", "")
TARGET = os.environ.get("MAXRESCUE_TARGET", "")
OUTPUT = os.environ.get("MAXRESCUE_OUTPUT", "")
OUT_DIR = os.environ.get("MAXRESCUE_OUT") or os.path.dirname(RESULT) or os.getcwd()
CEILING_GB = float(os.environ.get("MAXRESCUE_CEILING_GB", "70") or 70)
CONVERT_BITMAPS = os.environ.get("MAXRESCUE_CONVERT_BITMAPS", "").lower() in (
    "1", "true", "yes"
)

if REPO and REPO not in sys.path:
    sys.path.insert(0, REPO)

import pymxs  # noqa: E402  — available only inside Max, by design

from maxrescue.core.batch import BatchRunner  # noqa: E402
from maxrescue.core.governor import Governor, MergeCandidate  # noqa: E402
from maxrescue.maxbridge.context import build_context  # noqa: E402
from maxrescue.xray.report import xray  # noqa: E402
from maxrescue.app.settings import Settings  # noqa: E402

rt = pymxs.runtime
REPORT_PATH = os.path.join(OUT_DIR, "rescue_report.json")
report: dict = {"log": [], "batches": []}


def log(message: str) -> None:
    print("[maxrescue] %s" % message)
    report["log"].append(message)


def flush() -> None:
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(REPORT_PATH, "w") as handle:
            json.dump(report, handle, indent=2, default=str)
    except Exception as exc:
        print("[maxrescue] could not write report: %s" % exc)


def write_result(text: str) -> None:
    if not RESULT:
        return
    try:
        with open(RESULT, "w") as handle:
            handle.write(text)
    except Exception as exc:
        print("[maxrescue] could not write result: %s" % exc)


def main() -> str:
    if not (TARGET and os.path.isfile(TARGET)):
        return "RESCUE_FAIL no MAXRESCUE_TARGET"
    output = OUTPUT or os.path.splitext(TARGET)[0] + "_rescued.max"

    settings = Settings.load()
    settings.convert_bitmaps = CONVERT_BITMAPS or settings.convert_bitmaps
    settings.ram_budget_gb = CEILING_GB
    settings.save()

    # The X-ray runs first and needs no Max at all — it gives the weights the
    # governor plans from, and names anything that will not merge.
    log("x-raying %s" % TARGET)
    started = time.time()
    survey = xray(TARGET)
    report["xray"] = survey.to_dict()
    log(
        "x-ray: %s · %d objects · %d nodes named (%.0f%%)"
        % (
            survey.verdict,
            len(survey.inventory.objects),
            survey.nodes.resolved_count,
            survey.nodes.resolution_rate * 100,
        )
    )
    for line in survey.observations:
        log("  • %s" % line)
    if survey.malware:
        return "RESCUE_REFUSED known payload signature present — clean the file first"
    flush()

    candidates = [
        MergeCandidate(
            name=node.name,
            weight=node.bytes + node.object_bytes,
            position=node.position,
            parent_position=node.parent_position,
        )
        for node in survey.nodes.nodes
        if node.name
    ]
    if not candidates:
        return "RESCUE_FAIL the x-ray resolved no named nodes to merge"
    log("%d named node(s) eligible to merge" % len(candidates))

    context = build_context()
    governor = Governor(ram_budget=settings.ram_budget_bytes)
    runner = BatchRunner(
        context=context,
        governor=governor,
        source_path=TARGET,
        output_path=output,
        config=settings.plan_config(),
        proxy_dir=settings.proxy_out_dir,
        reduce_each_batch=settings.reduce_each_batch,
    )

    for event in runner.run(candidates):
        log("[%s] %s" % (event.phase, event.message))
        flush()

    outcome = runner.outcome
    report["batches"] = [
        {
            "index": b.index,
            "outcome": b.outcome.value,
            "requested": b.requested,
            "merged": b.merged,
            "shortfall": b.shortfall,
            "merge_cost_mb": b.merge_cost_mb,
            "peak_rss_mb": b.peak_rss_mb,
            "seconds": b.seconds,
            "note": b.note,
        }
        for b in outcome.batches
    ]
    report["outcome"] = {
        "output": outcome.output_path,
        "merged": outcome.merged,
        "requested": outcome.requested,
        "complete": outcome.complete,
        "halted": outcome.halted,
        "peak_rss_mb": outcome.peak_rss_mb,
        "rss_end_mb": outcome.memory_after.rss_mb if outcome.memory_after else None,
        "calibration": governor.calibration.describe(),
    }
    for line in outcome.log:
        log(line)
    flush()

    if outcome.halted:
        return "RESCUE_HALTED merged=%d/%d output=%s" % (
            outcome.merged, outcome.requested, output,
        )
    if not outcome.complete:
        return "RESCUE_PARTIAL merged=%d/%d output=%s" % (
            outcome.merged, outcome.requested, output,
        )
    return "RESCUE_OK merged=%d peak=%.0fMB output=%s in %.0fs" % (
        outcome.merged, outcome.peak_rss_mb, output, time.time() - started,
    )


try:
    summary = main()
except Exception as exc:
    summary = "RESCUE_FAIL %s: %s" % (type(exc).__name__, exc)
    report["traceback"] = traceback.format_exc()
    log(summary)

report["summary"] = summary
flush()
write_result(summary)
log(summary)
log("report written to %s" % REPORT_PATH)
