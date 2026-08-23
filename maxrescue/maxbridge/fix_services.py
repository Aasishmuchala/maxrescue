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

#: VRayProxy display modes. 0 is bounding box.
#:
#: This is VIEWPORT ONLY. Chaos: "VRayProxy imports a geometry from an external
#: mesh at render time only. The geometry is not present in the scene, and does
#: not use up any resources, EXCEPT FOR THE VIEWPORT PREVIEW." The renderer
#: streams the full mesh from the .vrmesh regardless of what the viewport shows,
#: so a bounding-box proxy renders at full quality.
PROXY_DISPLAY_BOUNDING_BOX = 0

#: Proxy properties that DO reach the renderer, with the value that means
#: "unchanged". Everything else on a VRayProxy is viewport decoration; these are
#: the ones that can silently alter the image.
#:
#: Names are marked unverified in the research, so each is checked only if the
#: build actually exposes it — and what was checked is recorded either way.
RENDER_AFFECTING_PROXY_DEFAULTS = {
    # "0.0 means that no point cloud level is loaded and the original mesh is
    # rendered instead" — a point-cloud level would render simplified geometry.
    "point_cloud_on": False,
    "pointcloud_on": False,
    "point_cloud_level_multiplier": 0.0,
    # "control the loaded vrmesh level for rendering" — render-affecting for
    # multi-resolution meshes.
    "lod_scale": 0.0,
    "scale": 1.0,
    # Y/Z swap, for meshes not exported from Max. Ours always are.
    "flip_axis": False,
    # Remaps UVs to a different channel; would change texture placement.
    "first_map_channel_on": False,
    "force_first_map_channel": False,
}

#: The ONLY render-identical bitmap mode. `#renderMode_UseProxies` renders the
#: downscaled image and changes the result.
BITMAP_MODE_SAFE = "renderMode_UseFullRes_FlushFromMemory"

#: Verified (property name -> value) pairs for scatter viewport display.
#:
#: EMPTY, and that is the point. The research is explicit that Forest Pack /
#: RailClone / tyFlow property names are undocumented — and their VALUES more so.
#: Those plugins expose a viewport display group AND a render display group, plus
#: a "Disable Object" that removes the object from the RENDER. Writing a guessed
#: integer into a guessed property name could therefore delete 40,000 trees from
#: the render while the report says "switched to a light viewport mode".
#:
#: Until the on-box `getPropNames` dump exists, this stage DISCOVERS and REPORTS
#: and changes nothing. Populate from `settings.json` once a mapping is proven.
VERIFIED_SCATTER_DISPLAY: dict[str, tuple[str, int]] = {}


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

    #: Which property carries the filename. Setting the wrong one silently
    #: does nothing — VRayBitmap ignores `.filename` entirely.
    _FILE_PROPERTY = {
        "Bitmaptexture": "filename",
        "VRayBitmap": "HDRIMapName",
        "VRayHDRI": "HDRIMapName",
        "CoronaBitmap": "filename",
    }

    def downscale_texture(self, handle: int, floor_px: int, out_dir: str) -> str:
        """Write a reduced copy and relink the loader to it.

        The original is NEVER modified or deleted. The reduced copy goes to a
        separate folder, so full-resolution art is always one relink away — this
        stage is the only one that changes the render, and it must not also be
        the one that loses the source material.

        Gamma is pinned to 1.0 on both open and save. Letting Max apply its
        default would re-gamma the image on every pass, so a texture reduced
        twice would drift in colour.
        """
        loader = rt.getAnimByHandle(handle)
        if loader is None:
            raise RuntimeError(f"texture loader {handle} no longer exists")

        class_name = str(rt.classOf(loader))
        prop = self._FILE_PROPERTY.get(class_name)
        if prop is None:
            raise RuntimeError(
                f"{class_name} has no known filename property; refusing to guess"
            )

        source = str(getattr(loader, prop, "") or "")
        if not source or not os.path.isfile(source):
            raise RuntimeError(f"source texture is not on disk: {source!r}")

        target_dir = self._resolve_out_dir(out_dir or "textures_reduced")
        os.makedirs(target_dir, exist_ok=True)
        stem, extension = ntpath.splitext(ntpath.basename(source))
        destination = ntpath.join(target_dir, f"{stem}_{floor_px}{extension}")

        original = None
        reduced = None
        try:
            original = rt.openBitMap(source, gamma=1.0)
            width, height = int(original.width), int(original.height)
            longest = max(width, height)
            if longest <= floor_px:
                raise RuntimeError(
                    f"{source} is {longest}px, at or under the {floor_px}px floor"
                )
            ratio = floor_px / float(longest)
            new_width = max(1, int(round(width * ratio)))
            new_height = max(1, int(round(height * ratio)))

            reduced = rt.bitmap(new_width, new_height, filename=destination, gamma=1.0)
            rt.copy(original, reduced)
            rt.save(reduced)
        finally:
            for bitmap in (reduced, original):
                if bitmap is not None:
                    try:
                        rt.close(bitmap)
                    except Exception:
                        pass
            try:
                rt.freeSceneBitmaps()
            except Exception:
                pass

        # Nothing is relinked until the replacement verifiably exists. A relink
        # to a file that was not written turns a memory problem into a missing
        # texture, which is strictly worse.
        if not os.path.isfile(destination) or os.path.getsize(destination) <= 0:
            raise RuntimeError(
                f"the reduced copy was not written to {destination}; the original "
                "is untouched and still linked"
            )

        setattr(loader, prop, destination)
        # Read back: assignment reporting success is not the same as it having
        # happened, and VRayBitmap is documented to ignore the wrong property.
        if str(getattr(loader, prop, "")) != destination:
            raise RuntimeError(
                f"relink of {class_name} did not take — the loader still points "
                f"at {source}"
            )
        return destination

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
        """Export one node to `.vrmesh` and replace it with a VRayProxy.

        `exportMultiple=True` is the documented **per-object** mode: the mesh
        file carries no transform and the proxy keeps the original's, pivot
        included. The single-file mode (`False`) bakes world transforms into the
        mesh and requires the proxy to sit at the origin — with
        `autoCreateProxies` also applying the original transform, the object
        ends up displaced by its own position.

        The created proxy is resolved to a real **node**. `getClassInstances`
        yields base objects, and a base object fails `isValidNode`, so using one
        as a node handle makes the mandatory bounding-box display silently fail
        — and the whole memory saving depends on that display mode.

        Once `autoCreateProxies` runs, the original is **already deleted**. Every
        message past that point says so, because headless undo does not restore
        and the backup is the only way back.
        """
        node = self._node(handle)
        name = str(node.name)
        before = self._bounds(node)

        target_dir = self._resolve_out_dir(out_dir)
        os.makedirs(target_dir, exist_ok=True)
        path = self._unique_path(target_dir, name)

        rt.select(node)
        created = rt.vrayMeshExport(
            meshFile=path,
            autoCreateProxies=True,
            # Per-object: the mesh carries no transform and the proxy keeps the
            # original's, pivot included.
            exportMultiple=True,
            # Sub-materials reassigned to the right faces.
            createMultiMtl=True,
            # NEVER condense: it renumbers material IDs, and a Multi/Sub whose
            # IDs have moved renders the wrong material on every face.
            condenseMultiMtl=False,
            # FALSE keeps V-Ray's chunked voxelisation, so it streams only the
            # chunks a bucket needs. "Optimize for instancing" collapses the mesh
            # to a single voxel, which loads the whole thing at render time —
            # the opposite of what a memory tool wants. Purely a loading
            # strategy; it does not change the geometry either way.
            oneVoxelPerMesh=False,
            # Viewport preview only. The renderer always streams the full mesh.
            maxPreviewFaces=1000,
            # A point-cloud level would render simplified geometry.
            exportPointClouds=False,
            animation=False,
        )

        if not os.path.exists(path):
            # Nothing was created, so nothing was replaced — the original stands.
            raise RuntimeError(
                f"vrayMeshExport produced no file for {name}; the original is untouched"
            )

        proxy = self._resolve_created_node(created, path)
        if proxy is None:
            raise RuntimeError(
                f"exported {name} but could not identify the created proxy node. "
                "autoCreateProxies has ALREADY replaced the original, so the "
                "scene has changed — recover from the backup, not from undo."
            )

        # Bounding box before and after. This is what catches a transform
        # applied twice, which no handle check would ever notice.
        after = self._bounds(proxy)
        if not self._bounds_close(before, after):
            raise RuntimeError(
                f"{name}: the proxy sits at {after} but the original occupied "
                f"{before} — the transform did not survive the export. The "
                "original is already gone; recover from the backup."
            )

        # Every property that reaches the renderer must be at its neutral
        # value. The bounding-box display below is viewport-only; these are not.
        drifted = self._render_affecting_drift(proxy)
        if drifted:
            raise RuntimeError(
                f"{name}: the created proxy has render-affecting properties away "
                f"from their neutral values ({drifted}), so it would not render "
                "identically. The original is already gone; recover from the backup."
            )

        # Bounding box display. VIEWPORT ONLY — the renderer streams the full
        # mesh from disk regardless. Anything heavier keeps a preview mesh in
        # RAM, and a modifier here reverts the proxy to a regular mesh entirely.
        if not self._set_display(proxy, PROXY_DISPLAY_BOUNDING_BOX):
            raise RuntimeError(
                f"{name}: converted, but the proxy display mode could not be set "
                "to bounding box, so the conversion saves nothing. The original "
                "is already gone; recover from the backup."
            )

        try:
            return int(rt.getHandleByAnim(proxy))
        except Exception:
            return 0

    def set_proxy_display(self, handle: int, mode: int = PROXY_DISPLAY_BOUNDING_BOX) -> None:
        """Kept for the port contract. `convert_to_proxy` already sets and
        verifies the display, because a silent failure there costs the entire
        saving the conversion existed for."""
        try:
            self._set_display(self._node(handle), mode)
        except Exception as exc:
            self.notes.append(f"could not set proxy display on {handle}: {exc}")

    def _render_affecting_drift(self, proxy) -> list[str]:
        """Render-affecting proxy properties that are NOT at their neutral value.

        Only properties this build actually exposes are checked — the names are
        unverified in the research, and asserting on a name that does not exist
        would fail every conversion. What was checked is recorded either way, so
        a build that exposes none of them is visible rather than silently
        assumed clean.
        """
        available = self._properties(proxy)
        drifted: list[str] = []
        checked: list[str] = []

        for prop, neutral in RENDER_AFFECTING_PROXY_DEFAULTS.items():
            if prop.lower() not in available:
                continue
            checked.append(prop)
            try:
                value = getattr(proxy, prop)
            except Exception:
                drifted.append(f"{prop}=<unreadable>")
                continue
            try:
                if isinstance(neutral, bool):
                    if bool(value) != neutral:
                        drifted.append(f"{prop}={value!r}")
                elif abs(float(value) - float(neutral)) > 1e-6:
                    drifted.append(f"{prop}={value!r}")
            except (TypeError, ValueError):
                drifted.append(f"{prop}={value!r}")

        if not checked:
            self.notes.append(
                "no render-affecting proxy properties were exposed by this build "
                "— point cloud, LOD scale and map-channel settings could NOT be "
                "confirmed neutral"
            )
        return drifted

    @staticmethod
    def _set_display(node, mode: int) -> bool:
        try:
            node.display = mode
            return int(node.display) == int(mode)
        except Exception:
            return False

    @staticmethod
    def _bounds(node):
        """World-space bounding box as two triples, or None if unreadable."""
        try:
            low, high = node.min, node.max
            return (
                (float(low.x), float(low.y), float(low.z)),
                (float(high.x), float(high.y), float(high.z)),
            )
        except Exception:
            return None

    @staticmethod
    def _bounds_close(before, after, rel_tol: float = 0.02, abs_floor: float = 0.1) -> bool:
        """Whether two bounding boxes describe the same object in the same place.

        Unreadable bounds are treated as a pass: refusing every node whose box
        cannot be read would disable the largest memory lever over a diagnostic
        that is not itself load-bearing.
        """
        if before is None or after is None:
            return True
        for low, high in zip(before, after):
            for a, b in zip(low, high):
                if abs(a - b) > max(abs_floor, rel_tol * max(abs(a), abs(b))):
                    return False
        return True

    def _resolve_created_node(self, created, path: str):
        """The created proxy as a NODE, never a base object."""
        candidates = []
        try:
            candidates.extend(list(created or []))
        except TypeError:
            # The documented return array is not iterable on some builds.
            pass

        for candidate in candidates:
            if self._is_node(candidate):
                return candidate

        found = self._find_proxy_by_file(path)
        return found

    @staticmethod
    def _is_node(candidate) -> bool:
        try:
            return candidate is not None and bool(rt.isValidNode(candidate))
        except Exception:
            return False

    @staticmethod
    def _find_proxy_by_file(path: str):
        """Recover the created proxy by its mesh file.

        `getClassInstances` returns base objects, so each match is walked back
        to its node with `refs.dependentNodes`. Only safe because
        `_unique_path` guarantees the filename is fresh; more than one match
        means refuse rather than guess.
        """
        matches = []
        try:
            cls = getattr(rt, "VRayProxy", None)
            if cls is None:
                return None
            for base in rt.getClassInstances(cls):
                try:
                    if str(base.fileName).lower() != path.lower():
                        continue
                except Exception:
                    continue
                try:
                    for node in rt.refs.dependentNodes(base) or []:
                        if rt.isValidNode(node):
                            matches.append(node)
                except Exception:
                    continue
        except Exception:
            return None
        return matches[0] if len(matches) == 1 else None

    # -- viewport-only levers ---------------------------------------------

    def set_bitmap_proxy_mode(self, mode: str = BITMAP_MODE_SAFE) -> bool:
        """True only if the setting actually took.

        Returning None regardless meant a headless session with no
        `BitmapProxyMgr` reported this lever as applied, and memory freed
        elsewhere was then credited to it.
        """
        if "UseProxies" in mode:
            raise ValueError(
                "renderMode_UseProxies renders the downscaled image and changes "
                "the result; it must never be set automatically"
            )
        manager = getattr(rt, "BitmapProxyMgr", None)
        if manager is None:
            self.notes.append(
                "BitmapProxyMgr is not available on this build — bitmap paging "
                "did NOT happen"
            )
            return False
        try:
            manager.globalProxyEnable = True
            manager.globalProxyRenderMode = rt.name(mode)
            return True
        except Exception as exc:
            self.notes.append(f"bitmap paging could not be set: {exc}")
            return False

    def set_nitrous_texture_limit(self, pixels: int) -> bool:
        manager = getattr(rt, "NitrousGraphicsManager", None)
        if manager is None:
            self.notes.append(
                "NitrousGraphicsManager is not available on this build — the "
                "viewport texture cap did NOT happen"
            )
            return False
        applied = False
        for setter, argument in (
            ("SetTextureSizeLimit", (pixels, True)),
            ("SetBackgroundTextureSizeLimit", (pixels, True)),
            ("SetProceduralTextureSizeLimit", (pixels,)),
        ):
            try:
                getattr(manager, setter)(*argument)
                applied = True
            except Exception as exc:
                self.notes.append(f"{setter} failed: {exc}")
        try:
            manager.ForceDisableMipMapGeneration(True)
        except Exception:
            pass
        return applied

    def set_scatter_display(self, handle: int, mode: str = "points") -> bool:
        """Switch a scatter object to a cheap viewport representation.

        Returns False and records what it saw unless the class has a **verified**
        (property, value) pair in :data:`VERIFIED_SCATTER_DISPLAY`.

        Forest Pack, RailClone and tyFlow each expose a viewport display group,
        a render display group, and a "Disable Object" that removes the object
        from the render. Their property names are undocumented and their enum
        values more so. Guessing one that happens to exist but means something
        else would silently change the render — in a stage declared
        render-identical — which is worse than doing nothing.

        So this reports the property names it found, and the box session turns
        that into a mapping.
        """
        try:
            node = self._node(handle)
        except Exception:
            return False

        class_name = ""
        try:
            class_name = str(rt.classOf(node))
        except Exception:
            pass

        verified = VERIFIED_SCATTER_DISPLAY.get(class_name)
        available = self._properties(node)

        if verified is None:
            self.notes.append(
                f"scatter {getattr(node, 'name', handle)} ({class_name or '?'}): "
                "no VERIFIED viewport-display mapping, so nothing was changed. "
                f"Properties present: {sorted(available)[:20]}"
            )
            return False

        prop, value = verified
        if prop.lower() not in available:
            self.notes.append(
                f"scatter {class_name}: verified property {prop!r} is absent on "
                f"this build — nothing changed. Properties present: {sorted(available)[:20]}"
            )
            return False

        try:
            setattr(node, prop, value)
            # Read back: a write that reported success but did not take would
            # otherwise be reported as an applied optimisation.
            return int(getattr(node, prop)) == int(value)
        except Exception as exc:
            self.notes.append(f"{class_name}: setting {prop} failed ({exc})")
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

    def convert_bitmaps_to_vray(self, tiled: bool = False) -> int:
        """NOT render-identical. Only ever called behind the diff gate.

        VRayBitmap uses a sharper filtering kernel than Max's Bitmap, so the
        image legitimately changes. Blur is reset to 1.0 to remove the one
        difference that is a pure artefact.

        **UV/tiling and output state are carried across.** Copying only the
        filename turns a floor with `U_Tiling = 12` into a single stretched
        tile — reported as "applied". A texture whose `coords` cannot be carried
        is left alone rather than converted wrongly.

        `tiled` does nothing yet: generating tiled `.tx` files (the lever that
        actually reduces render-time texture RAM) is not implemented, and the
        op title no longer claims it.
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

                # Mapping channel, tiling, offset, angle. Without this a tiled
                # material becomes one stretched tile.
                carried = True
                for attribute in ("coords", "output", "alphaSource"):
                    source = getattr(texture, attribute, None)
                    if source is None:
                        continue
                    try:
                        setattr(replacement, attribute, source)
                    except Exception:
                        if attribute == "coords":
                            carried = False
                if not carried:
                    self.notes.append(
                        f"{texture.name}: UV/tiling state could not be carried "
                        "over — left as a Max Bitmap rather than converted wrongly"
                    )
                    continue

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
