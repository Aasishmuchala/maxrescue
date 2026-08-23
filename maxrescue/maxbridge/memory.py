"""Process memory, with provenance.

Three sources, tried in order, and the sample records which one answered. Three
sources that disagree is itself a finding — spike S2 exists to check exactly
that before any number here is believed.
"""

from __future__ import annotations

import os
import subprocess

import pymxs

from maxrescue.core.types import MemorySample

rt = pymxs.runtime
MB = 1024 * 1024

#: Field order of sysinfo.getMAXMemoryInfo(). Index 2 is WorkingSet.
_FIELDS = (
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


def _psutil_rss():
    try:
        import psutil

        info = psutil.Process(os.getpid()).memory_info()
        return info.rss / MB, getattr(info, "peak_wset", 0) / MB
    except Exception:
        return None


def _max_memory():
    """Max's own counters.

    Whether pymxs surfaces this array 0-indexed or 1-indexed is not documented,
    so read it as a sequence and fall back to explicit indexing. Guessing wrong
    silently reports PageFaultCount as a byte count.
    """
    try:
        raw = rt.sysinfo.getMAXMemoryInfo()
        try:
            values = [int(v) for v in raw]
        except TypeError:
            values = [int(raw[i]) for i in range(1, 10)]
        if len(values) != len(_FIELDS):
            return None
        table = dict(zip(_FIELDS, values))
        return table["WorkingSet"] / MB, table["PeakWorkingSet"] / MB, table[
            "PagefileUsage"
        ] / MB
    except Exception:
        return None


def _ctypes_rss():
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
        return counters.WorkingSetSize / MB, counters.PeakWorkingSetSize / MB
    except Exception:
        return None


def _vram_mb():
    """VRAM for this process. Absent GPU degrades to None, never raises."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.STDOUT, timeout=10,
        ).decode("utf-8", "replace")
        pid = str(os.getpid())
        for row in out.strip().splitlines():
            parts = [p.strip() for p in row.split(",")]
            if len(parts) >= 2 and parts[0] == pid:
                return float(parts[1])
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.STDOUT, timeout=10,
        ).decode("utf-8", "replace")
        return float(out.strip().splitlines()[0])
    except Exception:
        return None


class MaxMemoryProbe:
    def sample(self) -> MemorySample:
        vram = _vram_mb()

        values = _psutil_rss()
        if values:
            return MemorySample(
                rss_mb=round(values[0], 1), peak_rss_mb=round(values[1], 1),
                vram_mb=vram, source="psutil",
            )

        values = _max_memory()
        if values:
            return MemorySample(
                rss_mb=round(values[0], 1), peak_rss_mb=round(values[1], 1),
                pagefile_mb=round(values[2], 1), vram_mb=vram,
                source="sysinfo.getMAXMemoryInfo",
            )

        values = _ctypes_rss()
        if values:
            return MemorySample(
                rss_mb=round(values[0], 1), peak_rss_mb=round(values[1], 1),
                vram_mb=vram, source="GetProcessMemoryInfo",
            )

        # Zero with an explicit source, so the report falls back to polygons
        # rather than printing a confident 0%.
        return MemorySample(rss_mb=0.0, vram_mb=vram, source="unavailable")

    def settle(self) -> None:
        for call in (lambda: rt.gc(light=False), rt.gc):
            try:
                call()
                return
            except Exception:
                continue
