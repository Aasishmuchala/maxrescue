"""Test doubles that model 3ds Max as it actually behaves.

A fake that always succeeds hides exactly the bug the real bridge will hit. So
these reproduce Max's awkward realities, each of which cost a sibling project a
session to discover:

* purging a material that is still referenced puts it straight back;
* converting a node to a proxy DELETES the original and creates a new node with
  a different handle;
* an undo transaction restores on an exception, not just on an explicit cancel;
* merging by name can return fewer objects than were asked for;
* freeing memory is not instantaneous — RSS only moves after a settle.

Fault-injection flags make the failure paths reachable, because the failure
paths are the ones that matter.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

from maxrescue.core.types import (
    Assignment,
    Engine,
    MemorySample,
    NodeFacts,
    SceneStats,
)

PROXY_PREVIEW_FACES = 200
PROXY_RSS_SAVING_PER_FACE = 0.00002  # MB — a plausible, deliberately modest model


# ---------------------------------------------------------------------------
# the scene
# ---------------------------------------------------------------------------


@dataclass
class FakeScene:
    nodes: dict[int, NodeFacts] = field(default_factory=dict)
    materials: dict[int, str] = field(default_factory=dict)
    assignments: dict[int, int | None] = field(default_factory=dict)
    notetracks: dict[int, int] = field(default_factory=dict)
    scene_states: list[str] = field(default_factory=list)
    mixer_clips: int = 0
    rss_mb: float = 8000.0
    vram_mb: float = 2000.0
    render_hidden: bool = False
    engine: Engine = Engine.VRAY
    path: str = r"C:\jobs\villa\villa.max"

    # fault injection
    fail_proxy_for: set[int] = field(default_factory=set)
    fail_collapse_for: set[int] = field(default_factory=set)
    keep_original_on_proxy: set[int] = field(default_factory=set)
    drop_material_on_proxy: set[int] = field(default_factory=set)
    backup_fails: bool = False
    merge_shortfall: int = 0

    _next_handle: int = 10_000

    def add(self, node: NodeFacts, material: int | None = None) -> NodeFacts:
        self.nodes[node.handle] = node
        self.assignments[node.handle] = material
        return node

    def new_handle(self) -> int:
        self._next_handle += 1
        return self._next_handle

    @property
    def used_materials(self) -> set[int]:
        return {m for m in self.assignments.values() if m is not None}


def archviz_scene() -> FakeScene:
    """A scene with one of everything the guards care about."""
    scene = FakeScene()
    scene.materials = {1: "Concrete", 2: "Glass", 3: "Unused_A", 4: "Unused_B"}

    scene.add(
        NodeFacts(
            handle=1,
            name="Villa_Hero",
            class_name="Editable_Poly",
            super_class="GeomObject",
            faces=800_000,
            modifier_classes=("Bend",),
        ),
        material=1,
    )
    for i in range(4):
        scene.add(
            NodeFacts(
                handle=10 + i,
                name=f"Tree_{i:02d}",
                class_name="Editable_Poly",
                super_class="GeomObject",
                faces=150_000,
                base_handle=555,
            ),
            material=2,
        )
    scene.add(
        NodeFacts(
            handle=20,
            name="Forest_Scatter",
            class_name="Forest_Pro",
            super_class="GeomObject",
            faces=2_000_000,
        ),
        material=2,
    )
    scene.add(
        NodeFacts(
            handle=30,
            name="Old_Draft",
            class_name="Editable_Poly",
            super_class="GeomObject",
            faces=90_000,
            is_hidden=True,
        ),
        material=1,
    )
    scene.add(
        NodeFacts(
            handle=31,
            name="Glow_Card",
            class_name="Editable_Poly",
            super_class="GeomObject",
            faces=12,
            is_hidden=True,
            primary_visibility=False,
        ),
        material=2,
    )
    scene.add(
        NodeFacts(
            handle=40,
            name="Rigged_Figure",
            class_name="Editable_Poly",
            super_class="GeomObject",
            faces=300_000,
            has_skin=True,
            modifier_classes=("Skin",),
        ),
        material=1,
    )
    scene.notetracks = {1: 3}
    scene.scene_states = ["draft", "final"]
    scene.mixer_clips = 2
    return scene


# ---------------------------------------------------------------------------
# ports
# ---------------------------------------------------------------------------


class FakeMemoryProbe:
    def __init__(self, scene: FakeScene):
        self.scene = scene
        self.settles = 0

    def sample(self) -> MemorySample:
        return MemorySample(
            rss_mb=round(self.scene.rss_mb, 1),
            peak_rss_mb=round(self.scene.rss_mb, 1),
            vram_mb=self.scene.vram_mb,
            source="fake",
        )

    def settle(self) -> None:
        self.settles += 1


class FakeSceneQuery:
    def __init__(self, scene: FakeScene, memory: FakeMemoryProbe):
        self.scene = scene
        self._memory = memory

    def stats(self) -> SceneStats:
        return SceneStats(
            polygons=sum(n.faces for n in self.scene.nodes.values()),
            nodes=len(self.scene.nodes),
            materials=len(self.scene.materials),
            file_mb=round(sum(n.faces for n in self.scene.nodes.values()) / 5000, 1),
            memory=self._memory.sample(),
        )

    def nodes(self):
        return tuple(self.scene.nodes.values())

    def node_exists(self, handle: int) -> bool:
        return handle in self.scene.nodes

    def modifier_count(self, handle: int) -> int | None:
        node = self.scene.nodes.get(handle)
        return None if node is None else len(node.modifier_classes)

    def material_in_use(self, handle: int) -> bool:
        return handle in self.scene.used_materials

    def engine(self) -> Engine:
        return self.scene.engine

    def render_hidden(self) -> bool:
        return self.scene.render_hidden

    def saved_file_mb(self) -> float:
        return self.stats().file_mb

    def scene_path(self) -> str:
        return self.scene.path


class FakeFixServices:
    def __init__(self, scene: FakeScene):
        self.scene = scene
        self.calls: list[str] = []
        self.bitmap_mode: str | None = None
        self.nitrous_limit: int | None = None
        self.notes: list[str] = []
        self.scatter_display: dict[int, str] = {}

    # -- hygiene
    def free_scene_bitmaps(self) -> None:
        self.calls.append("free_scene_bitmaps")
        self.scene.rss_mb -= 300

    def clear_undo_buffer(self) -> None:
        self.calls.append("clear_undo_buffer")
        self.scene.rss_mb -= 50

    def collect_garbage(self) -> None:
        self.calls.append("collect_garbage")
        self.scene.rss_mb -= 20

    def strip_notetracks(self, handle: int) -> int:
        count = self.scene.notetracks.pop(handle, 0)
        return count

    def strip_scene_state(self, name: str) -> None:
        if name in self.scene.scene_states:
            self.scene.scene_states.remove(name)

    def strip_motion_mixer(self) -> int:
        removed, self.scene.mixer_clips = self.scene.mixer_clips, 0
        return removed

    def purge_unused_materials(self) -> int:
        """Max re-registers a material that is still referenced.

        A purge that reports removing everything it tried is lying; only the
        genuinely unreferenced ones go.
        """
        unused = [h for h in self.scene.materials if h not in self.scene.used_materials]
        for handle in unused:
            self.scene.materials.pop(handle)
        return len(unused)

    def remove_suspicious_callback(self, signature: str) -> None:
        self.calls.append(f"remove_callback:{signature}")

    # -- nodes
    def delete_node(self, handle: int) -> None:
        node = self.scene.nodes.pop(handle, None)
        self.scene.assignments.pop(handle, None)
        if node:
            self.scene.rss_mb -= node.faces * PROXY_RSS_SAVING_PER_FACE * 2

    # -- stacks
    def collapse_stack(self, handle: int) -> None:
        if handle in self.scene.fail_collapse_for:
            raise RuntimeError(f"collapse failed for {handle}")
        node = self.scene.nodes[handle]
        depth = len(node.modifier_classes)
        self.scene.nodes[handle] = replace(node, modifier_classes=())
        self.scene.rss_mb -= node.faces * PROXY_RSS_SAVING_PER_FACE * depth

    # -- proxies
    def convert_to_proxy(self, handle: int, out_dir: str) -> int:
        if handle in self.scene.fail_proxy_for:
            raise RuntimeError(f"vrayMeshExport failed for {handle}")
        node = self.scene.nodes[handle]
        material = self.scene.assignments.get(handle)

        new_handle = self.scene.new_handle()
        self.scene.nodes[new_handle] = replace(
            node,
            handle=new_handle,
            class_name="VRayProxy",
            faces=PROXY_PREVIEW_FACES,
            modifier_classes=(),
        )
        # The material follows the node — unless we are injecting the failure
        # the guard exists to catch.
        self.scene.assignments[new_handle] = (
            None if handle in self.scene.drop_material_on_proxy else material
        )
        if handle not in self.scene.keep_original_on_proxy:
            self.scene.nodes.pop(handle)
            self.scene.assignments.pop(handle, None)
        self.scene.rss_mb -= node.faces * PROXY_RSS_SAVING_PER_FACE
        return new_handle

    def set_proxy_display(self, handle: int, mode: int) -> None:
        self.calls.append(f"proxy_display:{handle}:{mode}")

    # -- viewport
    #: Set to model a build where these managers simply are not present.
    bitmap_manager_missing = False
    nitrous_missing = False

    def set_bitmap_proxy_mode(self, mode: str) -> bool:
        assert "UseProxies" not in mode, "that mode changes the render"
        if self.bitmap_manager_missing:
            self.notes.append("BitmapProxyMgr unavailable")
            return False
        self.bitmap_mode = mode
        self.scene.rss_mb -= 1200
        return True

    def set_nitrous_texture_limit(self, pixels: int) -> bool:
        if self.nitrous_missing:
            self.notes.append("NitrousGraphicsManager unavailable")
            return False
        self.nitrous_limit = pixels
        self.scene.vram_mb -= 400
        return True

    def set_scatter_display(self, handle: int, mode: str) -> bool:
        if handle not in self.scene.nodes:
            return False
        self.scatter_display[handle] = mode
        self.scene.vram_mb -= 200
        return True

    def set_show_in_viewport(self, material_handle: int, show: bool) -> None:
        self.calls.append(f"show_in_viewport:{material_handle}:{show}")

    def convert_bitmaps_to_vray(self, tiled: bool = True) -> int:
        self.calls.append(f"convert_bitmaps:{tiled}")
        self.scene.rss_mb -= 4000
        return 12


class FakeMergeServices:
    def __init__(self, scene: FakeScene, library: dict[str, NodeFacts] | None = None):
        self.scene = scene
        self.library = library or {}
        self.merged_calls: list[tuple[str, tuple[str, ...]]] = []
        self.resets = 0
        self.saved_to: str | None = None

    def object_names(self, path: str) -> tuple[list[str], list[str]]:
        return (list(self.library), [])

    def merge(self, path: str, names):
        names = tuple(names)
        self.merged_calls.append((path, names))
        wanted = list(names)
        if self.scene.merge_shortfall:
            wanted = wanted[: max(0, len(wanted) - self.scene.merge_shortfall)]
        merged: list[str] = []
        for name in wanted:
            node = self.library.get(name)
            if node is None:
                continue
            self.scene.add(node, material=1)
            self.scene.rss_mb += node.faces * PROXY_RSS_SAVING_PER_FACE * 3
            merged.append(name)
        problems = [f"{n}: not found" for n in names if n not in merged]
        return merged, problems

    def reset_scene(self) -> None:
        self.resets += 1
        self.scene.nodes.clear()
        self.scene.assignments.clear()
        self.scene.rss_mb = 3000.0

    def save_as(self, path: str) -> bool:
        self.saved_to = path
        return True


class FakeGuardServices:
    def __init__(self, scene: FakeScene):
        self.scene = scene
        self.pinned = False
        self.reattached: list[tuple[int, int]] = []

    def snapshot(self):
        self.pinned = True
        return tuple(
            Assignment(node_handle=h, material_handle=m)
            for h, m in sorted(self.scene.assignments.items())
        )

    def current(self):
        return tuple(
            Assignment(node_handle=h, material_handle=m)
            for h, m in sorted(self.scene.assignments.items())
        )

    def reattach(self, node_handle: int, material_handle: int) -> bool:
        if node_handle not in self.scene.nodes:
            return False
        self.scene.assignments[node_handle] = material_handle
        # Re-attaching re-registers the material in the library, exactly as the
        # live refcount does.
        self.scene.materials.setdefault(material_handle, f"restored_{material_handle}")
        self.reattached.append((node_handle, material_handle))
        return True

    def release(self) -> None:
        self.pinned = False


class FakeUndoTransaction:
    def __init__(self):
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeUndoService:
    """Restores on cancel AND on exception — the behaviour that makes a failed
    op roll back instead of half-applying."""

    def __init__(self, scene: FakeScene):
        self.scene = scene
        self.labels: list[str] = []

    @contextmanager
    def transaction(self, label: str):
        self.labels.append(label)
        snapshot = (
            copy.deepcopy(self.scene.nodes),
            copy.deepcopy(self.scene.assignments),
            copy.deepcopy(self.scene.materials),
            self.scene.rss_mb,
        )
        txn = FakeUndoTransaction()
        try:
            yield txn
        except Exception:
            (
                self.scene.nodes,
                self.scene.assignments,
                self.scene.materials,
                self.scene.rss_mb,
            ) = snapshot
            raise
        if txn.cancelled:
            (
                self.scene.nodes,
                self.scene.assignments,
                self.scene.materials,
                self.scene.rss_mb,
            ) = snapshot


class FakeBackupService:
    def __init__(self, scene: FakeScene):
        self.scene = scene
        self.created: list[str] = []

    def create(self) -> str:
        if self.scene.backup_fails:
            raise IOError("could not write backup")
        path = rf"C:\jobs\villa\maxrescue_backups\villa_backup_{len(self.created):03d}.max"
        self.created.append(path)
        return path


@dataclass
class FakeContext:
    query: FakeSceneQuery
    fixes: FakeFixServices
    merge: FakeMergeServices
    guard: FakeGuardServices
    undo: FakeUndoService
    backup: FakeBackupService
    memory: FakeMemoryProbe
    scene: FakeScene


def make_context(scene: FakeScene | None = None, library=None) -> FakeContext:
    scene = scene or archviz_scene()
    memory = FakeMemoryProbe(scene)
    return FakeContext(
        query=FakeSceneQuery(scene, memory),
        fixes=FakeFixServices(scene),
        merge=FakeMergeServices(scene, library),
        guard=FakeGuardServices(scene),
        undo=FakeUndoService(scene),
        backup=FakeBackupService(scene),
        memory=memory,
        scene=scene,
    )
