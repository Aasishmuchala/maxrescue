"""The guard matrix.

Every guard here exists because something went wrong once. The tests are
written as "this node must be REFUSED, and here is why" rather than as coverage,
because the failure mode being prevented is the point.

The asymmetry is deliberate throughout: refusing a node that could safely have
been converted costs a little memory. Converting one that could not costs the
user their scene.
"""

from __future__ import annotations

import pytest

from maxrescue.core.plan import (
    PlanConfig,
    build_plan,
    collapse_reason,
    hidden_reason,
    proxy_reason,
)
from maxrescue.core.types import Engine, NodeFacts, Stage

CONFIG = PlanConfig()


def node(**kw) -> NodeFacts:
    """A node that is safe to convert unless a keyword says otherwise."""
    base = dict(
        handle=1,
        name="Mesh_01",
        class_name="Editable_Poly",
        super_class="GeomObject",
        faces=200_000,
        vertices=100_000,
        renderable=True,
        is_hidden=False,
        primary_visibility=True,
    )
    base.update(kw)
    return NodeFacts(**base)


# ---------------------------------------------------------------------------
# proxy conversion
# ---------------------------------------------------------------------------


def test_a_plain_heavy_mesh_is_convertible():
    assert proxy_reason(node(), CONFIG) is None


def test_a_stack_of_proven_bakeable_modifiers_is_convertible():
    assert proxy_reason(node(modifier_classes=("Bend", "Shell", "Smooth")), CONFIG) is None


def test_an_unrecognised_modifier_is_refused_rather_than_assumed_safe():
    """A proxy captures the evaluated mesh. A modifier that contributes at
    render time instead — displacement, hair, render-time subdivision — is
    silently discarded by the bake, so anything unproven is refused."""
    reason = proxy_reason(node(modifier_classes=("SomeNewVendorModifier",)), CONFIG)
    assert reason is not None and "bakeable" in reason


def test_a_turbosmooth_whose_render_differs_is_refused():
    """The dangerous case is useRenderIterations ON: viewport shows 1
    iteration, the render uses 3, and a bake captures the viewport mesh. The
    first version of this test was inverted and passed exactly this node."""
    reason = proxy_reason(
        node(
            modifier_classes=("TurboSmooth",),
            render_differs_from_viewport=("TurboSmooth",),
        ),
        CONFIG,
    )
    assert reason is not None


@pytest.mark.parametrize(
    "kwargs, expect",
    [
        ({"super_class": "Helper"}, "not geometry"),
        ({"class_name": "VRayProxy"}, "already a proxy"),
        ({"class_name": "Forest_Pro"}, "scatter"),
        ({"class_name": "RailClone"}, "scatter"),
        ({"class_name": "tyFlow"}, "scatter"),
        ({"class_name": "PhoenixFD"}, "scatter"),
        ({"class_name": "VRayPlane"}, "scatter"),
        ({"class_name": "Sphere"}, "whitelist"),
        ({"in_xref": True}, "XRef"),
        ({"scripted_controller": True}, "script"),
        ({"is_animated": True}, "animated"),
        ({"has_skin": True}, "skinned"),
        ({"is_bone": True}, "bone"),
        ({"in_group": True}, "group"),
        ({"is_group_head": True}, "group"),
        ({"child_count": 3}, "children"),
        ({"has_parent": True}, "parent"),
        ({"negative_scale": True}, "mirrored"),
        ({"render_differs_from_viewport": ("TurboSmooth",)}, "renders differently"),
        ({"modifier_classes": ("VRayDisplacementMod",)}, "bakeable"),
        ({"modifier_classes": ("Hair_and_Fur",)}, "bakeable"),
        ({"modifier_classes": ("SomeVendorPlugin",)}, "bakeable"),
        ({"faces": 100}, "threshold"),
    ],
    ids=lambda v: str(v)[:40],
)
def test_proxy_guards_refuse_and_explain(kwargs, expect):
    reason = proxy_reason(node(**kwargs), CONFIG)
    assert reason is not None, f"{kwargs} should have been refused"
    assert expect.lower() in reason.lower()


def test_scatter_prefix_match_survives_a_vendor_version_bump():
    """iToo renamed Forest_Pro_3 to Forest_Pro. Exact-name matching would have
    silently started converting scatter objects after an update."""
    for name in ("Forest_Pro", "Forest_Pro_3", "ForestPackPro", "RailClone2"):
        assert proxy_reason(node(class_name=name), CONFIG) is not None


def test_a_group_member_is_refused_even_when_otherwise_perfect():
    """Selecting one member of a closed group selects the whole group — a
    production run turned 27 conversions into ~25,000 consumed nodes."""
    assert "group" in proxy_reason(node(in_group=True), CONFIG)


# ---------------------------------------------------------------------------
# hidden / non-rendering removal
# ---------------------------------------------------------------------------


def test_a_hidden_object_is_removable_when_the_scene_does_not_render_hidden():
    assert hidden_reason(node(is_hidden=True), CONFIG, set(), False) is None


def test_a_non_renderable_object_is_removable():
    assert hidden_reason(node(renderable=False), CONFIG, set(), False) is None


def test_nothing_is_removable_when_the_scene_renders_hidden_objects():
    """The whole premise of this stage is that hidden means invisible. With
    rendHidden on, it does not, and deleting changes the image."""
    assert hidden_reason(node(is_hidden=True), CONFIG, set(), True) is not None


def test_an_object_invisible_to_camera_is_kept_because_it_still_lights_the_scene():
    """primaryVisibility=False still contributes to GI and reflections.
    Deleting it is a visible change with an invisible cause."""
    reason = hidden_reason(
        node(primary_visibility=False, is_hidden=True), CONFIG, set(), False
    )
    assert reason is not None
    assert "gi" in reason.lower() or "reflection" in reason.lower()


def test_an_instance_master_is_kept_even_when_hidden():
    """Deleting the hidden master of an instance set takes the geometry with it
    and every visible copy goes too."""
    hidden_master = node(handle=1, is_hidden=True, base_handle=99)
    reason = hidden_reason(hidden_master, CONFIG, {99}, False)
    assert reason is not None and "instance" in reason.lower()


def test_a_constraint_target_is_kept():
    reason = hidden_reason(
        node(is_hidden=True, has_dependents=True), CONFIG, set(), False
    )
    assert reason is not None and "depends" in reason.lower()


@pytest.mark.parametrize(
    "kwargs",
    [{"child_count": 2}, {"in_group": True}, {"in_xref": True}, {"is_bone": True}],
    ids=lambda v: str(v)[:30],
)
def test_more_hidden_guards(kwargs):
    assert hidden_reason(node(is_hidden=True, **kwargs), CONFIG, set(), False)


def test_the_whole_hidden_stage_is_skipped_when_rendhidden_is_on(capsys):
    nodes = [node(handle=1, is_hidden=True), node(handle=2, renderable=False)]
    plan = build_plan(nodes, CONFIG, Engine.VRAY, render_hidden=True)
    assert not [op for op in plan.ops if op.stage is Stage.HIDDEN]
    assert any("rendHidden" in s for s in plan.skipped)


# ---------------------------------------------------------------------------
# stack collapse
# ---------------------------------------------------------------------------


def test_a_static_stack_is_collapsible():
    assert collapse_reason(node(modifier_classes=("Bend", "Twist")), CONFIG) is None


def test_a_node_with_no_stack_has_nothing_to_collapse():
    assert collapse_reason(node(modifier_classes=()), CONFIG) is not None


@pytest.mark.parametrize(
    "modifier, expect",
    [
        ("TurboSmooth", "drive the render"),
        ("MeshSmooth", "drive the render"),
        ("OpenSubdiv", "drive the render"),
        ("Skin", "deformation"),
        ("Morpher", "deformation"),
        ("Cloth", "deformation"),
        ("VRayDisplacementMod", "deformation"),
        ("Hair_and_Fur", "deformation"),
    ],
)
def test_collapse_refuses_modifiers_that_change_the_render_or_carry_deformation(
    modifier, expect
):
    reason = collapse_reason(node(modifier_classes=(modifier,)), CONFIG)
    assert reason is not None
    assert expect in reason.lower()


def test_collapse_refuses_an_animated_node():
    reason = collapse_reason(node(modifier_classes=("Bend",), is_animated=True), CONFIG)
    assert reason is not None and "animated" in reason.lower()


def test_one_unsafe_modifier_protects_the_whole_stack():
    """A stack is collapsed as a unit; a single Skin in it makes the whole
    collapse unsafe, not just that entry."""
    stack = ("Bend", "Skin", "Twist")
    assert collapse_reason(node(modifier_classes=stack), CONFIG) is not None


# ---------------------------------------------------------------------------
# assembling the plan
# ---------------------------------------------------------------------------


def test_stages_run_in_the_fixed_documented_order():
    nodes = [
        node(handle=1, faces=500_000),
        node(handle=2, is_hidden=True, faces=10),
        node(handle=3, modifier_classes=("Bend",), faces=10),
    ]
    plan = build_plan(nodes, CONFIG, Engine.VRAY, render_hidden=False)
    stages = [op.stage.value for op in plan.ops]
    assert stages == sorted(stages)


def test_every_exclusion_carries_a_reason():
    nodes = [node(handle=1, class_name="Forest_Pro", faces=900_000)]
    plan = build_plan(nodes, CONFIG, Engine.VRAY)
    assert plan.skipped
    assert all("—" in line for line in plan.skipped)


def test_heaviest_objects_are_proxied_first():
    """If a run halts partway, the biggest win should already be banked."""
    nodes = [
        node(handle=1, name="small", faces=40_000),
        node(handle=2, name="huge", faces=5_000_000),
        node(handle=3, name="mid", faces=200_000),
    ]
    plan = build_plan(nodes, CONFIG, Engine.VRAY)
    proxies = [op for op in plan.ops if op.stage is Stage.PROXY]
    assert [op.payload["faces"] for op in proxies] == [5_000_000, 200_000, 40_000]


def test_a_non_vray_scene_gets_hygiene_but_no_proxies():
    nodes = [node(handle=1, faces=900_000)]
    plan = build_plan(nodes, CONFIG, Engine.CORONA)
    assert not [op for op in plan.ops if op.stage is Stage.PROXY]
    assert any("Corona" in s for s in plan.skipped)
    assert [op for op in plan.ops if op.stage is Stage.HYGIENE]


def test_bitmap_conversion_is_absent_unless_explicitly_enabled():
    plan = build_plan([node()], CONFIG, Engine.VRAY)
    assert not [op for op in plan.ops if op.stage is Stage.BITMAP]
    assert any("NOT render-identical" in s for s in plan.skipped)


def test_bitmap_conversion_appears_when_asked_for_and_says_it_is_gated():
    config = PlanConfig(convert_bitmaps=True)
    plan = build_plan([node()], config, Engine.VRAY)
    ops = [op for op in plan.ops if op.stage is Stage.BITMAP]
    assert len(ops) == 1
    assert "diff" in ops[0].predicted_note


def test_only_the_bitmap_stage_is_not_render_identical():
    """A regression guard on the contract itself."""
    assert [s for s in Stage if not s.render_identical] == [Stage.BITMAP]


def test_hygiene_runs_even_for_an_empty_scene():
    plan = build_plan([], CONFIG, Engine.VRAY)
    assert [op for op in plan.ops if op.stage is Stage.HYGIENE]


def test_scatter_objects_get_a_viewport_op_but_never_a_proxy_op():
    nodes = [node(handle=1, class_name="Forest_Pro", faces=900_000)]
    plan = build_plan(nodes, CONFIG, Engine.VRAY)
    assert not [op for op in plan.ops if op.stage is Stage.PROXY]
    viewport = [op for op in plan.ops if op.id == "viewport.scatter"]
    assert viewport and viewport[0].targets == (1,)


def test_the_automatic_bitmap_op_never_uses_the_render_changing_mode():
    """`#renderMode_UseProxies` renders the downscaled image. Only the
    flush-from-memory mode is identical."""
    plan = build_plan([node()], CONFIG, Engine.VRAY)
    (op,) = [o for o in plan.ops if o.id == "viewport.bitmap_flush"]
    assert op.payload["mode"] == "renderMode_UseFullRes_FlushFromMemory"


def test_stage_ordering_does_not_destroy_within_stage_ordering():
    """Regression: sorting the assembled plan by (stage, id) re-alphabetised the
    proxy ops and quietly reversed the heaviest-first intent."""
    nodes = [node(handle=h, name=f"n{h}", faces=f) for h, f in
             [(1, 40_000), (2, 5_000_000), (3, 200_000), (10, 900_000)]]
    proxies = [op for op in build_plan(nodes, CONFIG, Engine.VRAY).ops
               if op.stage is Stage.PROXY]
    faces = [op.payload["faces"] for op in proxies]
    assert faces == sorted(faces, reverse=True)
