"""Deterministic rendering, for the identity gate.

Two renders of an *unmodified* scene differ unless determinism is forced. The
main culprit is the light cache, which is computed multi-threaded and therefore
differs run to run — so it is computed once, saved, and both sides of the
comparison load the same file.

Property names here are read from the live renderer rather than assumed: several
are unverified in the V-Ray 7 docs, and the spike dumps `showProperties` so the
real names end up in the report instead of in someone's memory.
"""

from __future__ import annotations

import os

import pymxs

rt = pymxs.runtime

#: Anything the gate depends on gets saved and restored around a render.
_TRACKED = (
    "dmc_lockNoisePattern",
    "dmc_randomSeed",
    "imageSampler_type",
    "output_saveRawFile",
    "output_rawFileName",
    "output_splitgbuffer",
    "system_vrayLog_level",
    "lightcache_mode",
    "lightcache_loadFileName",
    "lightcache_saveFileName",
    "lightcache_autoSave",
    "lightcache_dontDelete",
    "options_dontRenderImage",
    "imageSampler_renderMask_type",
)


def _get(renderer, name, default=None):
    try:
        return getattr(renderer, name)
    except Exception:
        return default


def _set(renderer, name, value) -> bool:
    try:
        setattr(renderer, name, value)
        return True
    except Exception:
        return False


class MaxRenderServices:
    def __init__(self) -> None:
        self.notes: list[str] = []

    def available_properties(self) -> tuple[str, ...]:
        """What this V-Ray build actually exposes — recorded, not guessed."""
        try:
            return tuple(str(n) for n in (rt.getPropNames(rt.renderers.current) or []))
        except Exception:
            return ()

    def prepare_deterministic(self, light_cache_file: str | None = None) -> dict:
        renderer = rt.renderers.current
        saved = {name: _get(renderer, name) for name in _TRACKED}
        saved["_vfb"] = _get(rt, "rendShowVFB", True)

        # Locked noise + fixed seed: without both, adaptive sampling wanders.
        _set(renderer, "dmc_lockNoisePattern", True)
        _set(renderer, "dmc_randomSeed", 12345)
        # Bucket, not progressive: progressive loads every asset and its
        # stopping point is time-dependent.
        _set(renderer, "imageSampler_type", 1)

        if light_cache_file:
            # Mode 2 = "from file" in every V-Ray build seen; if the property is
            # missing the gate still works, it is just slower and noisier.
            if not _set(renderer, "lightcache_mode", 2):
                self.notes.append(
                    "could not set the light cache to load from file — two "
                    "renders may differ for reasons unrelated to the change"
                )
            # LOAD, not save. Setting only the save name recomputes the cache
            # on both sides of the comparison, so two renders of an unmodified
            # scene differ and the gate rejects everything.
            if not _set(renderer, "lightcache_loadFileName", light_cache_file):
                self.notes.append(
                    "lightcache_loadFileName could not be set — renders will not "
                    "be reproducible and any diff is meaningless"
                )
            _set(renderer, "lightcache_saveFileName", light_cache_file)
            _set(renderer, "lightcache_dontDelete", True)

        # The denoiser is a post-process; leaving it on compares denoised
        # images and hides exactly the small differences the gate exists to
        # find. Named as a requirement in the research, and previously absent.
        for element in ("VRayDenoiser", "VRayVFB_denoiser"):
            try:
                for instance in rt.getClassInstances(getattr(rt, element)) or []:
                    instance.enabled = False
            except Exception:
                continue

        try:
            rt.rendShowVFB = False
        except Exception:
            pass
        return saved

    def render_to(self, path: str) -> bool:
        renderer = rt.renderers.current
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)

        # Raw multichannel output: nothing is held in the memory frame buffer,
        # and the pixels on disk are exactly what V-Ray produced.
        _set(renderer, "output_saveRawFile", True)
        _set(renderer, "output_rawFileName", path)
        try:
            rt.render(outputFile=path, vfb=False, quiet=True)
        except Exception as exc:
            self.notes.append(f"render failed: {exc}")
            return False
        return os.path.exists(path)

    def restore(self, saved: dict) -> None:
        renderer = rt.renderers.current
        for name, value in saved.items():
            if name.startswith("_"):
                continue
            if value is not None:
                _set(renderer, name, value)
        try:
            rt.rendShowVFB = saved.get("_vfb", True)
        except Exception:
            pass
