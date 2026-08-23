"""Mutations.

Two rules run through everything here:

1. **A failed export never reaches a delete.** The constructive half must
   verifiably succeed before the destructive half is even considered.
2. **Never guess a plugin property name.** Forest Pack, RailClone and tyFlow
   property names are not documented and change between versions, so this
   discovers them at runtime with `showProperties` and skips — loudly — when it
   cannot find one. A guessed property that happens to exist and means something
   else is far worse than a skip.
"""

from __future__ import annotations

import ntpath
import os
import re

import pymxs

rt = pymxs.runtime

#: VRayProxy display modes. 0 is bounding box — anything heavier defeats the
#: purpose, since the preview mesh then lives in RAM too.
PROXY_DISPLAY_BOUNDING_BOX = 0

#: The ONLY render-identical bitmap mode. `#renderMode_UseProxies` renders the
#: downscaled image and changes the result.
BITMAP_MODE_SAFE = "renderMode_UseFullRes_FlushFromMemory"

#: Candidate viewport-display properties, in preference order. Viewport group
#: only — never the render group, never "Disable Object".
_SCATTER_DISPLAY_CANDIDATES = (
    ("dispmode", 3),
    ("displaymode", 3),
    ("viewport_display", 3),
    ("vpdisplay", 1),
    ("display_mode", 1),
)


class MaxFixServices:
    def __init__(self) -> None:
        self.notes: list[str] = []

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _node(handle: int):
        node = rt.getAnimByHandle(handle)
        if node is None or not rt.isValidNode(node):
            raise RuntimeError(f"node {handle} no longer exists")
        return node

    # -- hygiene -----------------------------------------------------------

    def free_scene_bitmaps(self) -> None:
        rt.freeSceneBitmaps()

    def clear_undo_buffer(self) -> None:
        rt.clearUndoBuffer()

    def collect_garbage(self) -> None:
        try:
            rt.gc(light=False)
        except Exception:
            rt.gc()

    def strip_notetracks(self, handle: int) -> int:
        node = self._node(handle)
        removed = 0
        try:
            count = int(rt.numNoteTracks(node))
        except Exception:
            return 0
        # Backwards: deleting shifts the indices of everything after it.
        for index in range(count, 0, -1):
            try:
                track = rt.getNoteTrack(node, index)
                rt.deleteNoteTrack(node, track)
                removed += 1
            except Exception:
                continue
        return removed

    def strip_scene_state(self, name: str) -> None:
        # `Delete`, not `DeleteSceneState` — the latter does not exist.
        rt.sceneStateMgr.Delete(name)

    def strip_motion_mixer(self) -> int:
        """Motion Mixer data survives a merge and can be enormous."""
        removed = 0
        try:
            while int(rt.theMixer.numMaxMixers()) > 0:
                rt.theMixer.removeMaxMixer(1, False, 1)
                removed += 1
                if removed > 10_000:  # never spin on a misbehaving API
                    break
        except Exception:
            pass
        return removed

    def purge_unused_materials(self) -> int:
        """Delete materials nothing references.

        `rt.free` does NOT delete a material; `deleteItem` does. Direct indexing
        is 0-based while `deleteItem`'s argument is 1-based, which is a
        delightful way to delete the wrong one.
        """
        used: set[int] = set()
        try:
            for node in rt.objects:
                material = getattr(node, "material", None)
                if material is not None:
                    used.add(int(rt.getHandleByAnim(material)))
        except Exception:
            return 0

        removed = 0
        try:
            library = rt.sceneMaterials
            for index in range(int(library.count) - 1, -1, -1):
                material = library[index]
                try:
                    handle = int(rt.getHandleByAnim(material))
                except Exception:
                    continue
                if handle in used:
                    continue
                rt.deleteItem(library, index + 1)  # 1-based argument
                removed += 1
        except Exception:
            pass
        return removed

    def remove_suspicious_callback(self, signature: str) -> None:
        rt.callbacks.removeScripts(id=rt.name(signature))

    # -- nodes -------------------------------------------------------------

    def delete_node(self, handle: int) -> None:
        rt.delete(self._node(handle))

    # -- stacks ------------------------------------------------------------

    def collapse_stack(self, handle: int) -> None:
        """Collapse the whole stack while preserving instancing.

        The trailing `False` is what keeps instances instanced; without it every
        member of an instance set becomes a unique object and memory goes UP.
        """
        node = self._node(handle)
        count = int(node.modifiers.count)
        if count == 0:
            return
        rt.maxOps.CollapseNodeTo(node, count, False)

    # -- proxies -----------------------------------------------------------

    def convert_to_proxy(self, handle: int, out_dir: str) -> int:
        node = self._node(handle)
        name = str(node.name)
        target_dir = self._resolve_out_dir(out_dir)
        os.makedirs(target_dir, exist_ok=True)
        path = self._unique_path(target_dir, name)

        rt.select(node)
        created = rt.vrayMeshExport(
            meshFile=path,
            autoCreateProxies=True,
            exportMultiple=False,
            createMultiMtl=True,
            oneVoxelPerMesh=True,
            maxPreviewFaces=1000,
            exportPointClouds=False,
        )

        # The file must exist before anything destructive is contemplated.
        if not os.path.exists(path):
            raise RuntimeError(f"vrayMeshExport produced no file for {name}")

        proxy = None
        try:
            for item in created or []:
                proxy = item
                break
        except TypeError:
            # The documented return array is not iterable on some builds.
            proxy = self._find_proxy_by_file(path)
        if proxy is None:
            proxy = self._find_proxy_by_file(path)
        if proxy is None:
            raise RuntimeError(
                f"exported {name} but could not identify the created proxy; "
                "the original has been left untouched"
            )

        try:
            return int(rt.getHandleByAnim(proxy))
        except Exception:
            return 0

    def set_proxy_display(self, handle: int, mode: int = PROXY_DISPLAY_BOUNDING_BOX) -> None:
        """Never leave a modifier on a proxy — it reverts to a regular mesh and
        loses every saving."""
        try:
            node = self._node(handle)
            node.display = mode
        except Exception as exc:
            self.notes.append(f"could not set proxy display on {handle}: {exc}")

    @staticmethod
    def _resolve_out_dir(out_dir: str) -> str:
        """Resolve relative to the SCENE, never the process CWD.

        Max's working directory is Program Files; a relative path resolved there
        raises PermissionError and halted a run on default settings.
        """
        if out_dir and ntpath.isabs(out_dir):
            return out_dir
        folder = str(rt.maxFilePath or "") or os.getcwd()
        return ntpath.join(folder, out_dir or "proxies")

    @staticmethod
    def _unique_path(folder: str, name: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "mesh"
        for index in range(0, 10_000):
            suffix = "" if index == 0 else f"_{index:03d}"
            path = ntpath.join(folder, f"{safe}{suffix}.vrmesh")
            if not os.path.exists(path):
                return path
        raise RuntimeError(f"cannot find a free .vrmesh name for {name}")

    @staticmethod
    def _find_proxy_by_file(path: str):
        """Recover the created proxy by its file.

        Only safe because `_unique_path` guarantees the filename is fresh. If
        more than one node claims it, refuse rather than guess.
        """
        matches = []
        try:
            cls = getattr(rt, "VRayProxy", None)
            if cls is None:
                return None
            for node in rt.getClassInstances(cls):
                try:
                    if str(node.fileName).lower() == path.lower():
                        matches.append(node)
                except Exception:
                    continue
        except Exception:
            return None
        return matches[0] if len(matches) == 1 else None

    # -- viewport-only levers ---------------------------------------------

    def set_bitmap_proxy_mode(self, mode: str = BITMAP_MODE_SAFE) -> None:
        if "UseProxies" in mode:
            raise ValueError(
                "renderMode_UseProxies renders the downscaled image and changes "
                "the result; it must never be set automatically"
            )
        manager = getattr(rt, "BitmapProxyMgr", None)
        if manager is None:
            self.notes.append("BitmapProxyMgr unavailable; bitmap paging skipped")
            return
        manager.globalProxyEnable = True
        manager.globalProxyRenderMode = rt.name(mode)

    def set_nitrous_texture_limit(self, pixels: int) -> None:
        manager = getattr(rt, "NitrousGraphicsManager", None)
        if manager is None:
            self.notes.append("NitrousGraphicsManager unavailable; VRAM cap skipped")
            return
        for setter, argument in (
            ("SetTextureSizeLimit", (pixels, True)),
            ("SetBackgroundTextureSizeLimit", (pixels, True)),
            ("SetProceduralTextureSizeLimit", (pixels,)),
        ):
            try:
                getattr(manager, setter)(*argument)
            except Exception as exc:
                self.notes.append(f"{setter} failed: {exc}")
        try:
            manager.ForceDisableMipMapGeneration(True)
        except Exception:
            pass

    def set_scatter_display(self, handle: int, mode: str = "points") -> bool:
        """Switch a scatter object to a cheap viewport representation.

        Property names for Forest Pack / RailClone / tyFlow are undocumented and
        version-dependent, so they are discovered rather than assumed. A plugin
        whose property cannot be found is skipped and noted — guessing a
        property that exists but means something else could change the render.
        """
        try:
            node = self._node(handle)
        except Exception:
            return False

        available = self._properties(node)
        for prop, value in _SCATTER_DISPLAY_CANDIDATES:
            if prop not in available:
                continue
            try:
                setattr(node, prop, value)
                return True
            except Exception as exc:
                self.notes.append(f"{node.name}: setting {prop} failed ({exc})")
        self.notes.append(
            f"{getattr(node, 'name', handle)}: no known viewport-display property "
            f"(saw: {sorted(available)[:12]}) — left untouched"
        )
        return False

    @staticmethod
    def _properties(node) -> set[str]:
        try:
            names = rt.getPropNames(node)
            return {str(n).lower() for n in (names or [])}
        except Exception:
            return set()

    def set_show_in_viewport(self, material_handle: int, show: bool) -> None:
        try:
            material = rt.getAnimByHandle(material_handle)
            if material is not None:
                rt.showTextureMap(material, show)
        except Exception:
            pass

    # -- opt-in, gated by an image diff -----------------------------------

    def convert_bitmaps_to_vray(self, tiled: bool = True) -> int:
        """NOT render-identical. Only ever called behind the diff gate.

        VRayBitmap defaults to a sharper filtering kernel than Max's Bitmap, so
        the image legitimately changes. Blur is reset to 1.0 to remove the one
        difference that is a pure artefact.
        """
        converted = 0
        cls = getattr(rt, "Bitmaptexture", None)
        vray_cls = getattr(rt, "VRayBitmap", None)
        if cls is None or vray_cls is None:
            self.notes.append("VRayBitmap unavailable; conversion skipped")
            return 0

        for texture in list(rt.getClassInstances(cls) or []):
            try:
                path = str(texture.filename)
                if not path:
                    continue
                replacement = vray_cls()
                replacement.HDRIMapName = path
                replacement.name = str(texture.name)
                # A blur other than 1.0 is a Max-specific artefact and would
                # show up as a difference that is nothing to do with the loader.
                try:
                    replacement.blur = 1.0
                except Exception:
                    pass
                rt.replaceInstances(texture, replacement)
                converted += 1
            except Exception as exc:
                self.notes.append(f"bitmap conversion failed for one texture: {exc}")
        return converted
