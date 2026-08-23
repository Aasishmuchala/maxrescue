"""The planner: decide every operation *before* anything is touched.

This module is where the "renders are bit-identical" contract is actually
enforced. Each guard returns a reason string, and a reason means the node is
excluded and the reason is logged. Nothing is skipped silently — a silent skip
reads as "covered" when it was not, which is how a tool starts lying about what
it did.

Pure. Testable without 3ds Max, which is the only reason this many guards can be
exercised at all.

The class lists and several guards are carried over from MaxSlim, where they
were paid for in production incidents. Each one has its incident noted.
"""

from __future__ import annotations

import ntpath
from dataclasses import dataclass

from maxrescue.core.types import Engine, NodeFacts, Op, Plan, Stage, TextureFacts

__all__ = [
    "PlanConfig",
    "build_plan",
    "collapse_reason",
    "hidden_reason",
    "proxy_reason",
    "texture_reason",
]

#: The smallest a texture may ever be reduced to.
#:
#: 4K is the agreed limit of what may be lost, so this is a floor the
#: configuration cannot go under — a settings file asking for 512 would quietly
#: destroy far more quality than was authorised, and nobody would see it happen.
#: An 8K map becomes 4K; a 4K map is left exactly as it is.
TEXTURE_FLOOR_FLOOR = 4096


@dataclass(frozen=True)
class PlanConfig:
    per_node_face_threshold: int = 30_000
    """Lower than MaxSlim's 100k: this tool is fighting for memory, not file
    size, and a 30k-face mesh still costs real RAM."""

    set_total_face_threshold: int = 100_000
    collapse_min_modifiers: int = 1
    proxy_out_dir: str = "proxies"
    delete_hidden: bool = True
    collapse_stacks: bool = True
    convert_bitmaps: bool = False
    """Opt-in. Bitmap→VRayBitmap is NOT render-identical by design."""

    nitrous_texture_limit: int = 512

    reduce_textures: bool = True
    texture_floor_px: int = TEXTURE_FLOOR_FLOOR
    """Longest edge a texture is reduced TO. Never honoured below
    :data:`TEXTURE_FLOOR_FLOOR`."""

    @property
    def effective_texture_floor(self) -> int:
        return max(TEXTURE_FLOOR_FLOOR, int(self.texture_floor_px))


# Classes that are already proxies — converting one again is a no-op that costs
# a file write and a node replacement.
_PROXY_CLASSES = frozenset(
    {"vrayproxy", "vraymesh", "coronaproxy", "vrayproxy2"}
)

# Scatter and simulation plugins. Their geometry is generated, not stored, so
# "converting" one destroys the system. Matched by lowercase prefix because
# vendors version their class names (Forest_Pro_3 -> Forest_Pro).
_NEVER_TOUCH_PREFIXES = (
    "forest",
    "railclone",
    "growfx",
    "multiscatter",
    "coronascatter",
    "vrayscatter",
    "phoenixfd",
    "tyflow",
    "tycache",
    "tysplinemesher",
    "pf_",
    "particle",
    "vrayplane",
    "vrayfur",
    "vrayinfinite",
    "vraysphere",
    "vrayenmesh",
    "vrayclipper",
)

# The only classes safe to bake into a proxy. Anything else may carry data the
# exporter does not round-trip.
_PROXYABLE_CLASSES = frozenset(
    {
        "editable_poly",
        "editable_mesh",
        "editable_patch",
        "polymeshobject",
        "trimeshgeometry",
        "editablepoly",
        "editablemesh",
    }
)

# Subdivision modifiers whose viewport iteration count also drives the render
# when `useRenderIterations` is off. Collapsing at viewport level changes the
# image; collapsing at render level explodes memory. Neither is wanted.
_RENDER_COUPLED = frozenset({"turbosmooth", "meshsmooth", "opensubdiv", "subdivide"})

# Modifiers whose evaluated viewport mesh IS what renders, so baking them into
# a proxy is faithful. An ALLOWLIST rather than a blocklist: a proxy captures
# the evaluated mesh, and any modifier not on this list may contribute at render
# time in a way the bake cannot capture (displacement, hair, render-time
# subdivision). Refusing an unknown modifier costs some memory; baking one
# wrongly costs the render.
_BAKEABLE_MODIFIERS = frozenset(
    {
        "bend", "twist", "taper", "stretch", "skew", "noise", "ripple", "wave",
        "shell", "symmetry", "mirror", "slice", "cap_holes", "caphole",
        "normalmodifier", "normal", "smooth", "edit_normals", "editnormals",
        "unwrap_uvw", "unwrapuvw", "uvwmap", "uvw_map", "uvwxform", "uvw_xform",
        "materialmodifier", "material", "vertexpaint", "vertex_paint",
        "editable_poly", "edit_poly", "editpoly", "edit_mesh", "editmesh",
        "meshselect", "mesh_select", "polyselect", "poly_select", "volselect",
        "vol_select", "facextrude", "face_extrude", "push", "relax", "xform",
        "affectregion", "affect_region", "deleteMesh", "delete_mesh",
        "optimize", "prooptimizer", "multires", "tessellate", "quadify_mesh",
        "quadifymesh", "turn_to_poly", "turntopoly", "turn_to_mesh", "turntomesh",
    }
)

# Stack entries that must never be baked away.
_UNSAFE_TO_COLLAPSE = frozenset(
    {
        "skin",
        "physique",
        "morpher",
        "cloth",
        "hair_and_fur",
        "hairfarm",
        "vraydisplacementmod",
        "displace",
        "flex",
        "pathdeform",
        "surfdeform",
    }
)


def _norm(text: str) -> str:
    return (text or "").strip().lower().replace(" ", "_")


# ---------------------------------------------------------------------------
# per-stage guards
# ---------------------------------------------------------------------------


def proxy_reason(node: NodeFacts, config: PlanConfig) -> str | None:
    """Why this node must not become a proxy, or None if it may."""
    cls = _norm(node.class_name)

    if _norm(node.super_class) != "geomobject":
        return "not geometry"
    if cls in _PROXY_CLASSES:
        return "already a proxy"
    if any(cls.startswith(p) for p in _NEVER_TOUCH_PREFIXES):
        return "scatter/simulation plugin — geometry is generated, never stored"
    if cls not in _PROXYABLE_CLASSES:
        return f"class '{node.class_name}' outside the safe-convert whitelist"
    if node.in_xref:
        return "inside an XRef — never touched"
    if node.scripted_controller:
        return "script-driven controller — baking would freeze its behaviour"
    if node.is_animated:
        return "animated (has keys) — a proxy is a single static mesh"
    if node.has_skin:
        return "skinned — the deformation lives in the stack"
    if node.is_bone:
        return "bone"
    if node.in_group or node.is_group_head:
        # Selecting one member of a closed group selects the whole group; a
        # production run turned 27 intended conversions into ~25k consumed nodes.
        return "in a group — selecting a member selects the whole group"
    if node.child_count:
        return "has linked children — converting would unlink them"
    if node.has_parent:
        return "linked to a parent — the proxy would not inherit the link"
    if node.negative_scale:
        # Chaos: mirrored meshes have local-space normals reversed and need an
        # xform reset before export. Not worth the risk automatically.
        return "mirrored / negative scale — normals do not survive the export"
    if node.render_differs_from_viewport:
        return (
            "renders differently from the viewport ("
            + ", ".join(node.render_differs_from_viewport)
            + ") — a proxy bakes the viewport mesh"
        )
    # A proxy captures the EVALUATED mesh. Any modifier that contributes at
    # render time rather than to that mesh would be silently discarded, so
    # anything not proven bakeable is refused.
    unknown = [
        m for m in node.modifier_classes if _norm(m) not in _BAKEABLE_MODIFIERS
    ]
    if unknown:
        return (
            "stack contains " + ", ".join(unknown) + " — not on the proven "
            "bakeable list, and a proxy would capture only the viewport mesh"
        )
    if node.faces < config.per_node_face_threshold:
        return f"only {node.faces:,} faces — below the {config.per_node_face_threshold:,} threshold"
    return None


def texture_reason(texture: TextureFacts, config: PlanConfig) -> str | None:
    """Why this texture must not be reduced, or None if it may be.

    The one stage that can change the render, so its refusals matter as much as
    its actions.
    """
    if not config.reduce_textures:
        return "texture reduction disabled"
    if not texture.path:
        return "no file path on this loader"
    if not texture.exists:
        # Relinking a texture that is not on disk would replace a broken
        # reference with a differently broken one, and lose the original path.
        return f"not on disk: {texture.path}"
    if texture.in_xref:
        return "inside an XRef — never touched"
    floor = config.effective_texture_floor
    if texture.longest_edge <= floor:
        return f"already {texture.longest_edge:,}px — at or under the {floor:,}px floor"
    return None


def hidden_reason(
    node: NodeFacts,
    config: PlanConfig,
    visible_bases: set[int],
    render_hidden: bool = False,
) -> str | None:
    """Why this hidden/non-renderable node must be kept, or None if it may go.

    `render_hidden` is the scene's own `rendHidden` flag. The whole premise that
    a hidden object is free to delete rests on it being **off** — which is the
    default, but not a guarantee. With it on, hidden objects render, and
    deleting one changes the image.
    """
    if not config.delete_hidden:
        return "hidden-object removal disabled"
    if render_hidden and node.renderable:
        return "scene has 'render hidden objects' enabled — this one renders"
    if not node.primary_visibility and node.renderable:
        # Invisible to camera but still contributing to GI and reflections.
        # Deleting it changes the image.
        return "invisible to camera but still lights the scene (GI/reflections)"
    if node.renderable and not node.is_hidden:
        return "renders normally"
    if node.in_xref:
        return "inside an XRef — never touched"
    if node.has_dependents:
        # Childless helpers are often LookAt/constraint targets.
        return "something else depends on it (constraint or controller target)"
    if node.child_count:
        return "has linked children"
    if node.in_group or node.is_group_head:
        return "in a group"
    if node.base_handle is not None and node.base_handle in visible_bases:
        # NOT because deleting it would take the geometry — Max reference
        # counting keeps a base object alive while any node still points at it.
        # The reason is narrower and still sufficient: this node is entangled
        # with visible ones, and an instance set is exactly where a plugin or a
        # group-select takes siblings with it. Deliberately over-conservative.
        return "shares its base object with a visible node — entangled with an instance set"
    if node.is_bone or node.has_skin:
        return "part of a rig"
    return None


def collapse_reason(node: NodeFacts, config: PlanConfig) -> str | None:
    """Why this node's stack must not be collapsed, or None if it may."""
    if not config.collapse_stacks:
        return "stack collapse disabled"
    if len(node.modifier_classes) < config.collapse_min_modifiers:
        return "no modifiers to collapse"
    if _norm(node.super_class) != "geomobject":
        return "not geometry"
    if node.in_xref:
        return "inside an XRef — never touched"
    if node.is_animated:
        return "animated — collapsing bakes one frame"
    if node.scripted_controller:
        return "script-driven controller"
    if node.in_group or node.is_group_head:
        # Same reasoning as the proxy guard: a closed group turns an operation
        # on one member into an operation on all of them.
        return "in a group — an operation on one member reaches the whole group"

    for modifier in node.modifier_classes:
        name = _norm(modifier)
        if name in _RENDER_COUPLED:
            return f"{modifier} iterations drive the render — collapsing changes it"
        if name in _UNSAFE_TO_COLLAPSE:
            return f"{modifier} carries deformation that cannot be baked safely"
        if any(name.startswith(p) for p in _NEVER_TOUCH_PREFIXES):
            return f"{modifier} belongs to a scatter/simulation system"
    return None


# ---------------------------------------------------------------------------
# stage planners
# ---------------------------------------------------------------------------


def _describe(node: NodeFacts, reason: str) -> str:
    return f"{node.name or f'<handle {node.handle}>'} ({node.faces:,} faces) — {reason}"


def plan_hygiene(nodes: list[NodeFacts], config: PlanConfig) -> tuple[list[Op], list[str]]:
    """Always safe, always first. Costs nothing and can only free memory."""
    ops = [
        Op(
            id="hygiene.bitmaps",
            stage=Stage.HYGIENE,
            title="release cached bitmaps",
        ),
        Op(
            id="hygiene.undo",
            stage=Stage.HYGIENE,
            title="clear the undo buffer",
        ),
        Op(
            id="hygiene.materials",
            stage=Stage.HYGIENE,
            title="purge unused materials",
        ),
        Op(
            id="hygiene.mixer",
            stage=Stage.HYGIENE,
            title="strip Motion Mixer data",
        ),
        Op(
            id="hygiene.gc",
            stage=Stage.HYGIENE,
            title="collect garbage",
        ),
    ]
    return ops, []


def plan_hidden(
    nodes: list[NodeFacts], config: PlanConfig, render_hidden: bool = False
) -> tuple[list[Op], list[str]]:
    if render_hidden:
        return [], [
            "hidden-object removal skipped entirely — the scene renders hidden "
            "objects (rendHidden is on), so none of them are free to delete"
        ]
    visible_bases = {
        n.base_handle
        for n in nodes
        if n.base_handle is not None and n.renderable and not n.is_hidden
    }
    targets: list[int] = []
    skipped: list[str] = []
    for node in nodes:
        if node.renderable and not node.is_hidden:
            continue
        reason = hidden_reason(node, config, visible_bases, render_hidden)
        if reason:
            skipped.append(_describe(node, reason))
        else:
            targets.append(node.handle)

    if not targets:
        return [], skipped
    return [
        Op(
            id="hidden.remove",
            stage=Stage.HIDDEN,
            title=f"remove {len(targets):,} non-rendering object(s)",
            targets=tuple(targets),
        )
    ], skipped


def plan_collapse(nodes: list[NodeFacts], config: PlanConfig) -> tuple[list[Op], list[str]]:
    ops: list[Op] = []
    skipped: list[str] = []
    for node in nodes:
        reason = collapse_reason(node, config)
        if reason:
            if node.modifier_classes:  # only worth reporting if there was a stack
                skipped.append(_describe(node, reason))
            continue
        ops.append(
            Op(
                id=f"collapse.{node.handle}",
                stage=Stage.COLLAPSE,
                title=f"collapse {len(node.modifier_classes)} modifier(s) on {node.name}",
                targets=(node.handle,),
                payload={"modifiers": list(node.modifier_classes)},
            )
        )
    return ops, skipped


def plan_proxies(
    nodes: list[NodeFacts], config: PlanConfig, engine: Engine
) -> tuple[list[Op], list[str]]:
    if not engine.is_vray:
        return [], [
            f"proxy conversion skipped — renderer is {engine.value}, not V-Ray"
        ]

    ops: list[Op] = []
    skipped: list[str] = []
    for node in nodes:
        reason = proxy_reason(node, config)
        if reason:
            # Only report nodes heavy enough to have been worth converting;
            # otherwise every tiny prop floods the log.
            if node.faces >= config.per_node_face_threshold // 4:
                skipped.append(_describe(node, reason))
            continue
        ops.append(
            Op(
                id=f"proxy.{node.handle}",
                stage=Stage.PROXY,
                title=f"proxy {node.name} ({node.faces:,} faces)",
                targets=(node.handle,),
                payload={"faces": node.faces},
            )
        )
    ops.sort(key=lambda op: -op.payload.get("faces", 0))
    return ops, skipped


def plan_viewport(nodes: list[NodeFacts], config: PlanConfig) -> tuple[list[Op], list[str]]:
    """Viewport-only levers. Render-identical by construction."""
    ops = [
        Op(
            id="viewport.bitmap_flush",
            stage=Stage.VIEWPORT,
            title="page full-resolution bitmaps out of memory",
            payload={"mode": "renderMode_UseFullRes_FlushFromMemory"},
        ),
        Op(
            id="viewport.nitrous",
            stage=Stage.VIEWPORT,
            title=f"cap viewport textures at {config.nitrous_texture_limit}px",
            payload={"pixels": config.nitrous_texture_limit},
        ),
    ]
    scatter = [
        n.handle
        for n in nodes
        if any(_norm(n.class_name).startswith(p) for p in _NEVER_TOUCH_PREFIXES)
    ]
    if scatter:
        ops.append(
            Op(
                id="viewport.scatter",
                stage=Stage.VIEWPORT,
                title=f"switch {len(scatter)} scatter object(s) to a light viewport mode",
                targets=tuple(scatter),
            )
        )
    return ops, []


def plan_textures(
    textures: list[TextureFacts], config: PlanConfig
) -> tuple[list[Op], list[str]]:
    """One op per oversized texture.

    Per texture rather than one bulk op, so a single unreadable file costs its
    own reduction and not the other four hundred.
    """
    ops: list[Op] = []
    skipped: list[str] = []
    floor = config.effective_texture_floor

    for texture in textures:
        reason = texture_reason(texture, config)
        if reason:
            skipped.append(
                f"{texture.path or f'<loader {texture.handle}>'} "
                f"({texture.longest_edge:,}px) — {reason}"
            )
            continue
        ops.append(
            Op(
                id=f"texture.{texture.handle}",
                stage=Stage.TEXTURES,
                title=(
                    f"{texture.longest_edge:,}px → {floor:,}px: "
                    f"{ntpath.basename(texture.path)}"
                ),
                targets=(texture.handle,),
                payload={"floor": floor, "path": texture.path, "loader": texture.loader},
                predicted_note=(
                    "This is the one reduction that changes the render. The "
                    "original file is never modified or deleted."
                ),
            )
        )

    # Biggest first: a halt partway leaves the largest saving already banked.
    ops.sort(key=lambda op: -op.payload.get("floor", 0))
    return ops, skipped


def plan_bitmaps(config: PlanConfig) -> tuple[list[Op], list[str]]:
    if not config.convert_bitmaps:
        return [], [
            "bitmap conversion not requested — it is NOT render-identical and "
            "must be enabled explicitly"
        ]
    return [
        Op(
            id="bitmap.convert",
            stage=Stage.BITMAP,
            title="convert bitmap loaders to VRayBitmap",
            predicted_note=(
                "NOT render-identical: VRayBitmap uses a different filtering "
                "kernel by design. Runs only when a render-identity gate is "
                "supplied, and rolls back if the diff fails."
            ),
        )
    ], []


def build_plan(
    nodes: list[NodeFacts],
    config: PlanConfig,
    engine: Engine = Engine.VRAY,
    render_hidden: bool = False,
    textures: list[TextureFacts] | None = None,
) -> Plan:
    """Assemble every stage in fixed order."""
    ops: list[Op] = []
    skipped: list[str] = []

    for planner in (
        lambda: plan_hygiene(nodes, config),
        lambda: plan_hidden(nodes, config, render_hidden),
        lambda: plan_collapse(nodes, config),
        lambda: plan_proxies(nodes, config, engine),
        lambda: plan_viewport(nodes, config),
        lambda: plan_textures(list(textures or []), config),
        lambda: plan_bitmaps(config),
    ):
        stage_ops, stage_skipped = planner()
        ops.extend(stage_ops)
        skipped.extend(stage_skipped)

    # Sort by stage ONLY. Python's sort is stable, so each planner's own
    # ordering survives — which matters because plan_proxies deliberately puts
    # the heaviest objects first, so a run that halts partway has still banked
    # the biggest wins. Adding op.id to this key silently undoes that.
    ops.sort(key=lambda op: op.stage.value)
    return Plan(ops=tuple(ops), skipped=tuple(skipped))
