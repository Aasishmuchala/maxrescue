"""The memory governor — how the scene gets rebuilt without ever holding it whole.

Two things make this hard and both are tested here:

* **The disk-to-RAM multiplier is unknown.** It cannot be looked up; it has to be
  *learned* from the first batches. So the governor starts on a declared,
  clearly-labelled prior and corrects itself from measurement.
* **Hierarchies must survive.** A parent merged in one batch and its child in
  another is how a rebuilt scene quietly loses its structure.

The invariants that must never break, whatever the input: every candidate is
merged exactly once, no batch is empty, and planning always terminates.
"""

from __future__ import annotations

import pytest

from maxrescue.core.governor import (
    PRIOR_DISK_TO_RAM,
    Batch,
    Calibration,
    Governor,
    MergeCandidate,
)

GB = 1 << 30
MB = 1 << 20


def _candidates(*weights: int, start: int = 0) -> list[MergeCandidate]:
    return [
        MergeCandidate(name=f"obj_{i + start}", weight=w, position=i + start)
        for i, w in enumerate(weights)
    ]


def _governor(budget_gb: float = 60, **kw) -> Governor:
    return Governor(ram_budget=int(budget_gb * GB), **kw)


def _all_names(batches: list[Batch]) -> list[str]:
    return [n for b in batches for n in b.names]


# --------------------------------------------------------------------------
# invariants — these hold for every input
# --------------------------------------------------------------------------


def test_every_candidate_is_merged_exactly_once():
    candidates = _candidates(*[10 * MB] * 200)
    batches = _governor().plan_all(candidates)
    names = _all_names(batches)
    assert sorted(names) == sorted(c.name for c in candidates)
    assert len(names) == len(set(names))


def test_no_batch_is_ever_empty():
    batches = _governor(budget_gb=1).plan_all(_candidates(*[100 * MB] * 50))
    assert batches
    assert all(b.names for b in batches)


def test_planning_terminates_when_everything_is_oversized():
    """Pathological input must not spin. Each oversized item becomes its own
    batch and the plan completes."""
    batches = _governor(budget_gb=1).plan_all(_candidates(*[50 * GB] * 5))
    assert len(batches) == 5
    assert all(b.isolated for b in batches)


def test_an_empty_scene_plans_nothing_rather_than_one_empty_batch():
    assert _governor().plan_all([]) == []


def test_planning_is_deterministic():
    candidates = _candidates(*[7 * MB] * 100)
    first = _governor().plan_all(candidates)
    second = _governor().plan_all(candidates)
    assert [b.names for b in first] == [b.names for b in second]


# --------------------------------------------------------------------------
# fitting to the budget
# --------------------------------------------------------------------------


def test_a_scene_that_fits_is_one_batch():
    batches = _governor(budget_gb=60).plan_all(_candidates(*[1 * MB] * 10))
    assert len(batches) == 1


def test_a_scene_that_does_not_fit_is_split():
    # prior multiplier turns 500 MB of disk into several GB of predicted RAM
    batches = _governor(budget_gb=4).plan_all(_candidates(*[100 * MB] * 20))
    assert len(batches) > 1


def test_no_batch_exceeds_the_usable_budget():
    governor = _governor(budget_gb=8)
    batches = governor.plan_all(_candidates(*[50 * MB] * 60))
    for batch in batches:
        if not batch.isolated:
            assert governor.predicted_ram(batch.weight) <= governor.usable_budget


def test_an_object_too_big_for_any_batch_is_isolated_and_flagged():
    candidates = _candidates(5 * MB, 500 * GB, 5 * MB)
    batches = _governor(budget_gb=8).plan_all(candidates)
    isolated = [b for b in batches if b.isolated]
    assert len(isolated) == 1
    assert isolated[0].names == ("obj_1",)
    assert isolated[0].note is not None
    assert "budget" in isolated[0].note.lower()


def test_the_safety_margin_leaves_headroom_below_the_hard_ceiling():
    """Filling to exactly the ceiling leaves nothing for Max's own overhead."""
    governor = _governor(budget_gb=100)
    assert governor.usable_budget < 100 * GB


# --------------------------------------------------------------------------
# hierarchies
# --------------------------------------------------------------------------


def _family() -> list[MergeCandidate]:
    return [
        MergeCandidate("Group_Head", 5 * MB, position=0),
        MergeCandidate("Child_A", 5 * MB, position=1, parent_position=0),
        MergeCandidate("Child_B", 5 * MB, position=2, parent_position=0),
        MergeCandidate("Loner", 5 * MB, position=3),
    ]


def test_a_parent_and_its_children_land_in_the_same_batch():
    """Splitting a hierarchy across batches is how a rebuilt scene silently
    loses its structure."""
    batches = _governor(budget_gb=1).plan_all(_family())
    home = {name: i for i, b in enumerate(batches) for name in b.names}
    assert home["Group_Head"] == home["Child_A"] == home["Child_B"]


def test_grandchildren_travel_with_the_whole_family():
    chain = [
        MergeCandidate("A", 1 * MB, position=0),
        MergeCandidate("B", 1 * MB, position=1, parent_position=0),
        MergeCandidate("C", 1 * MB, position=2, parent_position=1),
        MergeCandidate("D", 1 * MB, position=3, parent_position=2),
    ]
    batches = _governor(budget_gb=1).plan_all(chain)
    home = {name: i for i, b in enumerate(batches) for name in b.names}
    assert len({home[n] for n in "ABCD"}) == 1


def test_an_oversized_family_is_kept_together_and_flagged():
    """Correctness beats the budget here — a broken hierarchy cannot be undone
    by a later pass, but an over-budget batch can at least be reported."""
    big = [
        MergeCandidate("Head", 40 * GB, position=0),
        MergeCandidate("Kid", 40 * GB, position=1, parent_position=0),
    ]
    batches = _governor(budget_gb=8).plan_all(big)
    assert len(batches) == 1
    assert batches[0].isolated
    assert set(batches[0].names) == {"Head", "Kid"}


def test_a_parent_reference_outside_the_candidate_set_is_ignored():
    """Merging a subset is legitimate; a dangling parent index must not crash
    or drag in a phantom."""
    orphan = [MergeCandidate("Child", 1 * MB, position=5, parent_position=999)]
    batches = _governor().plan_all(orphan)
    assert _all_names(batches) == ["Child"]


def test_a_parent_cycle_does_not_hang():
    """A damaged file can describe A as B's parent and B as A's."""
    cyclic = [
        MergeCandidate("A", 1 * MB, position=0, parent_position=1),
        MergeCandidate("B", 1 * MB, position=1, parent_position=0),
    ]
    batches = _governor().plan_all(cyclic)
    assert sorted(_all_names(batches)) == ["A", "B"]


# --------------------------------------------------------------------------
# calibration — the multiplier is learned, not assumed
# --------------------------------------------------------------------------


def test_the_prior_is_explicitly_marked_as_not_measured():
    """Nothing downstream should be able to mistake the prior for a fact."""
    calibration = Calibration.prior()
    assert calibration.observed is False
    assert calibration.multiplier == PRIOR_DISK_TO_RAM
    assert "not" in calibration.describe().lower()


def test_observing_a_batch_replaces_the_prior_with_measurement():
    governor = _governor()
    governor.observe(batch_weight=1 * GB, rss_delta=4 * GB)
    assert governor.calibration.observed is True
    assert governor.calibration.multiplier == pytest.approx(4.0, rel=0.5)
    assert "measured" in governor.calibration.describe().lower()


def test_a_heavier_than_expected_batch_shrinks_the_next_one():
    governor = _governor(budget_gb=8)
    before = len(governor.plan_all(_candidates(*[20 * MB] * 40)))
    governor.observe(batch_weight=100 * MB, rss_delta=100 * GB)  # far worse than prior
    after = len(governor.plan_all(_candidates(*[20 * MB] * 40)))
    assert after > before


def test_a_lighter_than_expected_batch_grows_the_next_one():
    governor = _governor(budget_gb=8)
    before = len(governor.plan_all(_candidates(*[20 * MB] * 40)))
    governor.observe(batch_weight=1 * GB, rss_delta=1 * GB)  # much better than prior
    after = len(governor.plan_all(_candidates(*[20 * MB] * 40)))
    assert after <= before


def test_calibration_never_drops_to_a_multiplier_that_would_overcommit():
    """A single freakishly cheap batch must not convince the governor that RAM
    is free."""
    governor = _governor()
    governor.observe(batch_weight=1 * GB, rss_delta=0)
    assert governor.calibration.multiplier >= 1.0


def test_repeated_observations_converge_rather_than_oscillate():
    governor = _governor()
    for _ in range(10):
        governor.observe(batch_weight=1 * GB, rss_delta=6 * GB)
    assert governor.calibration.multiplier == pytest.approx(6.0, rel=0.2)
    assert governor.calibration.samples == 10


def test_observation_ignores_a_nonsense_negative_delta():
    """RSS can fall during a batch if Max frees a cache. That says nothing
    about cost and must not poison the estimate."""
    governor = _governor()
    governor.observe(batch_weight=1 * GB, rss_delta=6 * GB)
    before = governor.calibration.multiplier
    governor.observe(batch_weight=1 * GB, rss_delta=-2 * GB)
    assert governor.calibration.multiplier == before


def test_observation_ignores_a_zero_weight_batch():
    governor = _governor()
    governor.observe(batch_weight=0, rss_delta=5 * GB)
    assert governor.calibration.observed is False


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def test_the_plan_reports_what_it_predicted_and_on_what_basis():
    governor = _governor(budget_gb=8)
    batches = governor.plan_all(_candidates(*[50 * MB] * 40))
    summary = governor.describe_plan(batches)
    assert "batches" in summary.lower()
    assert "not measured" in summary.lower()  # the prior must announce itself


def test_isolated_batches_are_counted_in_the_summary():
    governor = _governor(budget_gb=1)
    batches = governor.plan_all(_candidates(5 * MB, 900 * GB))
    assert "1 over budget" in governor.describe_plan(batches)


# --------------------------------------------------------------------------
# joining to the X-ray
# --------------------------------------------------------------------------


def test_candidates_are_built_from_named_nodes():
    from maxrescue.core.governor import candidates_from
    from maxrescue.xray.nodes import NodeInfo

    nodes = [
        NodeInfo(position=0, name="Hero", bytes=100, object_bytes=5000,
                 parent_position=None, resolved=True),
        NodeInfo(position=1, name="Child", bytes=100, object_bytes=200,
                 parent_position=0, resolved=True),
    ]
    candidates = candidates_from(nodes)
    assert [c.name for c in candidates] == ["Hero", "Child"]
    assert candidates[0].weight == 5100
    assert candidates[1].parent_position == 0


def test_unnamed_nodes_are_dropped_because_merge_selects_by_name():
    from maxrescue.core.governor import candidates_from
    from maxrescue.xray.nodes import NodeInfo

    nodes = [
        NodeInfo(position=0, name="", bytes=100, object_bytes=5000, resolved=False),
        NodeInfo(position=1, name="Real", bytes=100, object_bytes=200, resolved=True),
    ]
    assert [c.name for c in candidates_from(nodes)] == ["Real"]
