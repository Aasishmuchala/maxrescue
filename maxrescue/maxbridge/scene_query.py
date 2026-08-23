"""Reading the open scene.

One bulk crossing of the MAXScript boundary (`mrNodeFacts`) returning a nested
array, then everything else is Python. Per-node `pymxs` access is ~10× slower
and produced a nine-minute stall on a 2,000-object scene in a sibling project.
"""

from __future__ import annotations

import os

import pymxs

from maxrescue.core.types import Engine, MemorySample, NodeFacts, SceneStats
from maxrescue.maxbridge.maxscript import compile_helpers

rt = pymxs.runtime

_BITMAP_CLASSES = (
    ("Bitmaptexture", "filename"),
    ("VRayBitmap", "HDRIMapName"),
    ("VRayHDRI", "HDRIMapName"),
    ("CoronaBitmap", "filename"),
)

_MALWARE_SIGNATURES = (
    "CRP",
    "ALC",
    "ALC2",
    "ADSL",
    "physxpluginmfx",
    "PhysXPluginMfx",
    "MSCPROP",
    "AlienBrain",
    "vaccinate",
)


def _text(value) -> str:
    try:
        return str(value)
    except Exception:
        return ""


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _bool(value, default: bool = False) -> bool:
    try:
        return bool(value)
    except Exception:
        return default


class MaxSceneQuery:
    def __init__(self, memory_probe=None) -> None:
        self._memory = memory_probe
        self._compiled = False

    def _ensure_compiled(self) -> None:
        if not self._compiled:
            compile_helpers(rt)
            self._compiled = True

    # -- nodes -------------------------------------------------------------

    def nodes(self) -> tuple[NodeFacts, ...]:
        self._ensure_compiled()
        rows = rt.mrNodeFacts()
        out: list[NodeFacts] = []
        for row in rows or []:
            try:
                values = list(row)
            except TypeError:
                continue
            if len(values) < 24:
                continue
            base = _int(values[6])
            out.append(
                NodeFacts(
                    handle=_int(values[0]),
                    name=_text(values[1]),
                    class_name=_text(values[2]),
                    super_class=_text(values[3]),
                    faces=_int(values[4]),
                    vertices=_int(values[5]),
                    base_handle=base or None,
                    modifier_classes=tuple(_text(m) for m in (values[7] or [])),
                    layer=_text(values[8]),
                    child_count=_int(values[9]),
                    has_parent=_bool(values[10]),
                    has_dependents=_bool(values[11]),
                    in_group=_bool(values[12]),
                    is_group_head=_bool(values[13]),
                    is_hidden=_bool(values[14]),
                    renderable=_bool(values[15], default=True),
                    primary_visibility=_bool(values[16], default=True),
                    is_animated=_bool(values[17]),
                    has_skin=_bool(values[18]),
                    is_bone=_bool(values[19]),
                    in_xref=_bool(values[20]),
                    scripted_controller=_bool(values[21]),
                    negative_scale=_bool(values[22]),
                    render_coupled_modifiers=tuple(
                        _text(m) for m in (values[23] or [])
                    ),
                )
            )
        return tuple(out)

    def node_exists(self, handle: int) -> bool:
        try:
            node = rt.getAnimByHandle(handle)
            return node is not None and bool(rt.isValidNode(node))
        except Exception:
            return False

    # -- materials ---------------------------------------------------------

    def material_in_use(self, handle: int) -> bool:
        """Usage by handle, never library membership.

        `sceneMaterials` still contains a material after it is unassigned, so
        membership is not liveness and a verify built on it halts good runs.
        """
        try:
            for node in rt.objects:
                material = getattr(node, "material", None)
                if material is None:
                    continue
                if int(rt.getHandleByAnim(material)) == int(handle):
                    return True
        except Exception:
            return False
        return False

    # -- scene-level -------------------------------------------------------

    def engine(self) -> Engine:
        try:
            name = str(rt.classOf(rt.renderers.current))
        except Exception:
            return Engine.UNKNOWN
        # Never exact equality: V-Ray's class name changes between hotfixes.
        if rt.matchPattern(name, pattern="V_Ray*") or rt.matchPattern(
            name, pattern="VRay*"
        ):
            return Engine.VRAY
        if rt.matchPattern(name, pattern="Corona*"):
            return Engine.CORONA
        return Engine.OTHER

    def render_hidden(self) -> bool:
        """Conservative on failure: if this cannot be read, assume hidden
        objects DO render, so nothing gets deleted on a guess."""
        try:
            return bool(rt.rendHidden)
        except Exception:
            return True

    def scene_path(self) -> str:
        folder = str(rt.maxFilePath or "")
        name = str(rt.maxFileName or "")
        return os.path.join(folder, name) if folder and name else ""

    def stats(self) -> SceneStats:
        polygons = vertices = 0
        try:
            for node in rt.geometry:
                counts = rt.getPolygonCount(node)
                polygons += _int(counts[0])
                vertices += _int(counts[1])
        except Exception:
            pass

        texture_mb, missing = self._texture_weight()
        memory: MemorySample | None = None
        if self._memory is not None:
            memory = self._memory.sample()

        return SceneStats(
            polygons=polygons,
            vertices=vertices,
            nodes=_int(getattr(rt.objects, "count", 0)),
            materials=_int(getattr(rt.sceneMaterials, "count", 0)),
            file_mb=self._file_mb(),
            texture_mb=texture_mb,
            missing_assets=missing,
            memory=memory,
        )

    def _file_mb(self) -> float:
        path = self.scene_path()
        try:
            return round(os.path.getsize(path) / (1024 * 1024), 1) if path else 0.0
        except OSError:
            return 0.0

    def _texture_weight(self) -> tuple[float, int]:
        seen: set[int] = set()
        total = 0
        missing = 0
        for class_name, prop in _BITMAP_CLASSES:
            cls = getattr(rt, class_name, None)
            if cls is None:
                continue
            try:
                instances = rt.getClassInstances(cls)
            except Exception:
                continue
            for instance in instances or []:
                try:
                    handle = int(rt.getHandleByAnim(instance))
                except Exception:
                    continue
                # VRayHDRI and VRayBitmap alias the same objects; dedupe by
                # handle or the total doubles.
                if handle in seen:
                    continue
                seen.add(handle)
                path = _text(getattr(instance, prop, ""))
                if not path:
                    continue
                try:
                    total += os.path.getsize(path)
                except OSError:
                    missing += 1
        return round(total / (1024 * 1024), 1), missing

    def saved_file_mb(self) -> float:
        """Weight via one temporary save. Expensive — called once per run."""
        import tempfile

        temp = os.path.join(
            tempfile.gettempdir(), f"maxrescue_measure_{os.getpid()}.max"
        )
        try:
            rt.saveMaxFile(temp, useNewFile=False, quiet=True, clearNeedSaveFlag=False)
            return round(os.path.getsize(temp) / (1024 * 1024), 1)
        except Exception:
            return 0.0
        finally:
            try:
                os.remove(temp)
            except OSError:
                pass

    # -- security ----------------------------------------------------------

    def suspicious_callbacks(self) -> tuple[str, ...]:
        """Persistent callback scripts matching a known payload id."""
        self._ensure_compiled()
        try:
            text = str(rt.mrSuspiciousCallbacks())
        except Exception:
            return ()
        found = []
        lowered = text.lower()
        for signature in _MALWARE_SIGNATURES:
            # Match the callback-id form, not a bare substring: "ALC" appears
            # inside plenty of innocent identifiers.
            if f"#{signature.lower()}" in lowered or f"id:#{signature.lower()}" in lowered:
                found.append(signature)
        return tuple(found)

    def scene_states(self) -> tuple[str, ...]:
        self._ensure_compiled()
        try:
            return tuple(str(s) for s in (rt.mrSceneStates() or []))
        except Exception:
            return ()
