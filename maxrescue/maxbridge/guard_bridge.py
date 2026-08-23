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

rt = pymxs.runtime


class MaxGuardServices:
    def __init__(self) -> None:
        self._pins: dict[int, object] = {}

    def _walk(self, pin: bool) -> tuple[Assignment, ...]:
        out: list[Assignment] = []
        for node in rt.objects:
            try:
                handle = int(rt.getHandleByAnim(node))
            except Exception:
                continue
            material = None
            try:
                material = node.material
            except Exception:
                material = None

            material_handle = None
            if material is not None:
                try:
                    material_handle = int(rt.getHandleByAnim(material))
                except Exception:
                    material_handle = None
                if pin and material_handle is not None:
                    # Hold the live ref, not just the number.
                    self._pins[material_handle] = material

            out.append(Assignment(node_handle=handle, material_handle=material_handle))
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
