# MaxRescue — live checklist

Box session: **[../docs/box-session.md](../docs/box-session.md)** · Plan: **[tasks/plan.md](plan.md)** · Contract: **[CLAUDE.md](../CLAUDE.md)** ·
Traps: **[docs/gotchas.md](../docs/gotchas.md)**

**Target:** one real scene, 170–192 GB open RSS → **≤70 GB**, V-Ray 7 renders
bit-identical, on a 128 GB / 32 GB-VRAM box. Max 2026 · V-Ray 7 · Vantage 3.3.1.

---

## Blockers before anything can be verified

- [ ] ⚠️ **Copy one real `.max` to `~/Desktop/maxrescue/fixtures/`** — the X-ray is BUILT and runnable; this is all that stands between it and real answers
- [ ] Confirm 256 GB box availability for P0 spikes
- [ ] Confirm whether Vantage runs on the same 128 GB box as V-Ray (if yes, the 70 GB budget must shrink)
- [ ] Confirm whether the scene is animated (narrows collapse + proxy stages)

## P0 — Spikes (on-box, gates everything) ⚠️ do these first

- [x] **T1** Spike harness STAGED — `run_spikes.ps1` → 3dsmaxbatch → `run_spikes.ms` → `spikes.py`; result-file contract, kill-on-timeout, zombie cleanup, exit codes named. **Unverified until it runs on the box.**
- [x] **T2** ⚠️ **S1 merge-peak STAGED** — measures baseline → `getMAXFileObjectNames` cost → merge of 1/100/1000 objects, emits `MERGE_SELECTIVE` or `MERGE_FULL`. **Unverified until it runs on the box.**
- [x] **T3** S2 memory-probe fidelity STAGED — three independent sources cross-checked, raw `getMAXMemoryInfo` array dumped so the index convention is provable
- [x] **T4** ⚠️ **S3 geometry-vs-texture split STAGED** — gated behind `MAXRESCUE_ALLOW_FULL_OPEN=1` since it loads the whole scene; 256GB box only
- [ ] **T5** S4 render determinism — two identical renders bit-exact under `idiff`; dump `showproperties (renderers.current)`

### ✅ Checkpoint A — architecture gate
- [ ] S1 verdict recorded — if `MERGE_FULL`, **revise P3 before writing it**; 256 GB in-session path becomes primary
- [ ] S3 split recorded — if textures dominate, promote Task 25 to required
- [ ] S4 green — the identity gate is trustworthy
- [ ] Human review

## P1 — X-ray (pure Python, no Max — parallelisable with P0)

- [x] **T6** chunk reader — 24 tests (headers, wide escape, nesting, malformed-raises-not-hangs, streaming O(1)). **OLE2 wrapper (`ole.py`) still to do**
- [x] **T7** `DllDirectory` + `ClassDirectory3` join, ClassID collision fix, **and** the signature scanner (malware vs suspicious separated; streaming with overlap so boundary-straddling matches are found). Missing-plugin *report* lands with T10
- [x] **T8** shallow scene walk — 18 tests; per-object class+weight from headers alone, histogram, heaviest-N, geometry split, DerivedObject special-casing, degrades on damage instead of raising
- [x] **T9** node graph — 17 tests; names, parent, material, modifier stacks via DerivedObject, per-node object-graph weight, `owner_of()` naming the heaviest object; degrades per-node with a reason, `--strict` raises
- [x] **T10** `maxrescue xray <file>` → text + JSON report, verdict, plugin list, malware findings, histogram. **HTML rendering deferred** (text+JSON cover the need)

### ✅ Checkpoint B — X-ray
- [x] Pure tests green on macOS with no Max (170 tests, <0.2s)
- [x] All three tiers built; `maxrescue xray` runnable
- [ ] **Validate against a real .max** (blocked: no fixture)
- [ ] X-ray verdict consistent with the S3 spike split
- [ ] Human review

## P2 — Skeleton + honest measurement

- [ ] **T11** Scaffold + vendored safety (undo, backup, guard) + fakes + 3 structural test suites
- [ ] **T12** `MemoryProbe` port + bridge (psutil → `getMAXMemoryInfo` → ctypes) + `nvidia-smi` VRAM
- [ ] **T13** Metrics/report extended to RSS/VRAM; measured-only, never predicted

### ✅ Checkpoint C — measurement
- [ ] The tool can state a scene's open RSS and be believed

## P3 — Batch merge + governor *(gated by Checkpoint A)*

- [ ] **T14** `MergeServices` — `getMAXFileObjectNames` + `mergeMaxFile` with **explicit** dup/mtl/reparent flags (never the prompting defaults)
- [x] **T15** Governor (pure) — 27 tests; self-calibrating disk→RAM multiplier learned from RSS deltas, families kept intact (union-find, cycle-safe), oversized families isolated + flagged, coverage/termination/determinism invariants pinned. `maxrescue plan` wired up
- [ ] **T16** Batch loop + `BatchReport` + per-batch backup/guard + quarantine-not-halt + journal
- [ ] **T17** On-box spine run — merge the whole scene batch-by-batch, **no ops**, save; counts preserved

### ✅ Checkpoint D — the spine
- [ ] A 170 GB scene rebuilt without ever holding it whole
- [ ] Render bit-identical with zero ops applied — merge itself proven lossless

## P4 — Reduction stages (8 render-identical)

- [ ] **T18** Hygiene — `gc`, `freeSceneBitmaps`, `clearUndoBuffer`, note tracks, scene states, Motion Mixer, unused materials
- [ ] **T19** Hidden / non-renderable removal — guards for dependents, scatter sources, instance masters; **keep** `primaryVisibility=false` (affects GI)
- [ ] **T20** Modifier-stack collapse — `CollapseNodeTo … off` keeps instances; **exclude** render-coupled TurboSmooth/MeshSmooth/OpenSubdiv, displacement, skin, animated
- [ ] **T21** Proxy conversion (dominant lever) — verified V-Ray 7 `vrayMeshExport` signature, `display = 0` (bbox), no modifiers on proxies, point clouds off, mirrored nodes skipped
- [ ] **T22** Viewport levers — `#renderMode_UseFullRes_FlushFromMemory` (**never** `UseProxies`), Nitrous size limits, scatter **viewport** display modes only

### ✅ Checkpoint E — reduction
- [ ] Measured open RSS after all eight stages
- [ ] Every render A/B bit-identical
- [ ] **Decision:** >70 GB → Task 25 required; ≤70 GB → optional

## P5 — Render-identity gate

- [ ] **T23** Render harness — locked seed, bucket, LC **from file**, denoiser off, raw EXR, fixed threads
- [ ] **T24** `maxrescue verify` — `idiff` bit-exact for identical stages; an altered scene must FAIL
- [ ] **T25** Opt-in Bitmap→VRayBitmap + `.tx` — off by default, blur reset to 1.0, auto-gated, **reverts whole stage on failure**

### ✅ Checkpoint F — identity
- [ ] Every automatic stage proven bit-identical, not asserted

## P6 — Ship

- [ ] **T26** CLI (`xray` / `rescue` / `verify`) + garbage-tolerant settings
- [ ] **T27** ⚠️ **Acceptance run** — 170–192 GB → **≤70 GB**, VRAM ≤32 GB, render bit-identical, full render completes on the 128 GB box without swapping
- [ ] **T28** Docs — README with the honest number, gotchas updated from the box

### ✅ Checkpoint G — ship
- [ ] Task 27 passed on the real scene
- [ ] Full suite green
- [ ] A second unrelated scene processed with no code changes

---

## Watch list (from research, unproven)

- Peak RAM of selective merge — **the** unknown (T2)
- `#promptMtlDups` / `#promptReparent` are the **defaults** — will hang a batch run
- `3dsmaxbatch` exit codes: `-6` OOM, `-7` no graphics device, `-8` license, `-130` Max error (a stdout `StreamHandler` alone causes -130)
- V-Ray disables the bitmap pager during rendering — interactive lever only
- Vantage loads proxies fully into VRAM; nothing here helps it
- Live Link keeps a second copy of geometry+textures in host RAM
- Modifiers left on a `VRayProxy` revert it to a regular mesh
