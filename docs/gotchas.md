# Gotchas — inherited, do not re-pay for these

Distilled from MaxOptimizer's `tests/inmax/STRESS-FINDINGS.md` (~35 production
bugs across 3 stress rounds) and MaxSlim's hardening passes. Every line here
cost a real failure in a sibling repo. Read before touching `maxbridge/`.

## MAXScript / pymxs API traps

- `rt.free` does **not** delete a material. Use `deleteItem sceneMaterials <1-based>`.
  Direct indexing is 0-based, `deleteItem`'s argument is 1-based.
- `sceneMaterials` membership ≠ liveness. A purged or merged material stays in
  the library; a liveness-based verify falsely halts. Verify *usage*, by handle.
- `callbacks.show()` **prints, does not return**. Capture via
  `(local ss = stringStream ""; callbacks.show to:ss; ss as string)`.
- `determinant3` is not a MAXScript global. Use `dot (cross m.row1 m.row2) m.row3`.
- `sceneStateMgr` is `GetCount()` / `GetSceneState(i)`, **1-based**; delete with
  `Delete(name)`, not `DeleteSceneState`.
- `VRayBitmap` / `VRayHDRI` use `.HDRIMapName`. Setting `.filename` silently does nothing.
- `rt.maxFileName or "untitled"` never falls through — an empty MXS string
  wrapper is truthy in Python. Call `str()` first.
- Group heads always report 0 faces. "Empty group" must mean *head with 0
  children*, not 0 faces — otherwise you delete real groups.
- Zero-face mesh ≠ junk. Camera/light targets, `PF_Source`, `VRayPlane` and
  `VRayProxy` are all 0-face by design. Whitelist mesh classes.
- Childless helpers may be LookAt/constraint targets. Check `refs.dependentNodes`
  before calling anything an orphan.
- Closed-group `select` selects the **whole group**. One production run turned
  27 intended conversions into ~25k consumed nodes.
- Per-controller reads need **their own `try`** each. One shared `try` around all
  four masked Script *transform* controllers — the strongest scripted signal.
- MAXScript has **no backslash line continuation**. One trailing `\` kills the
  file and the plugin silently never loads.
- A Python raw string ending in a backslash is a SyntaxError — embed Windows
  paths with forward slashes.
- Parse Max paths with `ntpath`, never `os.path`, so the suite runs on macOS.

## Engine / choreography

- **Stale handles cascade.** Fix N deletes a node fix N+2 targets. Every op needs
  a `still_applicable()` pre-check that **SKIPS**, never halts. Any exception in
  that pre-check degrades to a skip, never a blind apply.
- **A failed export must never reach a delete.** Constructive half verifies
  first; only then the destructive half.
- **Backup failure aborts the run** — and the report must not then claim
  "backup is intact".
- Headless `max undo` does **not** restore (GUI-only). The backup file is the
  only real safety net in batch.
- One raising rule must not abort the whole scan while the UI says "complete".
- Relative output dirs resolve against Max's CWD (Program Files) → PermissionError
  on default settings. Always resolve to absolute against the scene folder.
- `Settings.load` must tolerate garbage JSON (non-dict → defaults, never raise).
- Apply on the UI thread freezes Max at production scale.

## Performance

- Instance detection via `InstanceMgr` per node is O(n²) — 9-minute stall on
  ~2000 objects. Group by shared `baseObject` handle instead: O(n), 2.2 s.
- Hash **one representative per unique base object**, not every node.

## Machine-level

- ~12 back-to-back Max launches exhausted the pagefile and Max began crashing at
  boot. Kill zombie `3dsmax` / `3dsmaxbatch` processes between batch runs.
- Batch mode must never apply fixes a human hasn't reviewed — unless, as here,
  the op set is provably render-identical **and** backed up.

## New to MaxRescue (from research, unproven on box)

- **Peak RAM of a selective `mergeMAXFile` is undocumented.** It may approach
  full-open peak even for one node. This is spike #1 — the whole batching
  premise depends on it.
- `#promptMtlDups` and `#promptReparent` are the **defaults** — always pass
  explicit alternatives in batch or a dialog will hang the run.
- `3dsmaxbatch` exit codes worth handling: `-6` out of memory, `-7` could not
  create graphics device, `-8` license error, `-130` Max reported an error
  (a Python `logging.StreamHandler` to stdout is enough to cause -130 — log to file).
- Bitmap proxies: only `#renderMode_UseFullRes_FlushFromMemory` is
  render-identical. `#renderMode_UseProxies` **changes the render**.
- V-Ray **disables the bitmap pager during rendering** — it is an interactive
  lever only.
- Vantage loads V-Ray proxies **fully into VRAM**; proxying changes nothing for
  Vantage. Its only levers are its own dynamic-textures / LOD-bias settings.
- Live Link keeps a **second copy** of geometry+textures in host RAM.
- Modifiers left on a `VRayProxy` make it a regular mesh again — losing every
  memory saving.
