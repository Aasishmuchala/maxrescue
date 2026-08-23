"""What the window says — pure, so the wording can be tested.

The Qt layer draws this and nothing else. Every sentence the user reads about a
scene that will not open is decided here, which means the phrasing of a failure
gets the same scrutiny as the logic that produced it.

The audience is an artist with a scene that will not open, not the author of a
chunk parser. So nothing on screen says `single-monster-object`; it says *one
object is most of this scene, and here is its name*.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from maxrescue.core.requirements import assess
from maxrescue.xray.report import Verdict, XrayReport

__all__ = [
    "AppState",
    "Fact",
    "Outcome",
    "Phase",
    "Progress",
    "ViewModel",
    "present",
]


class Phase(Enum):
    EMPTY = "empty"
    SURVEYED = "surveyed"
    RUNNING = "running"
    DONE = "done"


@dataclass(frozen=True)
class Progress:
    fraction: float = 0.0
    message: str = ""
    resident_mb: float = 0.0


@dataclass(frozen=True)
class Outcome:
    """What the run produced. Every field is measured, none inferred."""

    output_path: str = ""
    open_rss_mb: float = 0.0
    peak_rss_mb: float = 0.0
    vram_mb: float | None = None
    objects_merged: int = 0
    objects_requested: int = 0
    textures_reduced: int = 0
    verified: bool | None = None
    """True = renders proved identical · False = they differ · None = not checked.
    The three are kept apart because 'not checked' must never read as 'fine'."""

    problems: tuple[str, ...] = ()

    @property
    def shortfall(self) -> int:
        return max(0, self.objects_requested - self.objects_merged)


@dataclass(frozen=True)
class Fact:
    """One label/value row. The answers to what an artist asks first."""

    label: str
    value: str


@dataclass(frozen=True)
class ViewModel:
    phase: Phase
    headline: str
    detail: str = ""
    facts: tuple[Fact, ...] = ()
    warnings: tuple[str, ...] = ()
    can_rescue: bool = False
    rescue_label: str = ""
    progress: float | None = None


@dataclass(frozen=True)
class AppState:
    """Everything the window knows."""

    path: str | None = None
    survey: XrayReport | None = None
    progress: Progress | None = None
    outcome: Outcome | None = None


def _size(value: float) -> str:
    if value >= 1 << 30:
        return f"{value / (1 << 30):,.1f} GB"
    if value >= 1 << 20:
        return f"{value / (1 << 20):,.0f} MB"
    return f"{value:,.0f} B"


def _diagnosis(survey: XrayReport) -> tuple[str, str]:
    """Headline and detail, in the words an artist would use.

    The X-ray's own observations are already written as sentences, so the detail
    line reuses them rather than inventing a second vocabulary that could drift
    away from what the report says.
    """
    first = survey.observations[0] if survey.observations else ""

    if survey.verdict == Verdict.MALWARE:
        return ("This file contains a known malicious script", first)
    if survey.verdict == Verdict.DAMAGED:
        return ("Part of this file could not be read", first)
    if survey.verdict == Verdict.MONSTER_OBJECT:
        return ("One object is most of this scene", first)
    if survey.verdict == Verdict.BLOAT:
        return ("The weight is spread across a great many objects", first)
    return ("Nothing obviously wrong with this file", first or
            "No single culprit — a rescue will still shed viewport and cache weight.")


def _facts(survey: XrayReport) -> tuple[Fact, ...]:
    inventory = survey.inventory
    plugins = survey.required_dlls
    nodes = survey.nodes

    rows = [
        Fact("Size on disk", _size(survey.file_bytes)),
        Fact("Objects", f"{len(inventory.objects):,}"),
    ]
    if nodes.total_nodes:
        named = f"{nodes.resolved_count:,} of {nodes.total_nodes:,} named"
        if nodes.resolution_rate < 0.9:
            named += f" ({nodes.resolution_rate:.0%} — some names unreadable)"
        rows.append(Fact("Nodes", named))
    rows.append(
        Fact("Plugins needed", ", ".join(plugins) if plugins else "none declared")
    )
    heaviest = inventory.heaviest(1)
    if heaviest:
        owner = nodes.owner_of(heaviest[0].position)
        name = owner.name if owner and owner.name else f"object #{heaviest[0].position}"
        rows.append(Fact("Heaviest", f"{name} — {_size(heaviest[0].bytes)}"))
    return tuple(rows)


def _warnings(survey: XrayReport) -> tuple[str, ...]:
    out: list[str] = []
    if survey.malware:
        names = sorted({f.signature for f in survey.malware})
        out.append(
            "A known malicious script signature is embedded in this file "
            f"({', '.join(names)}). Clean it before opening it anywhere."
        )
    if survey.inventory.truncated:
        out.append(
            "The scene index stops partway through, so these counts are a "
            "floor rather than a total."
        )
    scripted = survey.catalog.scripted_classes()
    if scripted and not survey.malware:
        out.append(
            f"{len(scripted)} MAXScript-defined class(es) are embedded. Nothing "
            "matched a known payload, but that is worth a look."
        )
    return tuple(out)


def _running(state: AppState) -> ViewModel:
    progress = state.progress
    facts = [
        Fact("Doing", progress.message or "working"),
    ]
    if progress.resident_mb:
        facts.append(Fact("Memory in use", f"{progress.resident_mb / 1024:,.1f} GB"))
    return ViewModel(
        phase=Phase.RUNNING,
        headline="Rescuing this scene",
        detail=progress.message or "working",
        facts=tuple(facts),
        # Starting again would merge everything a second time.
        can_rescue=False,
        rescue_label="Working…",
        progress=progress.fraction,
    )


def _done(state: AppState) -> ViewModel:
    outcome = state.outcome
    requirements = assess(
        open_rss_mb=outcome.open_rss_mb,
        peak_rss_mb=outcome.peak_rss_mb,
        vram_mb=outcome.vram_mb,
    )

    if outcome.verified is True:
        headline = "Done — and the renders are identical"
    elif outcome.verified is False:
        headline = "Done, but the renders DIFFER"
    else:
        headline = "Done"

    facts = [Fact("Saved to", outcome.output_path or "—")]
    if requirements.measured:
        facts.append(
            Fact("Opens at", f"{requirements.open_rss_gb:,.1f} GB (measured here)")
        )
        facts.append(
            Fact(
                "Runs comfortably on",
                f"{requirements.recommended_ram_gb} GB RAM"
                + (
                    f" · {requirements.recommended_vram_gb} GB VRAM"
                    if requirements.recommended_vram_gb
                    else ""
                ),
            )
        )
    facts.append(
        Fact("Objects", f"{outcome.objects_merged:,} of {outcome.objects_requested:,}")
    )
    if outcome.textures_reduced:
        facts.append(
            Fact(
                "Textures reduced",
                f"{outcome.textures_reduced:,} — originals kept, only the links moved",
            )
        )

    warnings: list[str] = []
    if outcome.verified is False:
        warnings.append(
            "The rescued scene does not render identically to the reference. "
            "Do not ship this file — send the report and both frames."
        )
    elif outcome.verified is None:
        warnings.append(
            "Renders were NOT verified — no reference frame was supplied, so "
            "nothing here proves the image is unchanged."
        )
    if outcome.shortfall:
        warnings.append(
            f"{outcome.shortfall:,} object(s) never arrived and are NOT in the "
            "output file."
        )
    warnings.extend(outcome.problems)

    return ViewModel(
        phase=Phase.DONE,
        headline=headline,
        detail=(
            f"{requirements.summary} {requirements.reasoning}"
            if requirements.measured
            else f"Saved to {outcome.output_path}."
        ),
        facts=tuple(facts),
        warnings=tuple(warnings),
        can_rescue=False,
        rescue_label="Rescue another scene",
        progress=1.0,
    )


def present(state: AppState) -> ViewModel:
    if state.outcome is not None:
        return _done(state)
    if state.progress is not None and state.survey is not None:
        return _running(state)
    if state.survey is None:
        return ViewModel(
            phase=Phase.EMPTY,
            headline="Drop a .max file here",
            detail="or click to browse. Nothing is opened in 3ds Max — this "
            "reads the file directly, so it works on scenes that crash on open.",
        )

    survey = state.survey
    headline, detail = _diagnosis(survey)
    warnings = _warnings(survey)
    infected = bool(survey.malware)

    return ViewModel(
        phase=Phase.SURVEYED,
        headline=headline,
        detail=detail,
        facts=_facts(survey),
        warnings=warnings,
        # Merging a compromised scene into a clean session would spread it.
        can_rescue=not infected,
        rescue_label="Cannot rescue an infected file" if infected else "Rescue this scene",
    )
