"""The bulk scene read, as one MAXScript function.

Per-node `pymxs` attribute access is roughly ten times slower than one crossing
that returns a whole array — on a 2,000-object scene the per-node version took
nine minutes in a sibling project. So this compiles one function, calls it once,
and gets back a nested MAXScript array that pymxs hands over as a sequence. No
string parsing, so a node called "Tree\\t01" cannot corrupt the read.

Every property read sits in **its own** `try`. That is not defensive noise: a
Script *transform* controller makes `n.position.controller` throw, and when all
four controller reads shared one `try`, the strongest scripted-controller signal
in the scene was exactly the one silently lost.

Instance detection is by shared `baseObject` handle, never `InstanceMgr` — the
latter is O(n²) and caused a nine-minute stall.
"""

from __future__ import annotations

__all__ = ["NODE_FACTS_FN", "NODE_FACTS_FIELDS", "compile_helpers"]

#: Order of the fields each row of `mrNodeFacts()` returns. Kept beside the
#: MAXScript so the two cannot drift apart unnoticed.
NODE_FACTS_FIELDS = (
    "handle",
    "name",
    "class_name",
    "super_class",
    "faces",
    "vertices",
    "base_handle",
    "modifiers",
    "layer",
    "child_count",
    "has_parent",
    "has_dependents",
    "in_group",
    "is_group_head",
    "is_hidden",
    "renderable",
    "primary_visibility",
    "is_animated",
    "has_skin",
    "is_bone",
    "in_xref",
    "scripted_controller",
    "negative_scale",
    "render_coupled",
)

NODE_FACTS_FN = r"""
fn mrSafeBool expr default:false = (
    local r = default
    try (r = expr) catch (r = default)
    r
)

fn mrNodeFacts = (
    local rows = #()
    for n in objects do (
        local handle = 0
        try (handle = getHandleByAnim n) catch ()

        local nm = ""
        try (nm = n.name as string) catch ()

        local cls = ""
        try (cls = (classOf n) as string) catch ()

        local supercls = ""
        try (supercls = (superClassOf n) as string) catch ()

        local faces = 0
        local verts = 0
        try (
            local c = getPolygonCount n
            faces = c[1]
            verts = c[2]
        ) catch ()

        -- Instance detection by shared base object: O(n), no InstanceMgr.
        local baseHandle = 0
        try (
            local bh = getHandleByAnim n.baseObject
            if bh != handle do baseHandle = bh
        ) catch ()

        local mods = #()
        local coupled = #()
        try (
            for m in n.modifiers do (
                local mc = (classOf m) as string
                append mods mc
                -- A subdivision modifier whose viewport iterations also drive
                -- the render: touching it changes the image.
                try (
                    if (isProperty m #useRenderIterations) and (not m.useRenderIterations) do
                        append coupled mc
                ) catch ()
            )
        ) catch ()

        local layerName = ""
        try (layerName = n.layer.name) catch ()

        local kids = 0
        try (kids = n.children.count) catch ()

        local hasParent = mrSafeBool (n.parent != undefined)

        -- Childless helpers are often LookAt / constraint targets; deleting one
        -- silently breaks whatever pointed at it.
        local deps = false
        try (
            local d = refs.dependentNodes n firstOnly:true
            deps = (d != undefined and d.count > 0)
        ) catch ()

        local inGrp = mrSafeBool (isGroupMember n)
        local grpHead = mrSafeBool (isGroupHead n)
        local hidden = mrSafeBool (n.isHidden)
        local rend = mrSafeBool (n.renderable) default:true
        local primVis = mrSafeBool (n.primaryVisibility) default:true
        local anim = mrSafeBool (n.isAnimated)
        local bone = mrSafeBool (isKindOf n BoneGeometry)
        local xref = mrSafeBool (isKindOf n XRefObject)

        local skin = false
        try (
            for m in n.modifiers while not skin do (
                local mc = ((classOf m) as string)
                if mc == "Skin" or mc == "Physique" or mc == "Morpher" do skin = true
            )
        ) catch ()

        -- Each controller read in ITS OWN try. Sharing one try here masked
        -- Script transform controllers, the strongest signal of the four.
        local scripted = false
        try (if (classOf n.controller) == Script_Controller do scripted = true) catch ()
        try (if (classOf n.position.controller) == Position_Script do scripted = true) catch ()
        try (if (classOf n.rotation.controller) == Rotation_Script do scripted = true) catch ()
        try (if (classOf n.scale.controller) == Scale_Script do scripted = true) catch ()

        -- determinant3 is NOT a MAXScript global; compute it explicitly.
        local negScale = false
        try (
            local m = n.transform
            if (dot (cross m.row1 m.row2) m.row3) < 0 do negScale = true
        ) catch ()

        append rows #(
            handle, nm, cls, supercls, faces, verts, baseHandle, mods, layerName,
            kids, hasParent, deps, inGrp, grpHead, hidden, rend, primVis,
            anim, skin, bone, xref, scripted, negScale, coupled
        )
    )
    rows
)

fn mrSuspiciousCallbacks = (
    local ss = stringStream ""
    -- callbacks.show PRINTS; it does not return. Capture the stream.
    callbacks.show to:ss
    ss as string
)

fn mrSceneStates = (
    local out = #()
    try (
        for i = 1 to sceneStateMgr.GetCount() do
            append out (sceneStateMgr.GetSceneState i)
    ) catch ()
    out
)
"""


def compile_helpers(rt) -> None:
    """Compile the helpers into the running MAXScript session, once."""
    rt.execute(NODE_FACTS_FN)
