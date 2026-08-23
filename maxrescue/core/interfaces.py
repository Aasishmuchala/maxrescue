"""The ports. Everything Max-shaped enters through here and nowhere else.

Methods carry safe default implementations rather than being abstract, so
adding a capability never breaks an existing fake. That is deliberate: in the
sibling projects, an abstract method added late silently broke every test double
at once and the resulting churn hid a real bug.

Notes attached to individual ports record behaviour that cost a session to learn
and would otherwise be re-learned.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from maxrescue.core.types import (
    Assignment,
    Engine,
    MemorySample,
    NodeFacts,
    SceneStats,
    TextureFacts,
)

__all__ = [
    "BackupService",
    "FixServices",
    "GuardServices",
    "MemoryProbe",
    "MergeServices",
    "RenderServices",
    "SceneContext",
    "SceneQuery",
    "UndoService",
    "UndoTransaction",
]


class UndoTransaction(Protocol):
    def cancel(self) -> None: ...

    @property
    def cancelled(self) -> bool: ...


class UndoService(Protocol):
    """Wraps `theHold`, not `pymxs.undo`.

    `pymxs.undo` swallows exceptions; `theHold` is strict LIFO and will re-raise,
    which is what makes a failed op roll back instead of half-applying.

    In batch mode Max's undo does **not** restore — it is GUI-only. The backup
    file is the real safety net, and this exists to keep in-session state sane.
    """

    def transaction(self, label: str): ...


class BackupService(Protocol):
    """A backup before any mutation. A failed backup aborts the run.

    The saved file must not become the open document — `useNewFile:false`.
    """

    def create(self) -> str: ...


class MemoryProbe(Protocol):
    """Process memory, with provenance.

    Prefers psutil, falls back to Max's own `sysinfo.getMAXMemoryInfo()`, then
    to `GetProcessMemoryInfo` via ctypes. Whichever answered is recorded on the
    sample, because three sources that disagree is itself a finding.
    """

    def sample(self) -> MemorySample: ...

    def settle(self) -> None:
        """Release what Max is going to release, so a delta means something."""


class SceneQuery(Protocol):
    """Read-only view of the open scene."""

    def stats(self) -> SceneStats: ...

    def nodes(self) -> Sequence[NodeFacts]:
        """All nodes, read in bulk.

        One crossing of the MAXScript boundary returning a whole array, never
        per-node attribute access — the latter is ~10x slower and produced a
        nine-minute stall on a 2000-object scene.
        """
        return ()

    def textures(self) -> Sequence[TextureFacts]:
        """Every bitmap the scene loads, with its real pixel dimensions.

        Dimensions come from the file on disk, not from the loader — a loader
        reports what it was told, and the file is what costs memory.
        """
        return ()

    def texture_facts(self, handle: int) -> TextureFacts | None:
        """One texture, looked up directly.

        Targeted for the same reason as `modifier_count`: verifying by re-reading
        `textures()` would cost a full bitmap sweep per operation.
        """
        return None

    def node_exists(self, handle: int) -> bool:
        return False

    def modifier_count(self, handle: int) -> int | None:
        """How many modifiers one node carries, or None if it cannot be read.

        Deliberately a single-node query. Verifying a collapse by re-reading
        `nodes()` costs a full scene crossing PER OPERATION, which turns a scene
        with thousands of collapse targets into thousands of full scene reads —
        the same O(n^2) shape that produced a nine-minute stall in a sibling
        project.

        `None` means "could not tell", which must never be read as success.
        """
        return None

    def material_in_use(self, handle: int) -> bool:
        """Usage, never library membership.

        `sceneMaterials` still contains a material after it is unassigned, and a
        purged material stays alive — so membership is not liveness, and a
        verify built on it falsely halts good runs.
        """
        return False

    def engine(self) -> Engine:
        return Engine.UNKNOWN

    def render_hidden(self) -> bool:
        """The scene's `rendHidden` flag.

        The premise that a hidden object is free to delete rests entirely on
        this being off. Defaults to True here so a bridge that forgets to
        implement it produces a conservative plan rather than a destructive one.
        """
        return True

    def saved_file_mb(self) -> float:
        """Weight via one temporary save. Expensive; called once per run, never
        in a loop."""
        return 0.0

    def scene_path(self) -> str:
        return ""


class FixServices(Protocol):
    """Mutations. Every one is verified by the caller immediately after."""

    # -- hygiene
    def free_scene_bitmaps(self) -> None: ...
    def clear_undo_buffer(self) -> None: ...
    def collect_garbage(self) -> None: ...
    def strip_notetracks(self, handle: int) -> int:
        return 0

    def strip_scene_state(self, name: str) -> None: ...
    def strip_motion_mixer(self) -> int:
        return 0

    def purge_unused_materials(self) -> int:
        return 0

    def remove_suspicious_callback(self, signature: str) -> None: ...

    # -- nodes
    def delete_node(self, handle: int) -> None: ...

    # -- stacks
    def downscale_texture(self, handle: int, floor_px: int, out_dir: str) -> str:
        """Write a reduced copy and relink the loader to it.

        Returns the new path. The original is NEVER modified or deleted — the
        reduced copy goes to a separate folder, so the full-resolution art is
        always one relink away.
        """
        return ""

    def collapse_stack(self, handle: int) -> None:
        """Collapse to the top of the stack, preserving instancing.

        `maxOps.CollapseNodeTo node node.modifiers.count off` — the trailing
        `off` is what keeps instances instanced.
        """

    # -- proxies
    def convert_to_proxy(self, handle: int, out_dir: str) -> int:
        """Export one node to `.vrmesh` and replace it with a VRayProxy.

        A failed export must never reach a delete. V-Ray bakes the mesh in world
        space, so the proxy node sits at the origin and the original transform
        has to be restored explicitly.
        """
        return 0

    def set_proxy_display(self, handle: int, mode: int) -> None:
        """0 = bounding box. Anything heavier defeats the point.

        Never leave a modifier on a proxy — it reverts to a regular mesh and
        loses every saving.
        """

    # -- viewport-only levers (render-identical by construction)
    def set_bitmap_proxy_mode(self, mode: str) -> None:
        """`#renderMode_UseFullRes_FlushFromMemory` only.

        `#renderMode_UseProxies` renders the downscaled image and **changes the
        render**. It must never be set by an automatic stage.
        """

    def set_nitrous_texture_limit(self, pixels: int) -> None: ...

    def set_scatter_display(self, handle: int, mode: str) -> bool:
        """Viewport display group only — never the render group, never
        'Disable Object', both of which change the render."""
        return False

    def set_show_in_viewport(self, material_handle: int, show: bool) -> None: ...

    # -- opt-in, gated by an image diff
    def convert_bitmaps_to_vray(self, tiled: bool = True) -> int:
        return 0


class MergeServices(Protocol):
    """Reading a scene in pieces without opening it whole."""

    def object_names(self, path: str) -> tuple[list[str], list[str]]:
        """`(names, missing_dlls)` without loading the scene."""
        return ([], [])

    def merge(self, path: str, names: Sequence[str]) -> tuple[list[str], list[str]]:
        """Merge the named objects into the open scene.

        Must pass explicit duplicate / material / reparent policies. The
        interactive defaults are `#promptMtlDups` and `#promptReparent`, which
        will hang a batch run behind a dialog nobody can click.

        Returns `(merged_names, problems)`.
        """
        return ([], [])

    def reset_scene(self) -> None: ...

    def save_as(self, path: str) -> bool:
        return False


class GuardServices(Protocol):
    """The material guard: snapshot, diff, re-attach.

    Pins live references to every material at snapshot time. That refcount is
    what keeps a material alive through a purge so it can be re-attached — a
    handle alone would be pointing at freed memory.
    """

    def snapshot(self) -> tuple[Assignment, ...]:
        return ()

    def current(self) -> tuple[Assignment, ...]:
        return ()

    def reattach(self, node_handle: int, material_handle: int) -> bool:
        return False

    def release(self) -> None:
        """Drop the pins. Must run in a `finally` so a halt cannot leak them."""


class RenderServices(Protocol):
    """Rendering for the identity gate.

    Determinism has to be forced: locked noise pattern, fixed seed, bucket
    sampler, light cache loaded from file, denoiser off. Without those, two
    renders of an unmodified scene differ and the gate is worthless.
    """

    def prepare_deterministic(self, light_cache_file: str | None = None) -> dict:
        return {}

    def render_to(self, path: str) -> bool:
        return False

    def restore(self, saved: dict) -> None: ...


class SceneContext(Protocol):
    """Everything a run needs, wired together."""

    query: SceneQuery
    fixes: FixServices
    merge: MergeServices
    guard: GuardServices
    undo: UndoService
    backup: BackupService
    memory: MemoryProbe
