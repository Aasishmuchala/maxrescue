"""The shared vocabulary. Pure — no Max, no Qt, no I/O.

Adapted from MaxSlim's stress-hardened `core/types.py`, with memory added as a
first-class measurement because that is what this tool exists to move.

Everything here is frozen. A run report that can be edited after the fact is a
run report nobody can trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "Assignment",
    "BatchOutcome",
    "BatchReport",
    "Engine",
    "GuardReport",
    "MemorySample",
    "Metrics",
    "NodeFacts",
    "Op",
    "OpResult",
    "OpStatus",
    "Plan",
    "RunReport",
    "SceneStats",
    "Stage",
]


class Engine(Enum):
    """The renderer in play. Non-V-Ray scenes get hygiene only."""

    VRAY = "V-Ray"
    CORONA = "Corona"
    OTHER = "other"
    UNKNOWN = "unknown"

    @property
    def is_vray(self) -> bool:
        return self is Engine.VRAY


class Stage(Enum):
    """Reduction stages, in the fixed order they run.

    Ordered cheapest-and-safest first, so a halt late in the run still leaves
    the cheap wins applied.
    """

    HYGIENE = 1
    HIDDEN = 2
    COLLAPSE = 3
    PROXY = 4
    VIEWPORT = 5
    BITMAP = 6

    @property
    def label(self) -> str:
        return {
            Stage.HYGIENE: "cleaning scene bloat",
            Stage.HIDDEN: "removing non-rendering objects",
            Stage.COLLAPSE: "collapsing modifier stacks",
            Stage.PROXY: "converting heavy meshes to proxies",
            Stage.VIEWPORT: "lightening viewport-only data",
            Stage.BITMAP: "converting bitmap loaders",
        }[self]

    @property
    def render_identical(self) -> bool:
        """False only for stages that can move a pixel, which are opt-in and
        gated behind an image diff."""
        return self is not Stage.BITMAP


class OpStatus(Enum):
    PENDING = "pending"
    APPLIED = "applied"
    SKIPPED = "skipped"
    FAILED = "failed"
    ROLLED_BACK = "rolled back"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class MemorySample:
    """One observation of process memory. Always says where it came from —
    a number whose provenance is unknown cannot be defended."""

    rss_mb: float
    peak_rss_mb: float = 0.0
    pagefile_mb: float = 0.0
    vram_mb: float | None = None
    source: str = "unknown"

    def delta_mb(self, other: MemorySample) -> float:
        return round(self.rss_mb - other.rss_mb, 1)


@dataclass(frozen=True)
class SceneStats:
    """Measured scene weight. Every field is counted, never estimated."""

    polygons: int = 0
    vertices: int = 0
    nodes: int = 0
    materials: int = 0
    file_mb: float = 0.0
    texture_mb: float = 0.0
    missing_assets: int = 0
    memory: MemorySample | None = None


@dataclass(frozen=True)
class NodeFacts:
    """What the planner needs to know about one node to judge it.

    Read once, in bulk, from the bridge — crossing the MAXScript boundary per
    node is roughly ten times slower and was a real production hang.
    """

    handle: int
    name: str
    class_name: str
    super_class: str
    faces: int = 0
    vertices: int = 0
    base_handle: int | None = None
    modifier_classes: tuple[str, ...] = ()
    material_handle: int | None = None
    layer: str = ""

    child_count: int = 0
    has_parent: bool = False
    has_dependents: bool = False
    in_group: bool = False
    is_group_head: bool = False

    is_hidden: bool = False
    renderable: bool = True
    primary_visibility: bool = True

    is_animated: bool = False
    has_skin: bool = False
    is_bone: bool = False
    in_xref: bool = False
    scripted_controller: bool = False
    negative_scale: bool = False

    render_coupled_modifiers: tuple[str, ...] = ()
    """Subdivision modifiers whose viewport iterations also drive the render.
    Collapsing or lowering these changes the image."""

    @property
    def is_instanced(self) -> bool:
        return self.base_handle is not None


@dataclass(frozen=True)
class Op:
    """One planned operation. Immutable; the plan is decided before anything
    is touched."""

    id: str
    stage: Stage
    title: str
    targets: tuple[int, ...] = ()
    payload: dict = field(default_factory=dict)
    predicted_note: str = ""


@dataclass(frozen=True)
class OpResult:
    op_id: str
    stage: Stage
    status: OpStatus
    message: str = ""
    verified: bool = False
    memory_before: MemorySample | None = None
    memory_after: MemorySample | None = None

    @property
    def freed_mb(self) -> float:
        if not (self.memory_before and self.memory_after):
            return 0.0
        return round(self.memory_before.rss_mb - self.memory_after.rss_mb, 1)


@dataclass(frozen=True)
class Plan:
    ops: tuple[Op, ...] = ()
    skipped: tuple[str, ...] = ()
    """Every exclusion with its reason. A silent skip reads as 'covered' when
    it was not — this is the difference between a report and a claim."""

    @property
    def is_empty(self) -> bool:
        return not self.ops


@dataclass(frozen=True)
class Assignment:
    """One node→material binding, as it stood at snapshot time."""

    node_handle: int
    material_handle: int | None


@dataclass(frozen=True)
class GuardReport:
    """Result of the material guard sweep."""

    checked: int = 0
    lost: tuple[Assignment, ...] = ()
    repaired: tuple[Assignment, ...] = ()
    unrepaired: tuple[Assignment, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.unrepaired


@dataclass(frozen=True)
class Metrics:
    """The measured outcome. Never a prediction."""

    rss_before_mb: float = 0.0
    rss_after_mb: float = 0.0
    vram_before_mb: float | None = None
    vram_after_mb: float | None = None
    polys_before: int = 0
    polys_after: int = 0
    file_mb_before: float = 0.0
    file_mb_after: float = 0.0
    nodes_before: int = 0
    nodes_after: int = 0

    @property
    def rss_freed_mb(self) -> float:
        return round(self.rss_before_mb - self.rss_after_mb, 1)

    @property
    def rss_reduction(self) -> float:
        if self.rss_before_mb <= 0:
            return 0.0
        return self.rss_freed_mb / self.rss_before_mb

    @property
    def poly_reduction(self) -> float:
        if self.polys_before <= 0:
            return 0.0
        return (self.polys_before - self.polys_after) / self.polys_before

    @property
    def measured(self) -> bool:
        """False when no memory sample was available, so the headline must fall
        back to polygons rather than quietly printing 0%."""
        return self.rss_before_mb > 0


@dataclass(frozen=True)
class RunReport:
    """One batch's outcome."""

    results: tuple[OpResult, ...] = ()
    guard: GuardReport = field(default_factory=GuardReport)
    metrics: Metrics = field(default_factory=Metrics)
    backup_path: str | None = None
    halted: bool = False
    cancelled: bool = False
    skipped_candidates: tuple[str, ...] = ()
    log: tuple[str, ...] = ()

    @property
    def applied(self) -> tuple[OpResult, ...]:
        return tuple(r for r in self.results if r.status is OpStatus.APPLIED)

    @property
    def ok(self) -> bool:
        return not self.halted and self.guard.clean


class BatchOutcome(Enum):
    COMPLETED = "completed"
    QUARANTINED = "quarantined"
    HALTED = "halted"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class BatchReport:
    """One merge batch: what went in, what came out, what it cost."""

    index: int
    names: tuple[str, ...]
    outcome: BatchOutcome
    merged: int = 0
    requested: int = 0
    run: RunReport | None = None
    memory_before: MemorySample | None = None
    memory_after_merge: MemorySample | None = None
    """Sampled straight after the merge, BEFORE reduction.

    This — not the end-of-batch figure — is what the merge actually cost, and
    the only honest input to the governor's calibration. Measuring after
    reduction conflates the cost of loading with the saving from reducing, and
    routinely goes negative, which silently teaches the governor nothing."""

    memory_after: MemorySample | None = None
    seconds: float = 0.0
    note: str = ""

    @property
    def shortfall(self) -> int:
        """Objects asked for but not merged. Never silently zero."""
        return max(0, self.requested - self.merged)

    @property
    def merge_cost_mb(self) -> float:
        """What loading this batch cost, before any reduction."""
        if not (self.memory_before and self.memory_after_merge):
            return 0.0
        return round(self.memory_after_merge.rss_mb - self.memory_before.rss_mb, 1)

    @property
    def rss_delta_mb(self) -> float:
        """Net change across the whole batch — usually negative, and that is
        the point."""
        if not (self.memory_before and self.memory_after):
            return 0.0
        return round(self.memory_after.rss_mb - self.memory_before.rss_mb, 1)

    @property
    def peak_rss_mb(self) -> float:
        """The high-water mark, which is what decides whether the machine
        survives — not the tidy figure at the end."""
        samples = [s.rss_mb for s in (self.memory_after_merge, self.memory_after) if s]
        return max(samples) if samples else 0.0
