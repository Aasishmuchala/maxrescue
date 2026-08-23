"""One reduction run: the choreography that keeps a scene intact.

The order is the safety contract, and it is not negotiable:

    guard snapshot  →  backup  →  measure  →  per-op transaction
                    →  verify each op immediately
                    →  halt or skip
                    →  guard sweep (ALWAYS, even after a halt)
                    →  measure  →  report

Three policies decide what happens when something goes wrong, and the
distinction between them is the whole design:

**Skip** — the op's target no longer exists, or its precondition no longer
holds. The run continues. Any exception raised while *checking* also degrades to
a skip, never to a blind apply.

**Halt** — an op failed or was rolled back. Later ops are never attempted,
because a failure means the scene is not in the state the plan assumed.

**Cancel** — the caller asked to stop. Applied work stays, the rest is marked
cancelled, and the guard still runs.

The guard sweep runs in all three cases, and the pins are released in a
`finally`, because a halt is exactly when material bindings are most likely to
be missing and least likely to be checked.

Pure — drives everything through the ports in `core.interfaces`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from maxrescue.core.plan import PlanConfig, build_plan
from maxrescue.core.types import (
    Assignment,
    GuardReport,
    Metrics,
    Op,
    OpResult,
    OpStatus,
    Plan,
    RunReport,
    Stage,
)

__all__ = ["ProgressEvent", "RescueSession"]


@dataclass(frozen=True)
class ProgressEvent:
    phase: str
    message: str
    fraction: float
    report: RunReport | None = None


@dataclass
class RescueSession:
    context: object
    config: PlanConfig = field(default_factory=PlanConfig)
    requires_backup: bool = True
    """False when the open scene was just built by merging and the source file
    on disk is itself the backup. Never False for a scene the user opened."""

    proxy_dir: str = ""

    gate: object = None
    """A callable returning a `VerifyResult`, used to check the one stage that
    is not render-identical.

    Without it the BITMAP stage is SKIPPED rather than applied. It previously
    ran unconditionally while the plan advertised "gated by an image diff; the
    whole stage reverts on failure" — there was no diff and no revert, which is
    a worse position than having no gate at all, because the claim was on the
    record."""

    def __post_init__(self) -> None:
        self._cancelled = False
        #: old handle -> new handle, for nodes REPLACED rather than removed.
        self._replacements: dict[int, int] = {}
        #: handles this run deliberately deleted.
        self._removed: set[int] = set()

    def cancel(self) -> None:
        self._cancelled = True

    # -- the run ----------------------------------------------------------

    def run_chunks(self) -> Iterator[ProgressEvent]:
        ctx = self.context
        log: list[str] = []
        results: list[OpResult] = []
        backup_path: str | None = None
        halted = False

        yield ProgressEvent("plan", "reading the scene", 0.02)
        nodes = list(ctx.query.nodes())
        engine = ctx.query.engine()
        render_hidden = ctx.query.render_hidden()

        plan: Plan = build_plan(nodes, self.config, engine, render_hidden)
        log.append(f"planned {len(plan.ops)} operation(s) over {len(nodes):,} node(s)")
        for line in plan.skipped:
            log.append(f"  skipped: {line}")

        yield ProgressEvent("plan", f"{len(plan.ops)} operations planned", 0.06)

        # Snapshot BEFORE any mutation, and before the backup, so a backup
        # failure still leaves us able to describe the scene.
        snapshot: tuple[Assignment, ...] = ctx.guard.snapshot()
        log.append(f"pinned {len(snapshot)} material assignment(s)")

        try:
            if self.requires_backup:
                yield ProgressEvent("backup", "saving a backup", 0.08)
                try:
                    backup_path = ctx.backup.create()
                    log.append(f"backup: {backup_path}")
                except Exception as exc:
                    log.append(f"! backup FAILED — {exc}; nothing was modified")
                    report = RunReport(
                        log=tuple(log), halted=True, skipped_candidates=plan.skipped
                    )
                    yield ProgressEvent("done", "aborted: backup failed", 1.0, report)
                    return

            ctx.memory.settle()
            before_stats = ctx.query.stats()
            before_memory = ctx.memory.sample()

            total = max(1, len(plan.ops))
            for index, op in enumerate(plan.ops):
                if self._cancelled:
                    results.extend(
                        OpResult(o.id, o.stage, OpStatus.CANCELLED) for o in plan.ops[index:]
                    )
                    log.append("cancelled by request")
                    break

                fraction = 0.10 + 0.72 * (index / total)
                yield ProgressEvent(op.stage.label, op.title, fraction)

                if op.stage is Stage.BITMAP and self.gate is None:
                    results.append(
                        OpResult(
                            op.id,
                            op.stage,
                            OpStatus.SKIPPED,
                            "no render-identity gate available — this stage is "
                            "not render-identical and will not run unchecked",
                        )
                    )
                    log.append(f"  ~ {op.title} — skipped: no gate available")
                    continue

                reason = self._still_applicable(op)
                if reason:
                    results.append(
                        OpResult(op.id, op.stage, OpStatus.SKIPPED, reason)
                    )
                    log.append(f"  ~ {op.title} — skipped: {reason}")
                    continue

                result = self._apply(op, before=ctx.memory.sample())
                results.append(result)
                if result.status is OpStatus.APPLIED:
                    log.append(f"  + {op.title}")
                else:
                    log.append(f"  ! {op.title} — {result.message}")
                    halted = True
                    log.append(
                        "run halted; the scene is as the last successful operation "
                        f"left it{f'. Backup: {backup_path}' if backup_path else ''}"
                    )
                    break

            # -- guard sweep: always, including after a halt
            yield ProgressEvent("guard", "re-checking material assignments", 0.86)
            guard = self._sweep(snapshot)
            log.extend(self._guard_log(guard))

            yield ProgressEvent("measure", "measuring the result", 0.92)
            ctx.memory.settle()
            after_stats = ctx.query.stats()
            after_memory = ctx.memory.sample()

            metrics = Metrics(
                rss_before_mb=before_memory.rss_mb,
                rss_after_mb=after_memory.rss_mb,
                vram_before_mb=before_memory.vram_mb,
                vram_after_mb=after_memory.vram_mb,
                polys_before=before_stats.polygons,
                polys_after=after_stats.polygons,
                file_mb_before=before_stats.file_mb,
                file_mb_after=after_stats.file_mb,
                nodes_before=before_stats.nodes,
                nodes_after=after_stats.nodes,
            )
            log.append(self._headline(metrics))

            report = RunReport(
                results=tuple(results),
                guard=guard,
                metrics=metrics,
                backup_path=backup_path,
                halted=halted,
                cancelled=self._cancelled,
                skipped_candidates=plan.skipped,
                log=tuple(log),
            )
            yield ProgressEvent("done", "finished", 1.0, report)
        finally:
            # Pins must not leak, whatever happened above.
            ctx.guard.release()

    # -- op handling ------------------------------------------------------

    def _still_applicable(self, op: Op) -> str | None:
        """Why this op can no longer run, or None.

        An earlier op may have deleted this op's target — proxy conversion
        replaces a node with a different handle, which invalidates every later
        op aimed at the old one. That is a SKIP, never a halt: the sibling
        project halted whole sessions on it.
        """
        try:
            if not op.targets:
                return None
            missing = [h for h in op.targets if not self.context.query.node_exists(h)]
            if len(missing) == len(op.targets):
                return "target no longer exists"
            return None
        except Exception as exc:
            # Never let a failed pre-check turn into a blind apply.
            return f"could not verify the target ({exc})"

    def _apply(self, op: Op, before) -> OpResult:
        ctx = self.context
        try:
            with ctx.undo.transaction(op.title):
                self._dispatch(op)
        except Exception as exc:
            return OpResult(op.id, op.stage, OpStatus.FAILED, str(exc), memory_before=before)

        try:
            verified = self._verify(op)
        except Exception as exc:
            return OpResult(
                op.id, op.stage, OpStatus.FAILED, f"verification failed: {exc}",
                memory_before=before,
            )

        if not verified:
            return OpResult(
                op.id, op.stage, OpStatus.FAILED,
                "the operation reported success but could not be verified",
                memory_before=before,
            )

        ctx.memory.settle()
        return OpResult(
            op.id,
            op.stage,
            OpStatus.APPLIED,
            verified=True,
            memory_before=before,
            memory_after=ctx.memory.sample(),
        )

    def _dispatch(self, op: Op) -> None:
        fixes = self.context.fixes

        if op.stage is Stage.HYGIENE:
            {
                "hygiene.bitmaps": fixes.free_scene_bitmaps,
                "hygiene.undo": fixes.clear_undo_buffer,
                "hygiene.materials": fixes.purge_unused_materials,
                "hygiene.mixer": fixes.strip_motion_mixer,
                "hygiene.gc": fixes.collect_garbage,
            }[op.id]()

        elif op.stage is Stage.HIDDEN:
            for handle in op.targets:
                if self.context.query.node_exists(handle):
                    fixes.delete_node(handle)
                    self._removed.add(handle)

        elif op.stage is Stage.COLLAPSE:
            for handle in op.targets:
                fixes.collapse_stack(handle)

        elif op.stage is Stage.PROXY:
            for handle in op.targets:
                new_handle = fixes.convert_to_proxy(handle, self.proxy_dir)
                if new_handle:
                    # Conversion REPLACES the node, so the guard must follow the
                    # binding to the new handle. Without this mapping the old
                    # handle simply vanishes and the guard skips it as
                    # "intentionally removed" — leaving the stage most likely to
                    # lose a material the one stage the guard cannot see.
                    self._replacements[handle] = new_handle
                    fixes.set_proxy_display(new_handle, 0)

        elif op.stage is Stage.VIEWPORT:
            if op.id == "viewport.bitmap_flush":
                fixes.set_bitmap_proxy_mode(op.payload["mode"])
            elif op.id == "viewport.nitrous":
                fixes.set_nitrous_texture_limit(op.payload["pixels"])
            elif op.id == "viewport.scatter":
                for handle in op.targets:
                    fixes.set_scatter_display(handle, "points")

        elif op.stage is Stage.BITMAP:
            fixes.convert_bitmaps_to_vray()
            # The gate runs INSIDE the undo transaction, so a failing diff
            # raises and the whole conversion is rolled back rather than left
            # in place with a warning nobody reads.
            result = self.gate()
            if not getattr(result, "passed", False):
                raise RuntimeError(
                    "bitmap conversion changed the render beyond the allowed "
                    f"tolerance ({getattr(result, 'summary', 'no detail')}) — "
                    "the whole stage has been rolled back"
                )

    def _verify(self, op: Op) -> bool:
        query = self.context.query

        if op.stage is Stage.HIDDEN:
            return all(not query.node_exists(h) for h in op.targets)
        if op.stage is Stage.PROXY:
            # The original must be gone. If it survived, the export produced a
            # duplicate rather than a replacement.
            return all(not query.node_exists(h) for h in op.targets)
        if op.stage is Stage.COLLAPSE:
            for handle in op.targets:
                remaining = query.modifier_count(handle)
                # Unknown is not success. A node whose stack cannot be read has
                # not been shown to be collapsed.
                if remaining is None or remaining > 0:
                    return False
            return True
        # Hygiene and viewport ops have no per-object post-condition worth
        # asserting; their effect is measured, not counted.
        return True

    # -- guard ------------------------------------------------------------

    def _sweep(self, snapshot: tuple[Assignment, ...]) -> GuardReport:
        """Compare every pinned binding against the scene as it now stands.

        Three cases, and keeping them apart is what makes the guard mean
        anything:

        * **Replaced** — proxy conversion swapped the node for a new handle. The
          binding must be checked on the NEW node. Treating the old handle as
          simply gone would blind the guard to the one stage most likely to drop
          a material.
        * **Deliberately removed** — the hidden-object stage deleted it. Not a
          loss.
        * **Vanished** — gone, and nothing in this run says it should be. That is
          a loss, and previously it was silently skipped alongside the
          intentional deletions.
        """
        ctx = self.context
        before = {a.node_handle: a.material_handle for a in snapshot}
        now = {a.node_handle: a.material_handle for a in ctx.guard.current()}

        lost: list[Assignment] = []
        repaired: list[Assignment] = []
        unrepaired: list[Assignment] = []

        def record(handle: int, material: int) -> None:
            lost.append(Assignment(handle, material))
            if ctx.guard.reattach(handle, material):
                repaired.append(Assignment(handle, material))
            else:
                unrepaired.append(Assignment(handle, material))

        for handle, material in before.items():
            if material is None:
                continue

            replacement = self._replacements.get(handle)
            if replacement is not None:
                if now.get(replacement) != material:
                    record(replacement, material)
                continue

            if handle in self._removed:
                continue  # deleted on purpose

            if handle not in now:
                # Vanished without this run asking for it — a group-select
                # cascade, or a plugin taking siblings with it.
                record(handle, material)
                continue

            if now[handle] != material:
                record(handle, material)

        return GuardReport(
            checked=len(before),
            lost=tuple(lost),
            repaired=tuple(repaired),
            unrepaired=tuple(unrepaired),
        )

    @staticmethod
    def _guard_log(guard: GuardReport) -> list[str]:
        if not guard.lost:
            return [f"material guard: all {guard.checked:,} assignment(s) intact"]
        lines = [
            f"material guard: {len(guard.lost)} assignment(s) went missing, "
            f"{len(guard.repaired)} re-attached"
        ]
        if guard.unrepaired:
            lines.append(
                f"! {len(guard.unrepaired)} assignment(s) could NOT be restored — "
                "compare against the backup before using this scene"
            )
        return lines

    @staticmethod
    def _headline(metrics: Metrics) -> str:
        if metrics.measured:
            return (
                f"measured: {metrics.rss_before_mb:,.0f} MB → "
                f"{metrics.rss_after_mb:,.0f} MB resident "
                f"({metrics.rss_reduction:.1%} freed)"
            )
        return (
            f"measured: {metrics.polys_before:,} → {metrics.polys_after:,} polygons "
            f"({metrics.poly_reduction:.1%}); no memory sample was available"
        )
