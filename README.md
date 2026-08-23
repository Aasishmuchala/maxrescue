# MaxRescue

> Box runbook: **[docs/box-session.md](docs/box-session.md)** · Plan: **[tasks/plan.md](tasks/plan.md)**
> · Checklist: **[tasks/todo.md](tasks/todo.md)** · Traps: **[docs/gotchas.md](docs/gotchas.md)**

Headless memory reduction for 3ds Max scenes too large to work with — **without
changing a rendered pixel**.

Reads the `.max` container directly (OLE2, no Max involved), then drives
`3dsmaxbatch.exe` to rebuild the scene into an empty session in batches under a
memory governor, reducing each batch while only that batch is resident.

**v1 target:** one real archviz scene, **170–192 GB → ≤70 GB** open RSS, on a
128 GB / 32 GB-VRAM box, with V-Ray 7 renders bit-identical to the original.
Pipeline: 3ds Max 2026 · V-Ray 7 · Chaos Vantage 3.3.1.

---

## Commands

| | Needs Max? | |
|---|---|---|
| `xray` | no | What is in this file, and why won't it open? |
| `plan` | no | What batches would a rescue use? |
| `rescue` | **yes** | Rebuild the scene in batches and write a new file |
| `verify` | no | Are two rendered frames identical? |

```bash
# Anywhere — no Max, no Windows, no plugins. Works on a file that crashes on open.
python -m maxrescue.app.cli xray  scene.max
python -m maxrescue.app.cli plan  scene.max --ceiling-gb 70
python -m maxrescue.app.cli verify before.exr after.exr
```

```powershell
# On the box
pwsh scripts\run_rescue.ps1 -Target "D:\jobs\villa\villa.max" -CeilingGB 70
```

### What `xray` answers

- **Why won't this open?** — one dominant object, spread bloat, a damaged chunk
  tree, or a known malware payload.
- **Which plugins does it need?** — every DLL the file declares, so a missing
  plugin is known before burning a 45-second Max launch.
- **What is the heavy thing called?** — names, modifier stacks, parent links.
- **Is anything scripted embedded in it?** — ALC / CRP / ADSL / PhysXPluginMfx /
  MSCPROP signatures, MAXScript-defined classes, unsafe-command tokens.

Exit codes: `0` fine · `2` unreadable · `3` known payload signature.

---

## The contract

**Renders are bit-identical.** Eight reduction stages run automatically, and
every one is render-identical by construction:

hygiene (bitmap cache, undo buffer, unused materials, Motion Mixer) · removal of
hidden and non-renderable objects · collapse of static modifier stacks · dense
meshes to V-Ray proxies at bounding-box display · full-resolution bitmaps paged
out of memory · Nitrous texture caps · scatter **viewport** display modes

One stage is **not** identical and is therefore opt-in and diff-gated:
Bitmap→VRayBitmap changes the filtering kernel by design (Chaos document this),
so `--convert-bitmaps` must be asked for and its result reviewed.

**Nothing relevant goes missing.** Backup before mutation, every operation
verified immediately, and a material guard that snapshots live references —
holding a refcount, not just a handle — then re-attaches anything lost.

**The numbers are measured.** Real RSS/VRAM/poly/MB deltas sampled on the box,
with the source of every sample recorded. The one prediction in the system is
the governor's disk→RAM multiplier, which starts as a declared prior marked
*NOT measured* and corrects itself from real batches.

**Every exclusion is logged with its reason.** A silent skip reads as "covered"
when it was not.

---

## Design notes worth knowing

**`olefile` loads whole streams.** `openstream()` returns a `BytesIO` holding the
entire stream — its own source carries a FIXME saying so. For a multi-gigabyte
`Scene` stream that defeats the design, so olefile parses the container and
MaxRescue reads bulk data off the FAT chain itself, with a visited bitmap to
catch cyclic allocation tables.

**Tier 3 is deliberately not load-bearing.** Node names and modifier stacks are
the most version-fragile code here, so each node parses inside its own guard. An
unknown layout costs that node its name and nothing else; weights and batching
never depend on it, and the report says what fraction resolved.

**Families are never split.** A parent and its children always merge together. An
oversized family is kept whole and flagged instead, because an over-budget batch
can be retried but a hierarchy split across two merges is silently wrong in a way
no later pass can detect.

**Vantage is out of scope.** Chaos confirm Vantage loads V-Ray proxies fully into
VRAM, so nothing done to the Max scene changes its footprint. Its only levers are
its own dynamic-textures and LOD-bias settings.

---

## Development

```bash
python3 -m venv .venv && ./.venv/bin/python -m pip install -e ".[dev]"
./.venv/bin/python -m pytest
```

424 tests, under a second, on macOS with no 3ds Max anywhere.

`xray/` and `core/` are pure — no `pymxs`, no Qt — and a test enforces it in both
directions. `maxbridge/` is the only place Max may exist.

Because `maxbridge/` cannot be imported off a Windows box, it is checked
statically instead: [tests/test_bridge_static.py](tests/test_bridge_static.py)
parses it and asserts it implements every port the core calls, plus the specific
traps it must not fall into. That is what makes staging code that has never run
defensible — but it is not a substitute for running it.

Sibling of MaxSlim / MaxOptimizer / MaxGaffer / MaxDirector; the hardened safety
machinery is carried over from those repos rather than rewritten.
