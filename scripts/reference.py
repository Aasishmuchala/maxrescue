"""Render a reference frame — run INSIDE 3ds Max via `3dsmaxbatch.exe`.

This is half of the proof that a rescue changed nothing. Run it on the machine
where the scene still opens (the 256 GB box) to capture what the scene looks
like *before*; after the rescue, `rescue.py` renders the output with the same
settings and compares the two frames pixel for pixel.

Determinism is forced, because two renders of an *unmodified* scene differ
otherwise and the comparison would be meaningless:

* locked noise pattern and a fixed seed, so adaptive sampling lands identically;
* bucket sampler, whose stopping point does not depend on elapsed time;
* the light cache computed once and **saved**, so the rescued render loads that
  same cache rather than recomputing a different one;
* denoiser off, because comparing denoised images hides precisely the small
  differences worth catching;
* raw EXR output, so the pixels on disk are exactly what V-Ray produced.

Environment:
    MAXRESCUE_REPO        repo root (added to sys.path)
    MAXRESCUE_RESULT      verdict file the harness reads
    MAXRESCUE_TARGET      the .max file to render
    MAXRESCUE_REFERENCE   where to write the reference EXR
    MAXRESCUE_LIGHTCACHE  where to write the light cache both renders will use
    MAXRESCUE_CAMERA      camera to render from (default: the active view)
    MAXRESCUE_WIDTH       render width  (default 1280)
    MAXRESCUE_HEIGHT      render height (default 720)
    MAXRESCUE_OUT         directory for the JSON report
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
REFERENCE = os.environ.get("MAXRESCUE_REFERENCE", "")
LIGHTCACHE = os.environ.get("MAXRESCUE_LIGHTCACHE", "")
CAMERA = os.environ.get("MAXRESCUE_CAMERA", "")
WIDTH = int(os.environ.get("MAXRESCUE_WIDTH", "1280") or 1280)
HEIGHT = int(os.environ.get("MAXRESCUE_HEIGHT", "720") or 720)
OUT_DIR = os.environ.get("MAXRESCUE_OUT") or os.path.dirname(RESULT) or os.getcwd()

if REPO and REPO not in sys.path:
    sys.path.insert(0, REPO)

import pymxs  # noqa: E402  — available only inside Max, by design

from maxrescue.maxbridge.render_services import MaxRenderServices  # noqa: E402

rt = pymxs.runtime
REPORT_PATH = os.path.join(OUT_DIR, "reference_report.json")
report: dict = {"log": []}


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


def pick_camera():
    """The camera to render from. A named one, or the only one, or the view."""
    cameras = list(rt.cameras or [])
    named = [c for c in cameras if str(getattr(c, "name", "")) == CAMERA]
    if CAMERA and not named:
        raise RuntimeError(
            "camera %r not found; scene has: %s"
            % (CAMERA, [str(c.name) for c in cameras])
        )
    if named:
        return named[0]
    if len(cameras) == 1:
        return cameras[0]
    return None


def main() -> str:
    if not (TARGET and os.path.isfile(TARGET)):
        return "REFERENCE_FAIL no MAXRESCUE_TARGET"
    if not REFERENCE:
        return "REFERENCE_FAIL no MAXRESCUE_REFERENCE output path"

    started = time.time()
    log("opening %s (this needs a machine the scene still fits on)" % TARGET)
    opened = rt.loadMaxFile(
        TARGET,
        useFileUnits=True,
        quiet=True,
        missingExtFilesAction=rt.name("logmsg"),
        missingDLLsAction=rt.name("logmsg"),
        missingXRefsAction=rt.name("logmsg"),
    )
    if not opened:
        return "REFERENCE_FAIL loadMaxFile returned false"

    render = MaxRenderServices()
    report["renderer_properties"] = list(render.available_properties())
    log("renderer exposes %d properties" % len(report["renderer_properties"]))

    camera = pick_camera()
    report["camera"] = str(camera.name) if camera is not None else "<active view>"
    log("rendering from %s at %dx%d" % (report["camera"], WIDTH, HEIGHT))

    # Both renders must agree on resolution, or idiff reports "different sizes"
    # and says nothing about whether the reduction moved a pixel.
    rt.renderWidth = WIDTH
    rt.renderHeight = HEIGHT

    saved = render.prepare_deterministic(light_cache_file=LIGHTCACHE or None)
    report["determinism_notes"] = list(render.notes)
    for note in render.notes:
        log("! %s" % note)

    try:
        if camera is not None:
            rt.viewport.setCamera(camera)
        ok = render.render_to(REFERENCE)
    finally:
        render.restore(saved)

    if not ok:
        return "REFERENCE_FAIL render produced no file"

    report["reference"] = REFERENCE
    report["lightcache"] = LIGHTCACHE
    report["seconds"] = round(time.time() - started, 1)
    return "REFERENCE_OK %s in %.0fs" % (REFERENCE, time.time() - started)


try:
    summary = main()
except Exception as exc:
    summary = "REFERENCE_FAIL %s: %s" % (type(exc).__name__, exc)
    report["traceback"] = traceback.format_exc()
    log(summary)

report["summary"] = summary
flush()
write_result(summary)
log(summary)
