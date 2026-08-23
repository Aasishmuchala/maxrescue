# MaxRescue

Headless memory-reduction tool for 3ds Max scenes too large to open.

Reads a `.max` file externally (OLE2 X-ray, no Max needed), then drives
`3dsmaxbatch.exe` to merge the scene into an empty session **in batches under a
memory governor**, applying only render-identical operations. Target for v1:
one real archviz scene from ~170 GB open RSS to **≤70 GB**, on a 128 GB / 32 GB-VRAM
box, with V-Ray 7 renders bit-identical to the original.

Pipeline: **3ds Max 2026 · V-Ray 7 · Chaos Vantage 3.3.1**.
Sibling of MaxSlim / MaxOptimizer / MaxGaffer / MaxDirector — the hardened
safety machinery (backup, undo transactions, material guard, planner guards) is
vendored from those repos rather than rewritten.

## The contract (non-negotiable)

1. **Renders are bit-identical.** Only zero-render-impact operations run
   automatically. Anything that could move a pixel (bitmap→VRayBitmap
   conversion, Poly→Mesh, proxy point-clouds, scatter render settings, XRef
   unloading) is opt-in and gated behind an `idiff` A/B with locked noise seed
   and light-cache-from-file.
2. **Nothing relevant goes missing.** Backup before any mutation; every op
   verified immediately; material guard re-checks every node→material binding
   after each batch and re-attaches from pinned live refs.
3. **The numbers are measured, never predicted.** Report real RSS / VRAM /
   poly / MB deltas sampled on the box — never estimated savings.
4. **Everything excluded is logged with its reason.** Silent skips read as
   "covered" when they weren't.

## Architecture rules

- **Hexagon, test-enforced.** `maxrescue/core/*` and `app/{controller,settings}.py`
  are pure — no `pymxs`, no PySide, no bridge imports. A test asserts this.
  Only `maxrescue/maxbridge/*` may import `pymxs`.
- **Cross the MAXScript boundary once.** Per-object `pymxs` attribute access is
  ~10× slower than one `rt.execute` that returns a whole array.
- **Fakes model reality, not convenience.** A fake that always succeeds hides
  the bug the real bridge will hit.
- Read `docs/gotchas.md` before touching the bridge — it carries ~35 production
  bugs already paid for in the sibling repos.

## Agent skills

### Issue tracker

Local markdown — issues live as files under `.scratch/<feature>/issues/`. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, label string = role name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
