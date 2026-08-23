"""MaxRescue P0 spikes — run INSIDE 3ds Max via `3dsmaxbatch.exe`.

These answer the questions the plan is gated on. Nothing else should be built on
top of the batch-merge design until S1 returns.

Design rules, each paid for by a sibling project:

* **Never print results to stdout as the verdict.** `3dsmaxbatch` exit codes and
  stdout are unreliable; the harness reads a result file. A Python
  `logging.StreamHandler` writing to stdout is by itself enough to make Max
  return exit -130.
* **Flush after every section.** A teardown crash must still leave the sections
  that already ran.
* **Discover, do not assume.** Several MAXScript property names and array index
  conventions in the research are unverified. Where that is true, this script
  dumps the raw values and records which convention it used, so the answer is in
  the report rather than in someone's memory.

Environment:
    MAXRESCUE_REPO    repo root (added to sys.path)
    MAXRESCUE_RESULT  verdict file the harness reads
    MAXRESCUE_TARGET  the .max file to probe
    MAXRESCUE_OUT     directory for the JSON report
    MAXRESCUE_ALLOW_FULL_OPEN
                      set to 1 to permit S3, which loads the ENTIRE scene.
                      Only set this on a machine with enough RAM to open it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback

REPO = os.environ.get("MAXRESCUE_REPO", "")
RESULT = os.environ.get("MAXRESCUE_RESULT", "")
TARGET = os.environ.get("MAXRESCUE_TARGET", "")
OUT_DIR = os.environ.get("MAXRESCUE_OUT") or os.path.dirname(RESULT) or os.getcwd()

if REPO and REPO not in sys.path:
    sys.path.insert(0, REPO)

import pymxs  # noqa: E402  — only available inside Max, by design

rt = pymxs.runtime

REPORT_PATH = os.path.join(OUT_DIR, "spike_report.json")
report: dict = {"sections": [], "environment": {}, "verdict": None}

_MEMORY_FIELDS = (
    "PageFaultCount",
    "PeakWorkingSet",
    "WorkingSet",
    "QuotaPeakPagedPool",
    "QuotaPagedPool",
    "QuotaPeakNonPagedPool",
    "QuotaNonPagedPool",
    "PagefileUsage",
    "PeakPagefileUsage",
)

MB = 1024 * 1024


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    """Print for the listener log AND keep it in the report."""
    print("[maxrescue] %s" % message)
    report.setdefault("log", []).append(message)


def flush() -> None:
    """Persist after every section — a later crash must not lose earlier data."""
    try:
        with open(REPORT_PATH, "w") as handle:
            json.dump(report, handle, indent=2, default=str)
    except Exception as exc:  # pragma: no cover - on-box only
        print("[maxrescue] could not write report: %s" % exc)


def write_result(text: str) -> None:
    if not RESULT:
        return
    try:
        with open(RESULT, "w") as handle:
            handle.write(text)
    except Exception as exc:  # pragma: no cover - on-box only
        print("[maxrescue] could not write result file: %s" % exc)


def section(name: str):
    """Run one spike, recording PASS/WARN/FAIL without aborting the battery."""

    def decorator(fn):
        started = time.time()
        entry = {"name": name, "status": "FAIL", "seconds": 0.0, "data": {}}
        try:
            data = fn() or {}
            entry["status"] = data.pop("_status", "PASS")
            entry["data"] = data
        except Exception as exc:
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            entry["traceback"] = traceback.format_exc()
            log("%s FAILED — %s" % (name, exc))
        entry["seconds"] = round(time.time() - started, 2)
        report["sections"].append(entry)
        log("%s -> %s (%.1fs)" % (name, entry["status"], entry["seconds"]))
        flush()
        return fn

    return decorator


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------


def max_memory() -> dict:
    """Max's own view of this process's memory.

    `sysinfo.getMAXMemoryInfo()` returns nine values. Whether pymxs surfaces
    that array 0-indexed or 1-indexed is NOT established in the docs, so this
    reads it as a sequence, records the raw list, and reports which route
    worked. Getting this wrong silently reports PageFaultCount as bytes.
    """
    raw = rt.sysinfo.getMAXMemoryInfo()
    route = "iterable"
    try:
        values = [int(v) for v in raw]
    except TypeError:
        route = "1-based-index"
        values = [int(raw[i]) for i in range(1, 10)]

    if len(values) != len(_MEMORY_FIELDS):
        return {"_raw": values, "_route": route, "_unexpected_length": len(values)}

    out = dict(zip(_MEMORY_FIELDS, values))
    out["_raw"] = values
    out["_route"] = route
    return out


def working_set_mb() -> float:
    info = max_memory()
    value = info.get("WorkingSet")
    if value is None:
        raw = info.get("_raw") or [0]
        value = max(raw)  # fail loud-ish rather than silently reporting zero
    return round(value / MB, 1)


def gpu_memory_mb() -> dict:
    """VRAM via nvidia-smi. Absent GPU degrades to None, never raises."""
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.STDOUT,
        )
        rows = out.decode("utf-8", "replace").strip().splitlines()
        parsed = []
        for row in rows:
            used, total = [p.strip() for p in row.split(",")[:2]]
            parsed.append({"used_mb": int(used), "total_mb": int(total)})
        return {"gpus": parsed}
    except Exception as exc:
        return {"gpus": None, "error": str(exc)}


def psutil_rss_mb():
    try:
        import psutil  # noqa: PLC0415

        return round(psutil.Process(os.getpid()).memory_info().rss / MB, 1)
    except Exception:
        return None


def ctypes_rss_mb():
    """Working set straight from the OS, as an independent third opinion."""
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return None
        return round(counters.WorkingSetSize / MB, 1)
    except Exception:
        return None


def settle() -> None:
    """Give Max a chance to release what it is going to release."""
    try:
        rt.gc(light=False)
    except Exception:
        try:
            rt.gc()
        except Exception:
            pass


def reset_scene() -> None:
    rt.resetMaxFile(rt.name("noPrompt"))
    settle()


# ---------------------------------------------------------------------------
# S0 — environment
# ---------------------------------------------------------------------------


@section("S0 environment")
def s0_environment():
    version = [int(v) for v in rt.maxVersion()]
    data = {
        "max_version_raw": version,
        "max_product": str(rt.maxVersion()[0] // 1000) if version else "?",
        "python": sys.version,
        "non_interactive": bool(rt.maxOps.isInNonInteractiveMode()),
        "target": TARGET,
        "target_exists": bool(TARGET) and os.path.isfile(TARGET),
        "target_bytes": os.path.getsize(TARGET)
        if TARGET and os.path.isfile(TARGET)
        else None,
        "psutil_available": psutil_rss_mb() is not None,
        "renderer": str(rt.classOf(rt.renderers.current)),
    }
    report["environment"] = data
    if not data["target_exists"]:
        data["_status"] = "WARN"
        log("TARGET not found — S1/S3 will be skipped. Set MAXRESCUE_TARGET.")
    return data


# ---------------------------------------------------------------------------
# S2 — memory probe fidelity (run before S1 so its numbers can be trusted)
# ---------------------------------------------------------------------------


@section("S2 memory probe fidelity")
def s2_memory_probe():
    info = max_memory()
    sources = {
        "max_getMAXMemoryInfo_mb": working_set_mb(),
        "psutil_mb": psutil_rss_mb(),
        "ctypes_GetProcessMemoryInfo_mb": ctypes_rss_mb(),
    }
    present = [v for v in sources.values() if v]
    agree = None
    if len(present) >= 2:
        agree = (max(present) - min(present)) / max(present) < 0.05

    data = {
        "sources": sources,
        "within_5_percent": agree,
        "raw_getMAXMemoryInfo": info,
        "system_memory": [int(v) for v in rt.sysinfo.getSystemMemoryInfo()],
        "gpu": gpu_memory_mb(),
    }
    if agree is False:
        data["_status"] = "WARN"
        log("memory sources disagree by more than 5%% — %s" % sources)
    return data


# ---------------------------------------------------------------------------
# S1 — THE GATE: does a selective merge deserialise the whole file?
# ---------------------------------------------------------------------------


@section("S1 merge peak (THE GATE)")
def s1_merge_peak():
    if not (TARGET and os.path.isfile(TARGET)):
        return {"_status": "WARN", "skipped": "no MAXRESCUE_TARGET"}

    reset_scene()
    baseline = working_set_mb()
    log("baseline working set: %.1f MB" % baseline)

    # --- reading the object list should not load the scene
    started = time.time()
    missing_dlls = []
    names = rt.getMAXFileObjectNames(
        TARGET,
        quiet=True,
        missingDLLsAction=rt.name("logmsg"),
        missingDLLsList=pymxs.byref(missing_dlls),
    )
    listing_seconds = round(time.time() - started, 2)
    name_list = [str(n) for n in names] if names else []
    after_listing = working_set_mb()
    log(
        "getMAXFileObjectNames: %d names in %.1fs, +%.1f MB"
        % (len(name_list), listing_seconds, after_listing - baseline)
    )

    data = {
        "target_bytes": os.path.getsize(TARGET),
        "baseline_mb": baseline,
        "listing": {
            "names": len(name_list),
            "seconds": listing_seconds,
            "working_set_mb": after_listing,
            "delta_mb": round(after_listing - baseline, 1),
            "missing_dlls": [str(d) for d in missing_dlls] if missing_dlls else [],
            "duplicate_names": len(name_list) - len(set(name_list)),
        },
        "merges": [],
    }
    flush()

    if not name_list:
        data["_status"] = "FAIL"
        data["note"] = "no object names returned — cannot proceed to merge"
        return data

    # --- merge progressively larger selections, measuring peak each time
    for count in (1, 100, 1000):
        if count > len(name_list):
            continue
        reset_scene()
        before = working_set_mb()
        selection = name_list[:count]
        merged = []
        started = time.time()
        ok = rt.mergeMaxFile(
            TARGET,
            selection,
            rt.name("noRedraw"),
            rt.name("mergeDups"),
            rt.name("useSceneMtlDups"),
            rt.name("neverReparent"),
            quiet=True,
            mergedNodes=pymxs.byref(merged),
            missingExtFilesAction=rt.name("logmsg"),
            missingDLLsAction=rt.name("logmsg"),
            missingXRefsAction=rt.name("logmsg"),
        )
        seconds = round(time.time() - started, 2)
        peak = max_memory().get("PeakWorkingSet", 0) / MB
        after = working_set_mb()
        entry = {
            "requested": count,
            "ok": bool(ok),
            "merged": len(merged) if merged else 0,
            "seconds": seconds,
            "before_mb": before,
            "after_mb": after,
            "delta_mb": round(after - before, 1),
            "process_peak_mb": round(peak, 1),
        }
        data["merges"].append(entry)
        log("merge %d objects -> +%.1f MB in %.1fs" % (count, entry["delta_mb"], seconds))
        flush()

    # --- the verdict the whole plan turns on
    first = data["merges"][0] if data["merges"] else None
    file_mb = data["target_bytes"] / MB
    if first is None:
        data["verdict"] = "UNKNOWN"
    elif first["delta_mb"] < max(2048, file_mb * 0.10):
        data["verdict"] = "MERGE_SELECTIVE"
        log("VERDICT: MERGE_SELECTIVE — batching is viable")
    else:
        data["verdict"] = "MERGE_FULL"
        data["_status"] = "WARN"
        log(
            "VERDICT: MERGE_FULL — merging one object cost %.1f MB. "
            "Batching does not bound peak RAM; revise P3." % first["delta_mb"]
        )
    report["verdict"] = data["verdict"]
    reset_scene()
    return data


# ---------------------------------------------------------------------------
# S3 — where the memory actually is (needs a machine the scene opens on)
# ---------------------------------------------------------------------------


@section("S3 geometry vs texture split")
def s3_geometry_vs_textures():
    if not (TARGET and os.path.isfile(TARGET)):
        return {"_status": "WARN", "skipped": "no MAXRESCUE_TARGET"}
    if os.environ.get("MAXRESCUE_ALLOW_FULL_OPEN", "").lower() not in ("1", "true", "yes"):
        return {
            "_status": "WARN",
            "skipped": (
                "full open not authorised — set MAXRESCUE_ALLOW_FULL_OPEN=1 on a "
                "machine with enough RAM (this loads the entire scene)"
            ),
        }

    reset_scene()
    baseline = working_set_mb()
    started = time.time()
    opened = rt.loadMaxFile(
        TARGET,
        useFileUnits=True,
        quiet=True,
        missingExtFilesAction=rt.name("logmsg"),
        missingDLLsAction=rt.name("logmsg"),
        missingXRefsAction=rt.name("logmsg"),
    )
    open_seconds = round(time.time() - started, 2)
    if not opened:
        return {"_status": "FAIL", "note": "loadMaxFile returned false"}

    settle()
    after_open = working_set_mb()
    gpu_open = gpu_memory_mb()

    polys = 0
    verts = 0
    for node in rt.geometry:
        try:
            counts = rt.getPolygonCount(node)
            polys += int(counts[0])
            verts += int(counts[1])
        except Exception:
            continue

    rt.freeSceneBitmaps()
    settle()
    after_free = working_set_mb()

    textures_mb = round(after_open - after_free, 1)
    data = {
        "open_seconds": open_seconds,
        "baseline_mb": baseline,
        "open_mb": after_open,
        "after_freeSceneBitmaps_mb": after_free,
        "textures_mb": textures_mb,
        "non_texture_mb": round(after_free - baseline, 1),
        "scene_total_mb": round(after_open - baseline, 1),
        "polygons": polys,
        "vertices": verts,
        "node_count": int(rt.objects.count),
        "material_count": int(rt.sceneMaterials.count),
        "gpu_at_open": gpu_open,
        "note": (
            "textures_mb is what freeSceneBitmaps() released; it is a floor for "
            "bitmap RAM, not an exact split."
        ),
    }
    log(
        "split: total %.1f MB · textures >= %.1f MB · everything else %.1f MB"
        % (data["scene_total_mb"], textures_mb, data["non_texture_mb"])
    )
    reset_scene()
    return data


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------


def _summarise() -> str:
    failures = [s for s in report["sections"] if s["status"] == "FAIL"]
    warnings = [s for s in report["sections"] if s["status"] == "WARN"]
    verdict = report.get("verdict") or "NO_VERDICT"
    if failures:
        return "SPIKES_FAIL %s failures=%d warnings=%d" % (
            verdict,
            len(failures),
            len(warnings),
        )
    return "SPIKES_OK %s warnings=%d" % (verdict, len(warnings))


summary = _summarise()
report["summary"] = summary
flush()
write_result(summary)
log(summary)
log("report written to %s" % REPORT_PATH)
