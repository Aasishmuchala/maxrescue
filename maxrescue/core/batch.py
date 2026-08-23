"""The outer loop: rebuild a scene in batches, reducing as it goes.

    reset  →  for each batch:  merge  →  measure  →  reduce  →  save
                            →  feed the measurement back to the governor
           →  final pass  →  save

Reducing *inside* the loop rather than at the end is the point. Converting a
tree to a proxy while only that batch is resident costs a fraction of doing it
once everything is loaded — and if the whole scene could be loaded at once, none
of this would be necessary.

Failure policy at this level differs from the per-op one, because the stakes
differ:

* **A batch that fails to merge is quarantined** — named in the report, and the
  run continues. One unreadable object should not cost the other 40,000.
* **A batch whose reduction halts stops the run.** A halt means the scene is not
  in the state the plan assumed, and merging more into an unknown state
  compounds the damage. What was completed is saved and named, so the run is
  resumable rather than lost.

The output file is written after every batch. A crash at batch 40 of 50 must not
throw away the first 39.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Callable, Iterator, Sequence

from maxrescue.core.governor import Batch, Governor, MergeCandidate
from maxrescue.core.plan import PlanConfig
from maxrescue.core.session import ProgressEvent, RescueSession
from maxrescue.core.types import BatchOutcome, BatchReport, MemorySample

__all__ = ["BatchRunner", "RescueOutcome"]


@dataclass(frozen=True)
class RescueOutcome:
    batches: tuple[BatchReport, ...] = ()
    output_path: str | None = None
    """None when no usable output was written — never a path that does not exist."""

    halted: bool = False
    save_failed: bool = False

    not_attempted: int = 0
    """Objects the run never reached — because it halted, ran out of headroom,
    or was cancelled.

    Counted explicitly. Without it a run that stopped after two of fifty batches
    reports `merged == requested` and reads as complete, because `requested`
    only ever counted the batches that were attempted."""
    memory_before: MemorySample | None = None
    memory_after: MemorySample | None = None
    log: tuple[str, ...] = ()

    @property
    def merged(self) -> int:
        return sum(b.merged for b in self.batches)

    @property
    def requested(self) -> int:
        return sum(b.requested for b in self.batches)

    @property
    def quarantined(self) -> tuple[BatchReport, ...]:
        return tuple(b for b in self.batches if b.outcome is BatchOutcome.QUARANTINED)

    @property
    def complete(self) -> bool:
        """Every object asked for actually arrived, and nothing halted."""
        return (
            not self.halted
            and not self.save_failed
            and not self.not_attempted
            and self.merged == self.requested
            and not self.quarantined
        )

    @property
    def peak_rss_mb(self) -> float:
        samples = [b.peak_rss_mb for b in self.batches if b.peak_rss_mb]
        return max(samples) if samples else 0.0


@dataclass
class BatchRunner:
    context: object
    governor: Governor
    source_path: str
    output_path: str
    config: PlanConfig = field(default_factory=PlanConfig)
    proxy_dir: str = ""
    reduce_each_batch: bool = True

    def __post_init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    # -- the run ----------------------------------------------------------

    def run(
        self,
        candidates: Sequence[MergeCandidate],
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> Iterator[ProgressEvent]:
        ctx = self.context
        log: list[str] = []
        reports: list[BatchReport] = []
        halted = False
        save_failed = False

        remaining = list(candidates)
        preview = self.governor.plan_all(remaining)
        log.append(self.governor.describe_plan(preview))
        expected = len(preview)

        yield ProgressEvent("plan", f"~{expected} batch(es) planned", 0.01)

        ctx.merge.reset_scene()
        ctx.memory.settle()
        memory_before = ctx.memory.sample()
        log.append(f"empty scene baseline: {memory_before.rss_mb:,.0f} MB resident")

        total = max(1, expected)
        index = 0
        while remaining:
            if self._cancelled:
                log.append("cancelled by request")
                break

            resident = int(ctx.memory.sample().rss_mb * 1024 * 1024)
            if not self.governor.has_headroom(resident):
                halted = True
                log.append(
                    f"! {resident / (1 << 30):.1f} GB already resident against a "
                    f"{self.governor.ram_budget / (1 << 30):.1f} GB ceiling — no "
                    f"room for the remaining {len(remaining):,} object(s). "
                    "Stopping cleanly with what is saved rather than being "
                    "killed mid-merge."
                )
                break

            # Re-plan every time, against BOTH the current calibration and the
            # memory already held. Planning up front sizes every batch after the
            # first with a prior that measurement has already disproved.
            batch, remaining = self.governor.next_batch(remaining, resident)
            if batch is None:
                break
            index += 1

            base = 0.02 + 0.94 * (min(index - 1, total) / total)
            yield ProgressEvent(
                "merge",
                f"batch {index} — {len(batch.names):,} objects",
                base,
            )

            report, batch_halted, batch_log = self._run_batch(index, batch)
            reports.append(report)
            log.extend(batch_log)

            # Teach the governor what this batch actually cost, so later
            # batches are sized from measurement rather than the prior.
            if report.merged:
                # The MERGE cost, not the net change. The net is normally
                # negative because reduction ran in between, and a negative
                # sample teaches the governor nothing.
                self.governor.observe(
                    batch_weight=batch.weight,
                    rss_delta=int(report.merge_cost_mb * 1024 * 1024),
                )

            # Save after every batch — a crash at batch 40 of 50 must not throw
            # away the first 39.
            if report.outcome is not BatchOutcome.QUARANTINED:
                if not ctx.merge.save_as(self.output_path):
                    # Silence here is the worst outcome available: the run
                    # continues for hours and reports success while naming an
                    # output file that is stale or absent.
                    halted = True
                    save_failed = True
                    log.append(
                        f"! batch {index}: saving to {self.output_path} FAILED. "
                        "Stopping — continuing would spend hours producing "
                        "nothing. Check free space and permissions."
                    )
                    reports[-1] = replace(report, outcome=BatchOutcome.HALTED)
                    break

            if batch_halted:
                halted = True
                log.append(
                    f"batch {index} halted — stopping. {index - 1} batch(es) are "
                    f"saved in {self.output_path}; re-run excluding this batch."
                )
                break

        # NOT gated on reduce_each_batch: that flag chooses WHEN reduction
        # happens, not WHETHER. Gating both on it meant switching it off made the
        # tool merge the scene, change nothing, and report memory figures as if
        # it had.
        if not halted and not self._cancelled:
            yield ProgressEvent("final", "final pass over the whole scene", 0.96)
            final, final_log = self._reduce("final pass")
            log.extend(final_log)
            if final is not None and final.halted:
                halted = True

        if not ctx.merge.save_as(self.output_path):
            save_failed = True
            halted = True
            log.append(f"! final save to {self.output_path} FAILED")
        ctx.memory.settle()
        memory_after = ctx.memory.sample()

        if remaining:
            log.append(
                f"! {len(remaining):,} object(s) were never attempted — they are "
                "NOT in the output."
            )

        outcome = RescueOutcome(
            batches=tuple(reports),
            not_attempted=len(remaining),
            output_path=None if save_failed else self.output_path,
            halted=halted,
            save_failed=save_failed,
            memory_before=memory_before,
            memory_after=memory_after,
            log=tuple(log + [self._headline(reports, memory_after)]),
        )
        # Set BEFORE the final yield: a consumer that breaks out of the loop on
        # the "done" event would otherwise find `outcome` unset and lose every
        # batch report.
        self.outcome = outcome
        yield ProgressEvent("done", "finished", 1.0, report=None)

    # -- one batch --------------------------------------------------------

    def _run_batch(self, index: int, batch: Batch) -> tuple[BatchReport, bool, list[str]]:
        ctx = self.context
        log: list[str] = []
        started = time.time()

        ctx.memory.settle()
        before = ctx.memory.sample()

        try:
            merged, problems = ctx.merge.merge(self.source_path, batch.names)
        except Exception as exc:
            log.append(f"! batch {index} could not be merged — {exc}; quarantined")
            return (
                BatchReport(
                    index=index,
                    names=batch.names,
                    outcome=BatchOutcome.QUARANTINED,
                    requested=len(batch.names),
                    memory_before=before,
                    memory_after_merge=before,
                    memory_after=before,
                    seconds=round(time.time() - started, 2),
                    note=str(exc),
                ),
                False,
                log,
            )

        ctx.memory.settle()
        after_merge = ctx.memory.sample()
        log.append(
            f"batch {index}: merged {len(merged):,}/{len(batch.names):,} "
            f"(+{after_merge.rss_mb - before.rss_mb:,.0f} MB)"
        )
        for problem in problems[:10]:
            log.append(f"    ! {problem}")
        if len(problems) > 10:
            log.append(f"    ! …and {len(problems) - 10:,} more")

        if not merged:
            log.append(f"! batch {index} merged nothing — quarantined")
            return (
                BatchReport(
                    index=index,
                    names=batch.names,
                    outcome=BatchOutcome.QUARANTINED,
                    requested=len(batch.names),
                    memory_before=before,
                    memory_after_merge=after_merge,
                    memory_after=after_merge,
                    seconds=round(time.time() - started, 2),
                    note="merge returned no objects",
                ),
                False,
                log,
            )

        run = None
        halted = False
        if self.reduce_each_batch:
            run, run_log = self._reduce(f"batch {index}")
            log.extend(run_log)
            halted = bool(run and run.halted)

        ctx.memory.settle()
        after = ctx.memory.sample()

        return (
            BatchReport(
                index=index,
                names=batch.names,
                outcome=BatchOutcome.HALTED if halted else BatchOutcome.COMPLETED,
                merged=len(merged),
                requested=len(batch.names),
                run=run,
                memory_before=before,
                memory_after_merge=after_merge,
                memory_after=after,
                seconds=round(time.time() - started, 2),
                note=batch.note or "",
            ),
            halted,
            log,
        )

    def _reduce(self, label: str):
        """Reduce whatever is currently in the scene.

        `requires_backup=False`: the source file on disk is untouched and is
        itself the backup, so a second copy per batch would be pure cost.
        """
        session = RescueSession(
            self.context,
            self.config,
            requires_backup=False,
            proxy_dir=self.proxy_dir,
        )
        report = None
        for event in session.run_chunks():
            if event.report:
                report = event.report
        if report is None:
            return None, [f"{label}: reduction produced no report"]
        lines = [f"  {label}: {line}" for line in report.log if line.startswith(("  +", "  !", "measured"))]
        return report, lines

    @staticmethod
    def _headline(reports: list[BatchReport], after: MemorySample) -> str:
        merged = sum(r.merged for r in reports)
        requested = sum(r.requested for r in reports)
        peak = max((r.peak_rss_mb for r in reports), default=0)
        peak_text = f"{peak:,.0f} MB" if peak else "not measured"
        return (
            f"merged {merged:,}/{requested:,} objects · peak {peak_text} resident "
            f"during the run · {after.rss_mb:,.0f} MB at the end"
        )
