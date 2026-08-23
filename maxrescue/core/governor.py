"""The memory governor: how a scene gets rebuilt without ever holding it whole.

The governor decides which objects go into each `mergeMaxFile` call so that peak
RAM stays under a ceiling the machine can actually meet.

Doing that requires predicting RAM from on-disk weight, and **that multiplier
cannot be looked up**. It depends on class, modifier-stack depth, instancing and
Max's own overhead. So the governor does the only honest thing available: it
starts on a declared prior, marked as *not measured*, and corrects itself from
the RSS delta of every batch it actually runs. After two or three batches the
number is the machine's own, not a guess.

This is the one place in MaxRescue that predicts anything, and it is allowed to
because it is self-correcting and because it never reports its prediction as a
result. The user-facing report states measured bytes only.

Two invariants hold for every input, including damaged ones:

* every candidate is merged exactly once — no drops, no duplicates;
* planning terminates, even when every object is individually over budget.

And one deliberate trade: **a family stays together even when it busts the
budget.** An over-budget batch can be reported and retried; a hierarchy split
across two merges is silently wrong in a way no later pass can detect.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "PRIOR_DISK_TO_RAM",
    "Batch",
    "Calibration",
    "Governor",
    "MergeCandidate",
]

#: Starting guess for on-disk bytes → resident bytes, pending spike S3.
#: Deliberately pessimistic: over-estimating costs an extra batch, while
#: under-estimating costs an out-of-memory crash on a 170 GB scene.
PRIOR_DISK_TO_RAM = 12.0

#: Share of the ceiling a batch may plan to occupy. The remainder absorbs Max's
#: own footprint and the fact that the multiplier is an average, not a bound.
DEFAULT_SAFETY = 0.75

#: A multiplier below this would let the governor believe geometry is free.
_MIN_MULTIPLIER = 1.0


@dataclass(frozen=True)
class MergeCandidate:
    """One object eligible to be merged, with everything needed to place it."""

    name: str
    weight: int
    """This node's OWN on-disk bytes, from the tier-2 walk."""

    position: int
    parent_position: int | None = None

    shared_key: int | None = None
    """The base object this node instances, if any.

    Two purposes, both load-bearing for instance-heavy archviz:

    * instances of one base must merge **together**, or each batch
      deserialises the shared mesh afresh and the rebuilt scene ends up with N
      copies of a 400k-face tree — bigger than the original;
    * the shared mesh must be counted **once** when sizing a batch. Counting it
      per instance makes 8,000 trees look like 8,000 unique meshes and the
      governor plans wildly more batches than the scene needs.
    """

    shared_weight: int = 0
    """Bytes of the shared base object. Counted once per `shared_key`."""


@dataclass(frozen=True)
class Batch:
    names: tuple[str, ...]
    weight: int
    isolated: bool = False
    """True when this batch could not be made to fit — it runs alone, and the
    caller is told rather than left to discover it as an out-of-memory kill."""
    note: str | None = None


@dataclass(frozen=True)
class Calibration:
    """The on-disk-to-RAM multiplier, and whether anyone has actually seen it."""

    multiplier: float
    samples: int = 0
    observed: bool = False

    @classmethod
    def prior(cls) -> Calibration:
        return cls(multiplier=PRIOR_DISK_TO_RAM, samples=0, observed=False)

    def describe(self) -> str:
        if not self.observed:
            return (
                f"disk→RAM ×{self.multiplier:.1f} (prior — NOT measured; "
                "batches will be resized once real deltas arrive)"
            )
        return (
            f"disk→RAM ×{self.multiplier:.1f} "
            f"(measured from {self.samples} batch(es))"
        )

    def with_observation(self, ratio: float) -> Calibration:
        """Fold in one measurement.

        A running mean over observations, so one freak batch cannot swing the
        estimate, but a consistent signal moves it fully within a few batches.
        """
        ratio = max(ratio, _MIN_MULTIPLIER)
        if not self.observed:
            return Calibration(multiplier=ratio, samples=1, observed=True)
        total = self.multiplier * self.samples + ratio
        samples = self.samples + 1
        return Calibration(
            multiplier=max(total / samples, _MIN_MULTIPLIER),
            samples=samples,
            observed=True,
        )


@dataclass
class Governor:
    ram_budget: int
    calibration: Calibration = field(default_factory=Calibration.prior)
    safety: float = DEFAULT_SAFETY

    # -- budget arithmetic -------------------------------------------------

    @property
    def usable_budget(self) -> int:
        """The ceiling a batch may plan against, after the safety margin."""
        return int(self.ram_budget * self.safety)

    def predicted_ram(self, weight: int) -> int:
        return int(weight * self.calibration.multiplier)

    def weight_budget(self, resident_bytes: int = 0) -> int:
        """On-disk bytes that will fit in the remaining headroom.

        `resident_bytes` is what the process already holds — Max's own footprint
        plus every batch merged so far. Sizing against a fixed fraction of the
        ceiling while the scene accumulates underneath means the ceiling is never
        actually enforced.
        """
        headroom = self.usable_budget - max(0, resident_bytes)
        return max(1, int(headroom / self.calibration.multiplier))

    # -- learning ----------------------------------------------------------

    def observe(self, batch_weight: int, rss_delta: int) -> None:
        """Record what a merged batch actually cost.

        Ignores meaningless samples rather than letting them poison the
        estimate: a zero-weight batch has no ratio, and RSS can *fall* during a
        merge when Max frees a cache, which says nothing about what geometry
        costs.
        """
        if batch_weight <= 0 or rss_delta <= 0:
            return
        self.calibration = self.calibration.with_observation(rss_delta / batch_weight)

    # -- planning ----------------------------------------------------------

    def plan_all(
        self, candidates: list[MergeCandidate], resident_bytes: int = 0
    ) -> list[Batch]:
        """Group every candidate into batches, families kept intact."""
        if not candidates:
            return []

        families = _families(candidates)
        budget = self.weight_budget(resident_bytes)

        # Heaviest first: first-fit-decreasing packs tighter and, more
        # usefully here, surfaces the over-budget families immediately.
        families.sort(key=lambda f: (-family_weight(f), f[0].name))

        batches: list[Batch] = []
        open_names: list[str] = []
        open_weight = 0

        def flush() -> None:
            nonlocal open_names, open_weight
            if open_names:
                batches.append(Batch(names=tuple(open_names), weight=open_weight))
                open_names, open_weight = [], 0

        for family in families:
            weight = family_weight(family)
            names = tuple(c.name for c in family)

            if weight > budget:
                # Cannot be made to fit. Run it alone and say so — a family is
                # never split, because a broken hierarchy cannot be recovered
                # by a later pass but an over-budget batch can be retried.
                flush()
                batches.append(
                    Batch(
                        names=names,
                        weight=weight,
                        isolated=True,
                        note=(
                            f"{_mb(weight)} on disk — predicted "
                            f"{_mb(self.predicted_ram(weight))} resident, over the "
                            f"{_mb(self.usable_budget)} usable budget. Merged alone; "
                            "expect this batch to be the risky one."
                        ),
                    )
                )
                continue

            if open_weight + weight > budget:
                flush()
            open_names.extend(names)
            open_weight += weight

        flush()
        return batches

    def next_batch(
        self, remaining: list[MergeCandidate], resident_bytes: int = 0
    ) -> tuple[Batch | None, list[MergeCandidate]]:
        """The next batch to merge, sized against the CURRENT calibration.

        Planning everything up front means every batch after the first is sized
        by a prior that measurement has already disproved. The self-correction
        only means something if the remaining work is re-planned after each
        observation.
        """
        if not remaining:
            return None, []
        batches = self.plan_all(remaining, resident_bytes)
        if not batches:
            return None, []
        first = batches[0]
        taken = set(first.names)
        return first, [c for c in remaining if c.name not in taken]

    def has_headroom(self, resident_bytes: int) -> bool:
        """Whether there is room to merge anything more at all.

        Batches are already sized against the headroom, so this only fires when
        the process has filled the ceiling — at which point stopping cleanly
        with a saved output beats being OOM-killed mid-merge.
        """
        return resident_bytes < self.usable_budget

    # -- reporting ---------------------------------------------------------

    def describe_plan(self, batches: list[Batch]) -> str:
        over = sum(1 for b in batches if b.isolated)
        total = sum(b.weight for b in batches)
        lines = [
            f"{len(batches)} batches, {_mb(total)} on disk total",
            f"  budget {_mb(self.ram_budget)} resident, "
            f"{_mb(self.usable_budget)} usable at {self.safety:.0%} safety",
            f"  {self.calibration.describe()}",
        ]
        if over:
            lines.append(
                f"  {over} over budget — merged alone, and the likeliest to fail"
            )
        return "\n".join(lines)


def family_weight(family: list[MergeCandidate]) -> int:
    """What a family costs, counting each shared base object once.

    Summing raw weights would count one instanced tree's mesh once per copy.
    """
    total = sum(c.weight for c in family)
    seen: set[int] = set()
    for candidate in family:
        if candidate.shared_key is None or candidate.shared_key in seen:
            continue
        seen.add(candidate.shared_key)
        total += candidate.shared_weight
    return total


def _families(candidates: list[MergeCandidate]) -> list[list[MergeCandidate]]:
    """Group candidates into connected components.

    Union-find over BOTH parent links and shared base objects. Parents outside
    the candidate set are ignored (merging a subset is legitimate), and cycles —
    which a damaged file can describe — collapse into one family instead of
    looping.
    """
    parent_of: dict[int, int] = {}

    def find(x: int) -> int:
        root = x
        while parent_of.get(root, root) != root:
            root = parent_of[root]
        while parent_of.get(x, x) != root:  # path compression
            parent_of[x], x = root, parent_of[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent_of[ra] = rb

    known = {c.position for c in candidates}
    for candidate in candidates:
        parent_of.setdefault(candidate.position, candidate.position)
    for candidate in candidates:
        if candidate.parent_position in known:
            union(candidate.position, candidate.parent_position)

    # Instances of one base object travel together, or each batch re-imports
    # the shared mesh and the output grows instead of shrinking.
    first_of_base: dict[int, int] = {}
    for candidate in candidates:
        if candidate.shared_key is None:
            continue
        anchor = first_of_base.setdefault(candidate.shared_key, candidate.position)
        union(candidate.position, anchor)

    grouped: dict[int, list[MergeCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(find(candidate.position), []).append(candidate)

    families = list(grouped.values())
    for family in families:
        family.sort(key=lambda c: c.position)
    return families


def _mb(value: int) -> str:
    if value >= 1 << 30:
        return f"{value / (1 << 30):,.1f} GB"
    return f"{value / (1 << 20):,.0f} MB"


def candidates_from(nodes) -> list[MergeCandidate]:
    """Turn resolved nodes into merge candidates.

    Deliberately duck-typed on `name` / `bytes` / `object_bytes` / `position` /
    `parent_position` so the planner keeps no dependency on the X-ray package.

    Only *named* nodes become candidates: `mergeMaxFile` selects by name, so a
    node whose name could not be resolved cannot be merged individually. The
    caller is told how many were dropped rather than left with a plan that
    silently covers less than the scene.
    """
    out: list[MergeCandidate] = []
    seen: dict[str, int] = {}
    for node in nodes:
        name = getattr(node, "name", "")
        if not name:
            continue
        # `mergeMaxFile` selects by NAME, and Max scenes routinely contain
        # duplicates. Asking for the same name in two batches merges every node
        # with that name twice, so four objects appear where two should — and
        # the merge only reports names that FAILED to arrive, never extras.
        if name in seen:
            seen[name] += 1
            continue
        seen[name] = 1

        base = getattr(node, "base_object_position", None)
        own = getattr(node, "bytes", 0)
        shared = getattr(node, "object_bytes", 0)
        out.append(
            MergeCandidate(
                name=name,
                weight=own if base is not None else own + shared,
                position=node.position,
                parent_position=getattr(node, "parent_position", None),
                shared_key=base,
                shared_weight=shared if base is not None else 0,
            )
        )
    return out


def duplicate_names(nodes) -> dict[str, int]:
    """Names shared by more than one node, with their counts.

    Surfaced rather than silently collapsed: merging by name cannot separate
    them, so the caller needs to know coverage is approximate.
    """
    counts: dict[str, int] = {}
    for node in nodes:
        name = getattr(node, "name", "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return {name: n for name, n in counts.items() if n > 1}
