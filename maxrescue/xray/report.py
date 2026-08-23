"""The X-ray report: what a `.max` file contains, without opening 3ds Max.

Deliberately reports **only what was measured**. On-disk chunk weights are
facts; in-memory weight is not, because the multiplier from disk bytes to RAM
depends on class, modifier-stack depth and instancing, and nothing here has been
calibrated against a real scene yet. Spike S3 measures that on the box. Until
then this report gives disk facts and says so — a made-up RAM figure would be
the most quotable and least true number on the page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from maxrescue.xray.directories import (
    ClassCatalog,
    parse_class_directory,
    parse_dll_directory,
)
from maxrescue.xray.nodes import NodeGraph, build_node_graph
from maxrescue.xray.ole import MaxFile, StreamInfo
from maxrescue.xray.scene_walk import SceneInventory, walk_scene
from maxrescue.xray.signatures import Finding, Severity, scan_max_file

__all__ = ["Verdict", "XrayReport", "xray"]

#: What makes one object "the problem" rather than merely the largest.
#:
#: A share of the total is not enough on its own: in a four-object scene every
#: object is 25% and nothing is wrong. So an object must also tower over the
#: *typical* object. That ratio is scale-free — it behaves the same in a scene
#: of ten objects and one of a hundred thousand.
_MONSTER_SHARE = 0.25
_MONSTER_VS_MEDIAN = 20
_MONSTER_MIN_OBJECTS = 3

#: Streams worth scanning for embedded scripts. Scanning everything on a
#: multi-GB file costs minutes for no extra signal.
_SCRIPT_BEARING = (
    "scriptedcustattribdefs",
    "scene",
    "config",
    "classdata",
)


class Verdict:
    MALWARE = "malware"
    DAMAGED = "damaged"
    MONSTER_OBJECT = "single-monster-object"
    BLOAT = "bloat"
    HEALTHY = "healthy"


@dataclass(frozen=True)
class XrayReport:
    path: str
    file_bytes: int
    streams: tuple[StreamInfo, ...]
    catalog: ClassCatalog
    inventory: SceneInventory
    nodes: NodeGraph
    findings: tuple[Finding, ...]
    verdict: str
    observations: tuple[str, ...]

    # -- derived ----------------------------------------------------------

    @property
    def malware(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.MALWARE)

    @property
    def suspicious(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.SUSPICIOUS)

    @property
    def required_dlls(self) -> list[str]:
        return [d.filename for d in self.catalog.referenced_dlls()]

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        inv = self.inventory
        return {
            "path": self.path,
            "file_bytes": self.file_bytes,
            "verdict": self.verdict,
            "observations": list(self.observations),
            "streams": [
                {"name": s.name, "bytes": s.size, "compressed": s.compressed}
                for s in self.streams
            ],
            "plugins": {
                "required_dlls": self.required_dlls,
                "all_declared_dlls": [d.filename for d in self.catalog.dlls],
                "scripted_classes": [
                    c.name or f"<class {c.index}>"
                    for c in self.catalog.scripted_classes()
                ],
            },
            "scene": {
                "objects": len(inv.objects),
                "object_bytes": inv.total_bytes,
                "geometry_bytes": inv.geometry_bytes,
                "unaccounted_bytes": inv.unaccounted_bytes,
                "truncated": inv.truncated,
                "error": inv.error,
                "histogram": [
                    {"class": r.class_name, "count": r.count, "bytes": r.bytes}
                    for r in inv.histogram()
                ],
                "heaviest": [
                    {
                        "position": o.position,
                        "class": o.class_name,
                        "bytes": o.bytes,
                    }
                    for o in inv.heaviest(20)
                ],
            },
            "nodes": {
                "total": self.nodes.total_nodes,
                "resolved": self.nodes.resolved_count,
                "resolution_rate": round(self.nodes.resolution_rate, 4),
                "heaviest": [
                    {
                        "name": n.name,
                        "position": n.position,
                        "object_class": n.object_class,
                        "base_object_class": n.base_object_class,
                        "modifiers": list(n.modifiers),
                        "object_bytes": n.object_bytes,
                        "parent_position": n.parent_position,
                    }
                    for n in self.nodes.heaviest(20)
                ],
            },
            "script_findings": [
                {
                    "signature": f.signature,
                    "severity": f.severity.value,
                    "stream": f.stream,
                    "offset": f.offset,
                    "encoding": f.encoding,
                    "occurrences": f.occurrences,
                    "context": f.context,
                }
                for f in self.findings
            ],
            "measurement_note": (
                "All byte figures are on-disk chunk weights. In-memory weight is "
                "NOT derived here — the disk-to-RAM multiplier depends on class, "
                "modifier depth and instancing, and has not been calibrated "
                "against a real scene (spike S3)."
            ),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def to_text(self) -> str:
        inv = self.inventory
        out: list[str] = []
        add = out.append

        add(f"MaxRescue X-ray — {self.path}")
        add(f"{_mb(self.file_bytes)} on disk · verdict: {self.verdict.upper()}")
        add("")

        for line in self.observations:
            add(f"  • {line}")
        if self.observations:
            add("")

        add("Streams")
        for s in sorted(self.streams, key=lambda s: -s.size):
            flag = " (compressed)" if s.compressed else ""
            add(f"  {s.name:<34} {_mb(s.size):>12}{flag}")
        add("")

        if inv.objects:
            add(f"Scene — {len(inv.objects):,} objects, {_mb(inv.total_bytes)}")
            if inv.truncated:
                add(f"  ! walk stopped early: {inv.error}")
            add("")
            add("  Heaviest classes")
            for row in inv.histogram()[:15]:
                share = row.bytes / inv.total_bytes * 100 if inv.total_bytes else 0
                add(
                    f"    {row.class_name:<34} {row.count:>7,} objs "
                    f"{_mb(row.bytes):>12}  {share:5.1f}%"
                )
            add("")
            if self.nodes.total_nodes:
                add(
                    f"  Heaviest nodes  ({self.nodes.resolved_count:,} of "
                    f"{self.nodes.total_nodes:,} named, "
                    f"{self.nodes.resolution_rate:.0%})"
                )
                for node in self.nodes.heaviest(10):
                    stack = (
                        " + " + " + ".join(node.modifiers) if node.modifiers else ""
                    )
                    label = node.name or f"<unnamed #{node.position}>"
                    add(
                        f"    {label:<32} {node.base_object_class}{stack}"
                        f"  {_mb(node.object_bytes)}"
                    )
                add("")
            add("  Heaviest single objects")
            for obj in inv.heaviest(10):
                add(
                    f"    #{obj.position:<8} {obj.class_name:<32} {_mb(obj.bytes):>12}"
                )
            add("")

        add("Plugins required by this scene")
        for name in self.required_dlls or ["  (none declared)"]:
            add(f"  {name}")
        scripted = self.catalog.scripted_classes()
        if scripted:
            add("")
            add(f"  {len(scripted)} scripted class(es) — MAXScript-defined:")
            for entry in scripted[:10]:
                add(f"    {entry.name or f'<class {entry.index}>'}")
        add("")

        if self.malware:
            add("KNOWN PAYLOAD SIGNATURES")
            for f in self.malware:
                add(
                    f"  {f.signature}  in {f.stream} @ {f.offset} "
                    f"({f.encoding}, ×{f.occurrences})"
                )
            add("")
        if self.suspicious:
            names = sorted({f.signature for f in self.suspicious})
            add(f"Unsafe-command tokens present ({len(names)}): {', '.join(names)}")
            add("  These appear in plenty of legitimate scripted plugins.")
            add("")

        add(
            "Byte figures are on-disk chunk weights. In-memory weight is not "
            "derived here —"
        )
        add("that multiplier needs calibrating against a real scene first.")
        return "\n".join(out)


def _mb(value: int) -> str:
    if value >= 1 << 30:
        return f"{value / (1 << 30):,.2f} GB"
    if value >= 1 << 20:
        return f"{value / (1 << 20):,.1f} MB"
    if value >= 1 << 10:
        return f"{value / (1 << 10):,.1f} KB"
    return f"{value} B"


def _judge(
    inventory: SceneInventory,
    nodes: NodeGraph,
    findings: tuple[Finding, ...],
    catalog: ClassCatalog,
) -> tuple[str, tuple[str, ...]]:
    """Pick one headline verdict, and say why in plain sentences."""
    notes: list[str] = []
    verdict = Verdict.HEALTHY

    malware = [f for f in findings if f.severity is Severity.MALWARE]
    if malware:
        verdict = Verdict.MALWARE
        names = sorted({f.signature for f in malware})
        notes.append(
            f"Known payload signature(s) present: {', '.join(names)}. "
            "Do not open this file in Max until it has been cleaned."
        )

    if inventory.truncated:
        if verdict == Verdict.HEALTHY:
            verdict = Verdict.DAMAGED
        notes.append(
            f"The scene walk stopped early — {inventory.error}. "
            "Everything after that point is unread, so counts are a floor."
        )

    total = inventory.total_bytes
    if inventory.objects and total:
        heaviest = inventory.heaviest(1)[0]
        share = heaviest.bytes / total
        weights = sorted(o.bytes for o in inventory.objects)
        median = weights[len(weights) // 2] or 1
        dominant = (
            len(inventory.objects) >= _MONSTER_MIN_OBJECTS
            and share >= _MONSTER_SHARE
            and heaviest.bytes >= median * _MONSTER_VS_MEDIAN
        )
        if dominant:
            if verdict == Verdict.HEALTHY:
                verdict = Verdict.MONSTER_OBJECT
            owner = nodes.owner_of(heaviest.position)
            who = (
                f'"{owner.name}" (#{heaviest.position}, {heaviest.class_name})'
                if owner is not None and owner.name
                else f"One object (#{heaviest.position}, {heaviest.class_name})"
            )
            notes.append(
                f"{who} is "
                f"{_mb(heaviest.bytes)} — {share:.0%} of the scene and "
                f"{heaviest.bytes // median:,}× the median object. "
                "A single object this dominant is usually why a file will not open."
            )
        elif len(inventory.objects) > 50_000:
            if verdict == Verdict.HEALTHY:
                verdict = Verdict.BLOAT
            notes.append(
                f"{len(inventory.objects):,} objects — weight is spread across many "
                "objects rather than concentrated, so batching will help more than "
                "hunting one culprit."
            )

    scripted = catalog.scripted_classes()
    if scripted:
        notes.append(
            f"{len(scripted)} scripted (MAXScript-defined) class(es) — the doorway "
            "embedded payloads use. Worth reading even with no signature hit."
        )

    if catalog.malformed_entries():
        notes.append(
            f"{len(catalog.malformed_entries())} class-directory entries could not "
            "be decoded; classes for those objects are unresolved."
        )

    if nodes.total_nodes and nodes.resolution_rate < 0.9:
        notes.append(
            f"Only {nodes.resolution_rate:.0%} of nodes could be fully resolved "
            f"({nodes.resolved_count:,} of {nodes.total_nodes:,}). Names and "
            "modifier stacks are incomplete; weights are unaffected."
        )

    if verdict == Verdict.HEALTHY and not notes:
        notes.append("Nothing anomalous found in the container, directories or scene index.")

    return verdict, tuple(notes)


def xray(path: str) -> XrayReport:
    """Read a `.max` file and report on it. Never opens 3ds Max."""
    import os

    with MaxFile.open(path) as mf:
        dlls = parse_dll_directory(mf.read("DllDirectory")) if mf.has("DllDirectory") else []
        classes = (
            parse_class_directory(mf.read("ClassDirectory3"))
            if mf.has("ClassDirectory3")
            else []
        )
        catalog = ClassCatalog(dlls=dlls, classes=classes)

        # ONE reader for both passes. `ChunkReader` seeks rather than
        # consuming, so it is reusable — and opening twice would inflate a
        # compressed Scene stream twice, at gigabyte scale.
        if mf.has("Scene"):
            reader = mf.open_reader("Scene")
            inventory = walk_scene(reader, catalog)
            nodes = build_node_graph(reader, catalog, inventory)
        else:
            inventory = SceneInventory(objects=(), stream_size=0)
            nodes = NodeGraph(nodes=())

        targets = [s.name for s in mf.streams if s.name.lower() in _SCRIPT_BEARING]
        findings = tuple(scan_max_file(mf, targets))
        streams = mf.streams

    verdict, observations = _judge(inventory, nodes, findings, catalog)
    return XrayReport(
        path=path,
        file_bytes=os.path.getsize(path),
        streams=streams,
        catalog=catalog,
        inventory=inventory,
        nodes=nodes,
        findings=findings,
        verdict=verdict,
        observations=observations,
    )
