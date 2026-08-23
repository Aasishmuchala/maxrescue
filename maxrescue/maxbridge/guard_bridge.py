"""The material guard.

Snapshotting **live references**, not handles, is the entire trick. A handle to a
purged material points at freed memory; a live reference holds a refcount that
keeps the material alive so it can be re-attached afterwards.

Pins are released in the session's `finally`. Leaking them would keep every
material in the scene alive for the life of the process — in a tool whose whole
purpose is memory, that would be an unusually embarrassing bug.
"""

from __future__ import annotations

import pymxs

from maxrescue.core.types import Assignment
from maxrescue.maxbridge.maxscript import compile_helpers

rt = pymxs.runtime


class MaxGuardServices:
    def __init__(self) -> None:
        self._pins: dict[int, object] = {}
        self._compiled = False

    def _ensure_compiled(self) -> None:
        if not self._compiled:
            compile_helpers(rt)
            self._compiled = True

    def _walk(self, pin: bool) -> tuple[Assignment, ...]:
        """All node→material bindings, read in ONE MAXScript crossing.

        This runs twice per session and a session runs per batch, so per-node
        pymxs access here is five million crossings on a 50k-object scene across
        50 batches — the exact shape that stalled a sibling project.

        Pinning still walks the pinned handles individually, because a pin has
        to hold the LIVE reference: a handle to a purged material points at
        freed memory, and the refcount is the whole reason re-attachment works.
        """
        self._ensure_compiled()
        try:
            rows = rt.mrMaterialAssignments()
        except Exception:
            return ()

        out: list[Assignment] = []
        for row in rows or []:
            try:
                node_handle = int(row[0])
                material_handle = int(row[1]) or None
            except Exception:
                continue

            if pin and material_handle is not None and material_handle not in self._pins:
                try:
                    material = rt.getAnimByHandle(material_handle)
                    if material is not None:
                        self._pins[material_handle] = material
                except Exception:
                    pass

            out.append(
                Assignment(node_handle=node_handle, material_handle=material_handle)
            )
        return tuple(out)

    def snapshot(self) -> tuple[Assignment, ...]:
        self._pins.clear()
        return self._walk(pin=True)

    def current(self) -> tuple[Assignment, ...]:
        return self._walk(pin=False)

    def reattach(self, node_handle: int, material_handle: int) -> bool:
        try:
            node = rt.getAnimByHandle(node_handle)
            if node is None or not rt.isValidNode(node):
                return False
            material = self._pins.get(material_handle) or rt.getAnimByHandle(
                material_handle
            )
            if material is None:
                return False
            node.material = material
            # Read back and compare handles — assignment reporting success is
            # not the same as the assignment having happened.
            return int(rt.getHandleByAnim(node.material)) == int(material_handle)
        except Exception:
            return False

    def release(self) -> None:
        self._pins.clear()
