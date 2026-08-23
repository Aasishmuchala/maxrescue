"""What machine will this scene run on?

The whole point of a rescue is being able to answer that afterwards. But the
answer has to keep two very different statements apart, because whoever reads it
will quote the bolder one:

* **Measured** — the rescued scene occupied N GB when it was opened on the
  machine that ran the rescue. A fact.
* **Recommended** — a machine size derived from that measurement by a rule
  written out in full. An argument, and it says so.

Opening a scene is the cheap half. V-Ray builds its own copy of the geometry on
top of Max's, so a machine sized to the open figure runs out during the first
render. Chaos's own example: a scene sitting at 20 GB idle peaked at 49 GB while
rendering, with V-Ray's tracker reporting only 15 GB of that — the rest was
Max. That is where the multiplier below comes from, and it is stated rather than
buried so it can be argued with.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["MachineRequirements", "RENDER_MULTIPLIER", "assess"]

#: Render peak ÷ open footprint, from Chaos's 20 GB idle → 49 GB rendering
#: example. Applied to the peak rather than the resting figure.
RENDER_MULTIPLIER = 2.5

#: Machine sizes people can actually buy. Recommending 93 GB helps nobody.
_REAL_SIZES = (16, 32, 64, 128, 256, 512, 1024)

_GB = 1024.0


def _round_up_to_real_size(gigabytes: float) -> int:
    for size in _REAL_SIZES:
        if gigabytes <= size:
            return size
    return _REAL_SIZES[-1]


@dataclass(frozen=True)
class MachineRequirements:
    measured: bool
    open_rss_gb: float
    peak_rss_gb: float
    vram_gb: float | None
    recommended_ram_gb: int | None
    recommended_vram_gb: int | None
    summary: str
    reasoning: str

    def fits_in(self, ram_gb: int) -> bool:
        """Whether a machine of this size can open AND render the scene."""
        if not self.measured or self.recommended_ram_gb is None:
            return False
        return ram_gb >= self.recommended_ram_gb

    def verdict_for(self, ram_gb: int) -> str:
        """A sentence, not a boolean — this is what someone reads before
        deciding whether to buy a machine."""
        if not self.measured:
            return (
                f"Whether a {ram_gb} GB machine copes was not measured, so there "
                "is nothing to report."
            )
        needed = self.recommended_ram_gb
        if ram_gb >= (needed or 0):
            return (
                f"A {ram_gb} GB machine handles this comfortably — it opens at "
                f"{self.open_rss_gb:,.1f} GB and needs about {needed} GB to "
                "render with room to spare."
            )
        return (
            f"A {ram_gb} GB machine is NOT enough. It opens at "
            f"{self.open_rss_gb:,.1f} GB, but rendering needs roughly "
            f"{needed} GB — it would swap or die partway through."
        )


def assess(
    open_rss_mb: float,
    peak_rss_mb: float,
    vram_mb: float | None = None,
) -> MachineRequirements:
    """Turn measured memory into a machine recommendation.

    A run with no usable sample recommends **nothing**. A number nobody can
    trace back to a measurement is worse than silence, because it will be
    repeated.
    """
    if open_rss_mb <= 0 and peak_rss_mb <= 0:
        return MachineRequirements(
            measured=False,
            open_rss_gb=0.0,
            peak_rss_gb=0.0,
            vram_gb=None,
            recommended_ram_gb=None,
            recommended_vram_gb=None,
            summary=(
                "Memory was not measured during this run, so no machine "
                "requirement can be stated."
            ),
            reasoning="",
        )

    open_gb = max(open_rss_mb, 0.0) / _GB
    peak_gb = max(peak_rss_mb, open_rss_mb, 0.0) / _GB

    # Size on the PEAK, not the resting figure: the peak is what has to fit.
    needed_gb = peak_gb * RENDER_MULTIPLIER
    recommended = _round_up_to_real_size(needed_gb)

    vram_gb = (vram_mb / _GB) if vram_mb else None
    # A third again on top, so a heavier camera angle does not tip it over.
    recommended_vram = (
        _round_up_to_real_size(vram_gb * 1.33) if vram_gb else None
    )

    summary = (
        f"Measured: opens at {open_gb:,.1f} GB"
        + (f", peaked at {peak_gb:,.1f} GB during the rescue" if peak_gb > open_gb else "")
        + f". Recommended machine: {recommended} GB RAM"
        + (f", {recommended_vram} GB VRAM" if recommended_vram else "")
        + "."
    )

    reasoning = (
        f"Opening is the cheap half. V-Ray builds its own copy of the geometry "
        f"on top of Max's, so the render peak runs about ×{RENDER_MULTIPLIER} "
        f"the open footprint (Chaos's own example: 20 GB idle → 49 GB "
        f"rendering). {peak_gb:,.1f} GB × {RENDER_MULTIPLIER} ≈ "
        f"{needed_gb:,.0f} GB, rounded up to the next real machine size."
    )

    return MachineRequirements(
        measured=True,
        open_rss_gb=round(open_gb, 1),
        peak_rss_gb=round(peak_gb, 1),
        vram_gb=round(vram_gb, 1) if vram_gb else None,
        recommended_ram_gb=recommended,
        recommended_vram_gb=recommended_vram,
        summary=summary,
        reasoning=reasoning,
    )
