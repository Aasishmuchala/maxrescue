# Primary sources

The three reports here — [max-format.md](max-format.md), [max-apis.md](max-apis.md),
[vray-vantage.md](vray-vantage.md) — cite their sources inline with URLs and a
confidence level per fact.

The **source files themselves are deliberately not vendored.** They were read
while writing the reports; none is part of MaxRescue, and two of them cannot be
redistributed:

| Source | Why it is not here | Where to get it |
|---|---|---|
| `plugapi.h` (SuperClassIDs, ClassIDs) | Autodesk SDK header, marked *"unpublished proprietary information… may not be disclosed to third parties"*. **Redistribution would be a licence violation.** | 3ds Max SDK, `maxsdk/include/plugapi.h`, installed with Max |
| `io_scene_max` (Blender's `.max` importer) | **GPL-3.0-or-later.** Vendoring it would impose GPL obligations on this repo | <https://extensions.blender.org> — "Import Autodesk MAX", by Sebastian Sille |
| `importMAX_plonka.py` (FreeCAD Importer3D) | Copyleft, same reasoning | FreeCAD's `Import3D` addon |
| `max_dump` | MIT, so redistributable — omitted only for consistency | <https://github.com/NeKidaem/max_dump> |
| kaetemi's `.max` format series (parts 1–6) | Blog posts | <https://blog.kaetemi.be/2012/08/17/3ds-max-file-format-part-1/> |
| Ryzom Core `pipeline_max` | AGPL-3.0 | <https://github.com/ryzom/ryzomcore> — `nel/tools/3d/pipeline_max` |

Everything MaxRescue actually does is written from scratch against the
documented behaviour in the reports. The byte layouts in `xray/chunks.py` and
`xray/directories.py` are facts about a file format, cross-checked against three
independent implementations and cited in the report — not code taken from any of
them.
