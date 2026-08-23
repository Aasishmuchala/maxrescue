# MaxRescue — implementation plan

**Date:** 2026-08-23 · **Owner:** Aasish Muchala · **Status:** awaiting review

## Overview

MaxRescue reduces the open-memory footprint of 3ds Max scenes that are too large
to work with, without changing a rendered pixel. It X-rays the `.max` file
externally (OLE2, no Max needed), then drives `3dsmaxbatch.exe` to rebuild the
scene into an empty session **in batches under a memory governor**, applying only
render-identical operations, and reports measured RSS/VRAM/poly/MB deltas.

**v1 is scoped entirely by one real scene:** currently 170–192 GB open RSS
(usually ~170) on a 256 GB workstation, 29 GB VRAM. Target: **≤70 GB open RSS**
on the 128 GB / 32 GB-VRAM box, with V-Ray 7 renders bit-identical to the
original.

Pipeline: 3ds Max 2026 · V-Ray 7 · Chaos Vantage 3.3.1.

## Locked decisions

| # | Decision | Choice |
|---|---|---|
| 1 | Product shape | Reusable tool (MaxRescue), sibling of MaxSlim/MaxOptimizer — but every v1 acceptance criterion is about this one scene |
| 2 | Target | **≤70 GB open RSS**, not "≤128 GB" — V-Ray's render peak stacks on top of Max's open RSS, and Vantage Live Link holds a second copy of geometry+textures in host RAM |
| 3 | X-ray depth | All three tiers (streams+directories, shallow scene walk, node graph) — **T3 degrades to T2 on any unknown chunk layout, never aborts** |
| 4 | Primary path | Dual: batch-merge (reusable, removes the big-box dependency) **and** in-session reduction on the 256 GB box (cannot fail, since the scene demonstrably opens there) |
| 5 | Op set | 8 render-identical stages automatic; Bitmap→VRayBitmap+`.tx` opt-in behind a mandatory `idiff` gate; Poly→Mesh and XRef offload deferred to v1.1 |
| 6 | Renderer | V-Ray 7 CPU. Vantage VRAM is **out of scope** — Chaos confirm Vantage loads proxies fully, so nothing MaxRescue does changes its VRAM |
| 7 | Interface | CLI-first (`maxrescue xray \| rescue \| verify`). No Qt UI in v1 — the tool runs headless by design |
| 8 | Output | Never overwrites. Writes `<scene>_rescued.max` plus a backup of any in-place mutation |
| 9 | Max versions | 2026 primary (Python 3.11.9); code targets ≥3.10 so 2024–2027 stay reachable |
| 10 | Issue tracker | Local markdown, `.scratch/<feature>/issues/` (see `docs/agents/issue-tracker.md`) |

## The contract

1. **Renders are bit-identical.** Only zero-render-impact ops run automatically.
   The one opt-in stage is gated by `idiff` with locked noise seed and light
   cache from file.
2. **Nothing relevant goes missing.** Backup before any mutation; every op
   verified immediately; the material guard re-checks every node→material
   binding after each batch and re-attaches from pinned live refs.
3. **The numbers are measured.** Real RSS/VRAM samples on the box. Never a
   predicted saving.
4. **Every exclusion is logged with its reason.** Silent skips read as "covered".

## Architecture

Hexagon, test-enforced — identical to MaxSlim's proven shape.

```
maxrescue/
  xray/          PURE, no Max, no pymxs — the external reader
    ole.py         olefile wrapper, stream inventory, gzip-stream handling
    chunks.py      chunk reader: 6-byte header, 0x80000000 container flag,
                   size==0 → 14-byte header with uint64 length
    directories.py DllDirectory (0x2038/0x2039/0x2037) +
                   ClassDirectory3 (0x2040/0x2060/0x2042); DllIndex -1=builtin, -2=scripted
    scene_walk.py  T2: top-level Scene chunks → (class, byte weight) histogram
    nodes.py       T3: node names 0x0962, refs 0x2034/0x2035, modifier stacks
                   via DerivedObject 0x2032/0x2033 — degrades to T2 on unknowns
    signatures.py  ALC/CRP/ADSL/PhysXPluginMfx sigs, unsafe-command tokens
    report.py      X-ray report model

  core/          PURE — planner, governor, choreography
    types.py       NodeInfo, SceneStats, MemorySample, Op, OpResult, Plan,
                   RunReport, BatchReport
    interfaces.py  ports: SceneQuery, FixServices, MergeServices, MemoryProbe,
                   GuardServices, UndoService, BackupService
    plan.py        stage planners + the full guard matrix (vendored from MaxSlim)
    governor.py    NEW — batch sizing from weight histogram + live RSS feedback
    session.py     per-batch choreography (adapted from MaxSlim)
    batch.py       NEW — outer loop over batches, two-level progress, BatchReport
    guard.py       material-guard diff (vendored)
    metrics.py     measured deltas, extended with RSS/VRAM (vendored + extended)
    report.py      wording

  maxbridge/     IMPURE — the only pymxs importers
    merge.py       NEW — getMAXFileObjectNames, mergeMaxFile with explicit flags
    memory.py      NEW — MemoryProbe: sysinfo.getMAXMemoryInfo(), nvidia-smi
    scene_query.py vendored + extended (stack depth, hidden/renderable, scatter)
    fix_services.py vendored + NEW (collapse, display modes, bitmap proxy, Nitrous)
    guard_bridge.py, backup.py, undo.py, maxscript.py, renderer_query.py — vendored

  app/
    cli.py         NEW — xray / rescue / verify
    settings.py    vendored (garbage-tolerant load)
    controller.py  adapted

  scripts/       on-box: run_rescue.ms, spikes.py, run_max_tests.ps1
  tests/         pure pytest + fakes that model reality
```

**Reuse:** `undo.py`, `backup.py`, `guard_bridge.py`, `core/guard.py`,
`core/metrics.py`, `app/settings.py`, and the three structural test suites
(`test_no_max_imports`, `test_maxscript_lint`, `test_scripts_static`) come
byte-for-byte from MaxSlim, namespace changed. `core/plan.py`'s guard matrix and
class lists (`_PROXYABLE_CLASSES`, `_NEVER_TOUCH_PREFIXES`, `_PROXY_CLASSES`,
`_MALWARE_SIGNATURES`) transfer directly. The genuinely new subsystems are the
X-ray, the memory probe, the merge primitives, the governor, and the batch loop.

## Dependency graph

```
xray/ (no deps, no Max) ───────────────────────┐
                                                ├──► governor ──┐
maxbridge/memory.py (MemoryProbe) ─────────────┘                │
                                                                 ▼
maxbridge/merge.py (getMAXFileObjectNames, mergeMaxFile) ──► batch loop
                                                                 │
vendored safety (backup, undo, guard) ──► session ◄──────────────┘
                                             │
                        op stages (proxy, collapse, hidden,
                        bitmap, Nitrous, scatter, hygiene)
                                             │
                                             ▼
                              render-identity gate (idiff)
                                             │
                                             ▼
                                          CLI / e2e
```

Implementation order follows this bottom-up, except that **P0 spikes come
first** — they are the highest-risk unknowns and one of them can invalidate the
batch architecture entirely.

---

## Phase 0 — Spikes (fail fast, on the box)

The whole batch premise rests on one undocumented behaviour. Prove it before
building anything on top of it. Run on the **256 GB box first** (safe — the
scene opens there), then re-confirm the critical ones on the 128 GB box.

### Task 1: Spike harness

**Description:** Port MaxOptimizer's headless harness to MaxRescue —
`run_max_tests.ps1` → `3dsmaxbatch.exe` → `.ms` wrapper → `spikes.py`, with the
result-file-not-stdout contract, kill-on-timeout, and zombie-process cleanup
between runs.

**Acceptance criteria:**
- [ ] `pwsh scripts\run_max_tests.ps1 -MaxVersion 2026` launches 3dsmaxbatch and exits 0 on a trivial assertion
- [ ] Result is read from `_spike_result.txt`, never parsed from stdout
- [ ] Timeout kills the process and exits non-zero; zombie `3dsmax`/`3dsmaxbatch` killed before each run
- [ ] Python logging goes to a file, never a stdout StreamHandler (causes exit -130)

**Verification:** run twice back-to-back; both exit 0, no orphan processes in Task Manager.
**Dependencies:** none · **Files:** `scripts/run_max_tests.ps1`, `scripts/run_spikes.ms`, `scripts/spikes.py` · **Scope:** M

### Task 2: S1 — merge-peak spike ⚠️ THE GATE

**Description:** Determine whether a *selective* `mergeMaxFile` deserialises the
whole file. Undocumented; Autodesk's own KB recommends batch-merge as a recovery
technique for files that crash on open, but a CGarchitect report shows merging
one box from a bloated file cost 3.2 GB. If peak ≈ full-open, batching controls
only steady state and the architecture changes.

**Acceptance criteria:**
- [ ] Baseline RSS sampled on an empty scene in batch mode
- [ ] `getMAXFileObjectNames` on the real scene: wall time + peak RSS recorded
- [ ] `mergeMaxFile` of **one small node** by name: peak RSS recorded
- [ ] Same for a 100-node batch and a 1000-node batch
- [ ] Verdict written: `MERGE_SELECTIVE` (peak stays low) or `MERGE_FULL` (peak ≈ full open)

**Verification:** peak RSS after merging one node is **< 30 GB** → batching viable. Cross-check against Task Manager.
**Dependencies:** 1 · **Files:** `scripts/spikes.py` · **Scope:** S

### Task 3: S2 — memory-probe fidelity

**Description:** Establish that the numbers the tool reports are true.

**Acceptance criteria:**
- [ ] `sysinfo.getMAXMemoryInfo()[3]` (WorkingSet) tracks Task Manager within 5 %
- [ ] `psutil` installed into Max 2026's Python (`python.exe -m pip install --user psutil`) and importable inside batch — or a documented `ctypes GetProcessMemoryInfo` fallback
- [ ] `nvidia-smi --query-gpu=memory.used` sampled and attributed to the right PID
- [ ] Whether 3dsmaxbatch creates a graphics device / consumes VRAM at all (exit -7 risk) recorded

**Verification:** three independent sources agree on the same open scene.
**Dependencies:** 1 · **Files:** `scripts/spikes.py` · **Scope:** S

### Task 4: S3 — geometry-vs-texture split ⚠️ DECIDES SCOPE

**Description:** Find out where the 170 GB actually is. Open the scene on the
256 GB box, sample RSS, then `freeSceneBitmaps()` + `gc()` and sample again. The
delta approximates bitmap RAM. Also record the poly total and the bitmap
inventory. The 22 GB open-to-open variance hints textures are significant.

**Acceptance criteria:**
- [ ] RSS before/after `freeSceneBitmaps()` + `gc()` recorded
- [ ] Total polys, node count, unique base-object count, bitmap count and on-disk texture MB recorded
- [ ] Split reported as "geometry ≈ X GB / textures ≈ Y GB / other ≈ Z GB"

**Verification:** the three components sum to within 15 % of open RSS.
**Dependencies:** 1 · **Files:** `scripts/spikes.py` · **Scope:** S

### Task 5: S4 — render determinism

**Description:** Prove the identity gate itself works before trusting it to
approve anything. Render the same scene twice with locked noise seed, fixed
`dmc_randomSeed`, bucket sampler, light cache loaded **from file**, denoiser off,
raw EXR output, and diff.

**Acceptance criteria:**
- [ ] Two renders of an unmodified scene are bit-exact under `idiff` (exit 0)
- [ ] Varying `VRAY_NUM_THREADS` does not change pixels
- [ ] The exact MAXScript property names used are recorded (`showproperties (renderers.current)` dumped to the report — several are unverified in V-Ray 7)

**Verification:** `idiff A.exr B.exr` exit code 0 on two independent runs.
**Dependencies:** 1 · **Files:** `scripts/spikes.py`, `scripts/render_ab.py` · **Scope:** M

### Checkpoint A — architecture gate

- [ ] S1 verdict recorded. If `MERGE_FULL`: batch-merge is downgraded to a
      stretch goal, the 256 GB in-session path becomes primary, and the plan is
      revised **before** P3 is written.
- [ ] S3 split recorded. If textures dominate, the opt-in VRayBitmap stage is
      promoted from "nice to have" to "required to hit 70 GB".
- [ ] S4 green — the identity gate is trustworthy.
- [ ] Human review before proceeding.

---

## Phase 1 — X-ray (pure Python, no Max)

Independent of everything above; can proceed in parallel with P0 while waiting
for box time. Needs one real `.max` file copied to the dev machine.

### Task 6: OLE2 container + chunk reader

**Description:** Open a `.max` with `olefile`, enumerate streams, handle
gzip-compressed streams, and implement the chunk reader.

**Acceptance criteria:**
- [ ] Streams enumerated with sizes; `\x05DocumentSummaryInformation` properties read
- [ ] Chunk header handles both forms: `uint16 id + int32 size` (size includes the 6-byte header; bit 31 = container), and the `size==0` escape → 14-byte header with `uint64` length, bit 63 = container
- [ ] Gzip-framed streams (`1F 8B` magic) transparently inflated
- [ ] Malformed/truncated chunks raise a typed error, never loop forever

**Verification:** parse a real `.max`; stream inventory matches `olefile.listdir`; total chunk spans reconcile to stream length exactly.
**Dependencies:** none · **Files:** `maxrescue/xray/ole.py`, `chunks.py`, `tests/test_chunks.py` · **Scope:** M

### Task 7: Class + DLL directories, missing plugins, malware signatures

**Description:** Decode `DllDirectory` and `ClassDirectory3`, join them, and
report every class the scene needs with its plugin DLL. Grep for known malware
signatures and unsafe-command tokens.

**Acceptance criteria:**
- [ ] `ClassDirectory3` entry `0x2060` unpacked as `int32 DllIndex, uint32 ClassID.A, uint32 ClassID.B, uint32 SuperClassID`; name from `0x2042` (UTF-16LE)
- [ ] `DllIndex -1` reported as built-in, **`-2` as scripted** (a first-class signal)
- [ ] Known ClassIDs resolved to friendly names (Editable Poly `(0x1BF8338D,0x192F6098)`, VRayMtl, VRayBitmap, TurboSmooth, …); unknown ones reported as raw IDs + DLL name
- [ ] Missing-plugin candidates listed (DLLs referenced by the file)
- [ ] Malware signature scan over UTF-16LE **and** ASCII: `AutodeskLicSerStuckClean*`, `ADSL_BScript`, `CRP_*Script`, `physXCrtRbkInfoCleanBeta`, `PhysXPluginStl`, `mscprop.dll`, plus unsafe tokens (`DOSCommand`, `ShellLaunch`, `registry.*`, …)

**Verification:** against a known-clean scene (no hits) and a scene with a benign planted signature (hit).
**Dependencies:** 6 · **Files:** `xray/directories.py`, `xray/signatures.py`, tests · **Scope:** M

### Task 8: T2 — shallow scene walk (weight histogram)

**Description:** Walk the top-level chunks of `Scene` without decoding contents.
Each chunk's id **is** its `ClassDirectory3` index and its size **is** its byte
weight. Produces the distribution the governor needs and finds single monster
objects instantly.

**Acceptance criteria:**
- [ ] Every top-level object reported as `(index, class name, superclass, bytes)`
- [ ] Hard-coded exceptions handled: `0x2032` = OSM DerivedObject, `0x2033` = WSM DerivedObject (not in the class table)
- [ ] Histogram + top-N heaviest objects + totals per class
- [ ] Runs on a multi-GB file in < 60 s and O(1) memory (streaming, no full read)

**Verification:** sum of object bytes ≈ `Scene` stream size; top-N cross-checked against Max's own object list on the box.
**Dependencies:** 7 · **Files:** `xray/scene_walk.py`, tests · **Scope:** M

### Task 9: T3 — node graph, with degradation

**Description:** Decode node chunks to attach names and modifier stacks to
weights. Node name `0x0962` (UTF-16LE); references via `0x2034` (flat int32
array, `-1` = null) or `0x2035` (map: `[flags, key, idx, …]`, slot 0 = transform,
1 = object, 3 = material, 6 = layer). Modifier stack = a `DerivedObject`'s
references whose superclass is `0x810`/`0x820`, in order; the remaining reference
is the base object.

**Acceptance criteria:**
- [ ] Node name, object class, material class, modifier-stack depth and modifier class list reported per node
- [ ] **Any unknown chunk layout downgrades that node to T2 (class + bytes, name unknown) and is logged — never raises**
- [ ] A `--strict` flag turns degradation into an error, for development only
- [ ] Degradation rate reported in the X-ray output ("94 % of nodes fully resolved")

**Verification:** node names cross-checked against `getMAXFileObjectNames` output from the box — ≥ 90 % match, mismatches enumerated.
**Dependencies:** 8 · **Files:** `xray/nodes.py`, tests · **Scope:** M

### Task 10: X-ray report + `maxrescue xray` CLI

**Description:** Wire the tiers into one command producing a JSON artefact and a
readable HTML report.

**Acceptance criteria:**
- [ ] `maxrescue xray <file>` runs with no Max installed and no network
- [ ] Report contains: verdict (bloat / missing plugin / malware / single monster object / healthy), stream inventory, class inventory with DLLs, weight histogram, top-N heaviest, plugin-risk list, malware findings, estimated in-memory weight, degradation rate
- [ ] JSON is stable and machine-readable (consumed by the governor in P3)
- [ ] Exit code non-zero on malware findings

**Verification:** run against the real scene; verdict matches what the box actually shows.
**Dependencies:** 9 · **Files:** `xray/report.py`, `app/cli.py`, tests · **Scope:** M

### Checkpoint B — X-ray

- [ ] All pure tests green; `pytest` runs on macOS with no Max
- [ ] X-ray of the real scene produces a verdict consistent with the S3 spike split
- [ ] Human review

---

## Phase 2 — Skeleton and honest measurement

### Task 11: Repo scaffold + vendored safety layer + test wall

**Description:** Package layout, `pyproject.toml`, vendored `undo.py`,
`backup.py` (subdir `maxrescue_backups`), `guard_bridge.py`, `core/guard.py`,
`settings.py`, the fakes module, and the three structural test suites.

**Acceptance criteria:**
- [ ] `test_no_max_imports.py` fails if any `core/*` or `xray/*` module imports pymxs/PySide/maxbridge
- [ ] `test_maxscript_lint.py` rejects trailing-backslash continuation in any `.ms`
- [ ] `test_scripts_static.py` compiles every on-box script and resolves every first-party import
- [ ] Fakes model reality: failed export never deletes, `delete_layer` refuses non-empty, undo restores on exception

**Verification:** `pytest -q` green on macOS.
**Dependencies:** none · **Files:** `pyproject.toml`, `maxrescue/{core,maxbridge,app}/`, `tests/` · **Scope:** M

### Task 12: MemoryProbe port + bridge + fake

**Description:** The port that makes every claim measurable.

**Acceptance criteria:**
- [ ] `MemorySample(rss_mb, peak_rss_mb, pagefile_mb, vram_mb, source)` typed and immutable
- [ ] Bridge implementation prefers `psutil`, falls back to `sysinfo.getMAXMemoryInfo()`, then `ctypes GetProcessMemoryInfo`; source recorded in every sample
- [ ] VRAM via `nvidia-smi` per-PID; absent GPU degrades to `None`, never raises
- [ ] Sampling is cheap enough to call between batches (< 100 ms)

**Verification:** matches the S2 spike numbers within 5 %.
**Dependencies:** 11, 3 · **Files:** `core/interfaces.py`, `maxbridge/memory.py`, `tests/fakes.py` · **Scope:** S

### Task 13: Metrics + report extended to memory

**Description:** Extend MaxSlim's measured-only metrics with RSS/VRAM so the
headline is a memory number, not a poly number.

**Acceptance criteria:**
- [ ] Headline = measured open-RSS reduction; breakdown carries polys, file MB, node/material counts, VRAM
- [ ] Never emits a predicted or estimated saving
- [ ] Report distinguishes a clean run from a halted one (a backup-failure halt must not claim "backup intact")

**Verification:** golden tests on the wording; no timestamps or host calls in the pure path.
**Dependencies:** 12 · **Files:** `core/metrics.py`, `core/report.py`, tests · **Scope:** S

### Checkpoint C — measurement

- [ ] The tool can state the current open RSS of a scene and be believed
- [ ] Tests green

---

## Phase 3 — Batch merge and the governor

**Gated by Checkpoint A.** If S1 returned `MERGE_FULL`, revise this phase first.

### Task 14: MergeServices port + bridge + fake

**Description:** The two primitives neither sibling repo has.

**Acceptance criteria:**
- [ ] `list_object_names(path)` → `getMAXFileObjectNames` with `missingDLLsAction:#logmsg` and the DLL list captured
- [ ] `merge(path, names)` passes **explicit** dup/material/reparent flags — never the interactive defaults `#promptMtlDups` / `#promptReparent`, which hang a batch run
- [ ] `mergedNodes:&arr` captured and verified against the requested names; shortfall reported per name
- [ ] Duplicate source names handled (documented behaviour or `mergeByHandles`)
- [ ] Missing-DLL / missing-external-file / missing-XRef lists surfaced into the report

**Verification:** on-box merge of a known 10-node subset; all 10 arrive, names match.
**Dependencies:** 11 · **Files:** `maxbridge/merge.py`, `core/interfaces.py`, `tests/fakes.py` · **Scope:** M

### Task 15: Governor (pure)

**Description:** Decide batch composition from the X-ray histogram and live RSS
feedback. Pure, fully testable without Max.

**Acceptance criteria:**
- [ ] Given a weight histogram and a RAM ceiling, emits batches whose predicted peak stays under the ceiling
- [ ] Adapts on live feedback: an over-budget batch shrinks the next one; consistent headroom grows it
- [ ] Objects too heavy for any batch are isolated into a batch of one and flagged
- [ ] Keeps parents with children in the same batch (or emits `#mergeChildren`/`includeFullGroup`), so hierarchies survive
- [ ] Never emits an empty batch or loops forever; total coverage of the name list is asserted

**Verification:** property tests — every object appears in exactly one batch; no batch exceeds the ceiling given the model; adversarial histograms (one 60 GB object, 50 000 tiny ones) behave sanely.
**Dependencies:** 10, 13 · **Files:** `core/governor.py`, `tests/test_governor.py` · **Scope:** M

### Task 16: Batch loop + BatchReport

**Description:** The outer loop: per-batch backup, merge, guard snapshot, ops,
verify, guard sweep, measure, save increment. Extends MaxSlim's per-op halt/skip
policy to two levels.

**Acceptance criteria:**
- [ ] Halt means "stop the batch run, leave completed output intact, name the batch that failed" — never a lost session
- [ ] A single failing object is quarantined and named; the run continues
- [ ] Guard sweep runs after every batch and on halt/cancel; pins released in `finally`
- [ ] Two-level progress (batch k of N, op j of M)
- [ ] `BatchReport` wraps per-batch `RunReport`s; a crash-forensics journal is flushed after every batch

**Verification:** fake-driven tests for halt, quarantine, cancel and guard repair; the journal survives a simulated hard crash.
**Dependencies:** 14, 15 · **Files:** `core/batch.py`, `core/session.py`, `maxbridge/journal.py`, tests · **Scope:** M

### Task 17: On-box spine run (merge only, no ops)

**Description:** Prove the spine end-to-end before any reduction logic: merge the
entire real scene batch-by-batch into an empty session and save it, with **no**
optimisation stages enabled.

**Acceptance criteria:**
- [ ] Output scene has the same node count, material count and poly count as the source (within a documented tolerance for merge-time material dedup)
- [ ] Peak RSS during the whole run stays under the ceiling
- [ ] Output opens on the 128 GB box (even if still heavy)
- [ ] Every quarantined object listed with a reason

**Verification:** node/material/poly counts compared source vs output; a render A/B on the un-optimised output is bit-identical.
**Dependencies:** 16 · **Files:** `scripts/run_rescue.ms` · **Scope:** M

### Checkpoint D — the spine

- [ ] A 170 GB scene can be rebuilt without ever holding it whole
- [ ] Renders still bit-identical with zero ops applied — proving merge itself is lossless
- [ ] Human review

---

## Phase 4 — Reduction stages

Each stage: planner guard (pure) → bridge primitive → immediate verify →
logged exclusion reason. Ordered cheapest-and-safest first.

### Task 18: Hygiene stages

`gc()`, `freeSceneBitmaps()`, `clearUndoBuffer()`, note-track strip, scene-state
strip, Motion Mixer strip, unused-material purge (report-only where verification
is unreliable — `sceneMaterials` membership ≠ liveness).

**Acceptance criteria:** each op verified by a measured count-to-zero or an RSS delta; XRef materials excluded; nothing halts on a purge that Max defers to save time.
**Verification:** fake tests + on-box RSS delta. **Dependencies:** 16 · **Scope:** M

### Task 19: Hidden / non-renderable node removal

**Acceptance criteria:** only when `rendHidden` is false; guards for
`refs.dependentNodes`, scatter sources, instance masters, constraint targets,
`primaryVisibility=false` (still affects GI/reflections — **keep**); every skip logged.
**Verification:** render A/B bit-identical on a fixture containing each guard case. **Dependencies:** 18 · **Scope:** M

### Task 20: Modifier-stack collapse

**Acceptance criteria:** `maxOps.CollapseNodeTo n n.modifiers.count off` (preserves
instances); skips animated stacks, skins, bones, scripted controllers; TurboSmooth/
MeshSmooth/OpenSubdiv with `useRenderIterations` false are **excluded** (collapsing
at viewport iterations changes the render); displacement and hair/cloth stacks excluded.
**Verification:** render A/B bit-identical; instance count unchanged after collapse. **Dependencies:** 19 · **Scope:** M

### Task 21: Proxy conversion (the dominant lever)

**Acceptance criteria:** vendor MaxSlim stages 5–6 (dense singletons + instance
sets); `vrayMeshExport` with the **verified V-Ray 7 signature** (`exportMultiple`,
`maxPreviewFaces`, `createMultiMtl`, `oneVoxelPerMesh`) — not the Maya parameter
names; proxy `display = 0` (bounding box); **no modifier is ever left on a proxy**
(it reverts to a regular mesh and loses every saving); point clouds **off**;
mirrored / negative-scale nodes skipped and logged; failed export never reaches a delete.
**Verification:** on-box S1/S2-style fidelity spikes + render A/B bit-identical. **Dependencies:** 20 · **Scope:** M

### Task 22: Viewport levers

**Acceptance criteria:** `BitmapProxyMgr.globalProxyRenderMode =
#renderMode_UseFullRes_FlushFromMemory` (**never** `#renderMode_UseProxies` —
that changes the render); `NitrousGraphicsManager` texture/background/procedural
size limits; scatter display modes (Forest point-cloud, RailClone boxes, tyFlow
Display operator) — **viewport groups only, never render groups or Disable Object**;
plugin property names discovered at runtime via `showProperties`, with a
documented skip when a plugin isn't present.
**Verification:** render A/B bit-identical; VRAM delta measured. **Dependencies:** 21 · **Scope:** M

### Checkpoint E — reduction

- [ ] Measured open RSS on the real scene after all eight identical stages
- [ ] Every render A/B bit-identical
- [ ] **Decision point:** if RSS > 70 GB, the opt-in VRayBitmap stage is required (Task 25); if ≤ 70 GB, it stays optional
- [ ] Human review

---

## Phase 5 — Render-identity gate

### Task 23: Render harness

**Acceptance criteria:** locked noise pattern + fixed `dmc_randomSeed`, bucket
sampler, light cache **from file**, denoiser off, memory frame buffer off with
raw EXR output, fixed thread count; property names verified against
`showproperties` output from spike S4 rather than assumed.
**Verification:** two runs bit-exact. **Dependencies:** 5, 22 · **Files:** `scripts/render_ab.py` · **Scope:** M

### Task 24: `maxrescue verify` — the gate

**Acceptance criteria:** `idiff` bit-exact for identical stages (any non-zero
pixel delta fails); tolerant thresholds only for explicitly opt-in stages;
result recorded in the run report with the exact command line used.
**Verification:** a deliberately altered scene must FAIL the gate. **Dependencies:** 23 · **Scope:** S

### Task 25: Opt-in Bitmap → VRayBitmap + `.tx`

**Acceptance criteria:** off by default, requires an explicit flag; converts via
the documented Scene Converter path with **Reset Filtering Blur to 1.0**;
generates tiled `.tx`; runs the gate automatically and **reverts the whole stage
on failure**; documents that filtering kernel and interpolation differ by design,
so the gate is tolerant + human-reviewed, never bit-exact.
**Verification:** gate runs; revert path exercised. **Dependencies:** 24 · **Scope:** M

### Checkpoint F — identity

- [ ] Every automatic stage bit-identical, proven, not asserted
- [ ] Opt-in stage's diff reviewed by a human before use

---

## Phase 6 — Ship

### Task 26: CLI + settings

`maxrescue xray <file>` / `rescue <file> [--out] [--ceiling-gb] [--convert-bitmaps]` /
`verify <a> <b>`. Settings JSON garbage-tolerant, thresholds discoverable.
**Scope:** M

### Task 27: Full run on the real scene ⚠️ THE ACCEPTANCE TEST

**Acceptance criteria:**
- [ ] Source 170–192 GB → output opens at **≤70 GB RSS** on the 128 GB box
- [ ] VRAM ≤ 32 GB
- [ ] V-Ray 7 render bit-identical to a reference rendered from the original
- [ ] A full V-Ray render **completes** on the 128 GB box without swapping
- [ ] Backup intact; material guard clean; every exclusion logged with a reason

**Verification:** measured, three sources agreeing. **Dependencies:** 26 · **Scope:** L (verification, not code)

### Task 28: Docs

README with the honest number, `docs/gotchas.md` updated with everything learned
on the box, on-box checklist for the next scene.
**Scope:** S

### Checkpoint G — ship

- [ ] Task 27 passed on the real scene
- [ ] Full test suite green
- [ ] A second, unrelated scene processed without code changes (proves reusability)

---

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Selective merge deserialises the whole file** (undocumented) | **High** — invalidates batch architecture | Spike S1 is task 2, before anything is built on it. Fallback: in-session reduction on the 256 GB box, which demonstrably works |
| Eight identical stages don't reach 70 GB | High | S3 measures the geometry/texture split in P0; opt-in VRayBitmap stage is designed and costed up front |
| T3 node-graph parsing breaks on this file's Max version | Medium | Degradation to T2 is an acceptance criterion, not an afterthought; T3 is never a dependency of batch planning |
| V-Ray 7 MAXScript property names differ from docs (several unverified) | Medium | Every renderer property is read from `showproperties` in spike S4 and recorded, never assumed |
| Renders not bit-reproducible even unmodified (light cache is multi-threaded) | Medium | S4 proves determinism before the gate is trusted; LC from file removes the main nondeterminism |
| Scatter plugin property names undocumented (Forest/RailClone/tyFlow) | Medium | Runtime discovery via `showProperties`; a plugin whose properties can't be read is skipped and logged, never guessed |
| Batch merge loses hierarchies / material instancing across batches | Medium | Governor keeps parents with children; explicit `#useSceneMtlDups`; task 17 verifies counts before any ops exist |
| Pagefile exhaustion from repeated Max launches (bit the sibling project) | Low | Zombie-process cleanup between runs; pagefile size documented as a prerequisite |
| Vantage VRAM unchanged by any of this | Low | Explicitly out of scope, documented — Chaos confirm proxies don't help Vantage; its levers are its own settings |

## Open questions

1. **A `.max` fixture on the dev machine.** P1 needs one real file (ideally the
   monster plus one small scene) copied to the Mac. Nothing in P1 can be
   verified without it.
2. **Does Vantage run on the same 128 GB box as V-Ray?** If yes, Live Link's
   second copy of geometry+textures must come out of the 70 GB budget, and the
   target may need to drop further.
3. **Is the 256 GB box available for the P0 spikes**, and for how long? Every
   spike except S1's 128 GB re-confirmation wants it.
4. **Does the scene use Forest Pack / RailClone / tyFlow / Phoenix?** Determines
   how much of task 22 is real work. The X-ray answers this from the DLL
   directory without opening the file.
5. **Animation.** Everything above assumes a still or a camera-only animation. If
   objects are animated, collapse and proxy stages narrow considerably.
