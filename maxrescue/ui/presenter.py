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

from maxrescue.xray.report import Verdict, XrayReport

__all__ = ["AppState", "Fact", "Phase", "ViewModel", "present"]


class Phase(Enum):
    EMPTY = "empty"
    SURVEYED = "surveyed"


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


@dataclass(frozen=True)
class AppState:
    """Everything the window knows."""

    path: str | None = None
    survey: XrayReport | None = None


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


def present(state: AppState) -> ViewModel:
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
