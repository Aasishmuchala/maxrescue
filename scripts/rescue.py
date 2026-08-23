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
    MAXRESCUE_TEXTURE_FLOOR_PX
                          smallest a texture may be reduced to (default 4096;
                          never honoured below it)
    MAXRESCUE_REFERENCE   a reference EXR from scripts/reference.py. When set,
                          the rescued scene is rendered with the same settings
                          and compared PIXEL FOR PIXEL against it.
    MAXRESCUE_LIGHTCACHE  the light cache the reference used — both renders must
                          load the same one or they differ for reasons that have
                          nothing to do with the rescue
    MAXRESCUE_CAMERA / MAXRESCUE_WIDTH / MAXRESCUE_HEIGHT
                          must match whatever the reference used
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
TEXTURE_FLOOR = int(os.environ.get("MAXRESCUE_TEXTURE_FLOOR_PX", "4096") or 4096)
REFERENCE = os.environ.get("MAXRESCUE_REFERENCE", "")
LIGHTCACHE = os.environ.get("MAXRESCUE_LIGHTCACHE", "")
CAMERA = os.environ.get("MAXRESCUE_CAMERA", "")
WIDTH = int(os.environ.get("MAXRESCUE_WIDTH", "1280") or 1280)
HEIGHT = int(os.environ.get("MAXRESCUE_HEIGHT", "720") or 720)

if REPO and REPO not in sys.path:
    sys.path.insert(0, REPO)

import pymxs  # noqa: E402  — available only inside Max, by design

from maxrescue.core.batch import BatchRunner  # noqa: E402
from maxrescue.core.governor import Governor, MergeCandidate  # noqa: E402
from maxrescue.maxbridge.context import build_context  # noqa: E402
from maxrescue.core.verify import Tolerance, compare  # noqa: E402
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

    # The whole safety model rests on the source being untouched — that is why
    # no per-batch backup is taken. If the output resolves to the source, the
    # first batch save destroys the original and there is no backup at all.
    # Compare resolved paths AND, where the file exists, the identity the OS
    # reports, so a UNC / drive-letter / 8.3 alias cannot slip past.
    if os.path.abspath(os.path.normcase(output)) == os.path.abspath(
        os.path.normcase(TARGET)
    ):
        return "RESCUE_REFUSED output is the source file — that would destroy it"
    try:
        if os.path.exists(output) and os.path.samefile(output, TARGET):
            return "RESCUE_REFUSED output resolves to the source file"
    except OSError:
        pass
    if os.path.exists(output) and not os.environ.get("MAXRESCUE_OVERWRITE"):
        return (
            "RESCUE_REFUSED output already exists: %s "
            "(set MAXRESCUE_OVERWRITE=1 to replace it)" % output
        )

    settings = Settings.load()
    settings.convert_bitmaps = CONVERT_BITMAPS or settings.convert_bitmaps
    settings.ram_budget_gb = CEILING_GB
    settings.texture_floor_px = TEXTURE_FLOOR
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

    # --- the proof, if a reference was supplied
    if REFERENCE and os.path.isfile(REFERENCE):
        verdict = verify_against_reference(context, output)
        report["verification"] = verdict
        flush()
        if not verdict.get("passed"):
            return "RESCUE_DIFFERENT the rescued scene does NOT render identically: %s" % (
                verdict.get("summary"),
            )
        return "RESCUE_VERIFIED renders are identical · merged=%d output=%s" % (
            outcome.merged, output,
        )
    if REFERENCE:
        log("! MAXRESCUE_REFERENCE was set but %s does not exist — NOT verified" % REFERENCE)
    if not outcome.complete:
        return "RESCUE_PARTIAL merged=%d/%d output=%s" % (
            outcome.merged, outcome.requested, output,
        )
    return "RESCUE_OK merged=%d peak=%.0fMB output=%s in %.0fs" % (
        outcome.merged, outcome.peak_rss_mb, output, time.time() - started,
    )


def verify_against_reference(context, output: str) -> dict:
    """Render the rescued scene and compare it with the reference.

    The rescued scene is already open, so this renders what was actually
    produced — not a re-import of it. Same camera, same resolution, same light
    cache, same seed. Anything less and a difference cannot be attributed.
    """
    produced = os.path.join(OUT_DIR, "rescued.exr")
    log("rendering the rescued scene for comparison")

    rt.renderWidth = WIDTH
    rt.renderHeight = HEIGHT

    camera = None
    if CAMERA:
        for candidate in rt.cameras or []:
            if str(getattr(candidate, "name", "")) == CAMERA:
                camera = candidate
                break
        if camera is None:
            return {
                "passed": False,
                "summary": "camera %r is not in the rescued scene" % CAMERA,
            }

    saved = context.render.prepare_deterministic(light_cache_file=LIGHTCACHE or None)
    try:
        if camera is not None:
            rt.viewport.setCamera(camera)
        rendered = context.render.render_to(produced)
    finally:
        context.render.restore(saved)

    if not rendered:
        return {"passed": False, "summary": "the comparison render produced no file"}

    # BIT-EXACT. Every automatic stage claims identity; a tolerance here would
    # quietly accept the very drift this exists to catch.
    result = compare(REFERENCE, produced, tolerance=Tolerance.bit_exact())
    log(result.describe())
    return {
        "passed": result.passed,
        "identical": result.identical,
        "summary": result.summary,
        "command": list(result.command),
        "reference": REFERENCE,
        "produced": produced,
    }


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
