# Why the render does not change

The whole tool rests on one claim: **the rescued scene renders identically to
the original.** This is what that claim actually rests on, and how you check it
rather than take my word for it.

---

## Proxies: the part people worry about, and why

Converting a mesh to a V-Ray proxy *looks* lossy. It is not, and the reason is
specific rather than reassuring.

A `.vrmesh` stores, in Chaos's words, **"everything that is needed to render the
mesh"** — vertices, face topology, texture channels, face material IDs,
smoothing groups, and normals. Explicit vertex normals are written too when the
source mesh has them. Nothing is decimated, simplified or approximated on
export.

The thing that makes a proxy cheap is **where the geometry lives**, not how much
of it there is: *"VRayProxy imports a geometry from an external mesh at render
time only. The geometry is not present in the scene, and does not use up any
resources, except for the viewport preview."*

So the memory saving comes from the mesh sitting on disk between renders instead
of in RAM. At render time the full mesh streams back in, chunk by chunk, at full
resolution.

### Bounding-box display is a viewport setting

This is the single most common misreading. Setting a proxy's display to
`bounding box` makes it a box **in the viewport**. It has no effect on the
render — the renderer never consults the display mode; it loads the mesh file.

That is why the display mode is set aggressively and the render settings are
not touched at all.

### What could actually degrade a proxy, and what stops it

| Risk | What it would do | Guard |
|---|---|---|
| Point-cloud export | Renders simplified point geometry instead of the mesh | `exportPointClouds=False` on export; `point_cloud_on` and its level multiplier asserted neutral on the created proxy |
| **LOD Scale** | Loads a coarser vrmesh level at render time | Asserted at its neutral value after conversion |
| Condensed material IDs | Renumbers IDs, so a Multi/Sub renders the wrong material on every face | `condenseMultiMtl=False`, `createMultiMtl=True` |
| Single-file export | Bakes world transforms into the mesh; combined with `autoCreateProxies` the object is displaced by its own position | `exportMultiple=True` (per-object), plus a **bounding-box comparison** before and after — the only check that catches a transform applied twice |
| Scale / flip-axis / map-channel remap | Changes size, orientation or texture placement | Each asserted neutral after conversion |
| Mirrored or negative-scale nodes | Normals are reversed in local space and do not survive the export | Refused outright and logged |
| A modifier that contributes at render time | Displacement, hair, render-time subdivision are baked away silently — a proxy captures the *evaluated viewport* mesh | Only modifiers on a proven-bakeable **allowlist** are converted; anything unrecognised is refused |
| A modifier left *on* the proxy | Chaos: "When a modifier is added, the proxy loses its memory-saving properties and is regarded as a regular mesh" | Never applied |

A proxy that cannot be confirmed neutral is a **failure**, not a warning — the
conversion raises and the run halts with the backup intact.

---

## The other seven automatic stages

| Stage | Why it cannot change the render |
|---|---|
| Hygiene (bitmap cache, undo buffer, unused materials, Motion Mixer) | Frees data nothing renders from |
| Hidden / non-renderable removal | Only runs when `rendHidden` is off, so these objects render nothing. Anything invisible-to-camera but still lighting the scene via GI or reflections is **kept** |
| Modifier-stack collapse | Bakes the same evaluated mesh the renderer would have used; render-coupled subdivision, displacement and deformation stacks are excluded |
| Bitmap paging | `UseFullRes_FlushFromMemory` — full-resolution textures at render time, paged out between. The mode that renders downscaled images (`UseProxies`) raises if anything ever asks for it |
| Nitrous texture caps | Viewport texture memory only |
| Scatter viewport display | Viewport display group only. Currently discovers and **reports** rather than acting, because the vendor property names and values are undocumented and the render group sits beside them |

**One stage is not render-identical, and it is opt-in.** Bitmap→VRayBitmap
changes the filtering kernel by design — Chaos document this. It requires an
explicit flag, refuses to run without a diff gate, and rolls back entirely if
the diff fails.

---

## How you check, instead of trusting the above

Reasoning is not proof. Render the same frame before and after and compare the
pixels.

**On the machine where the scene still opens** (the reference has to come from
the original, and the original is the file that does not fit):

```powershell
pwsh scripts\run_reference.ps1 -Target "D:\jobs\villa\villa.max" -Camera "Cam_Hero"
```

That renders `verify-out\reference.exr` and saves the light cache beside it.

**Then rescue with the same settings**, and it renders the result and compares
automatically:

```powershell
$env:MAXRESCUE_REFERENCE  = "…\verify-out\reference.exr"
$env:MAXRESCUE_LIGHTCACHE = "…\verify-out\reference.vrlmap"
$env:MAXRESCUE_CAMERA     = "Cam_Hero"
pwsh scripts\run_rescue.ps1 -Target "D:\jobs\villa\villa.max"
```

- **`RESCUE_VERIFIED`** — the two frames are bit-identical.
- **`RESCUE_DIFFERENT`** — they are not. That is a bug in a stage, not a
  tolerance to widen; send me the report and both EXRs.

The comparison is **bit-exact**. Every automatic stage claims identity, so any
non-zero pixel difference is a failure. There is no "close enough" setting.

### Why determinism has to be forced

Two renders of an *unmodified* scene differ unless you pin things down, and a
gate that fires on its own noise gets switched off within a day. Both renders
use: locked noise pattern, a fixed sampling seed, the bucket sampler (whose
stopping point does not depend on elapsed time), the **same saved light cache**
loaded from file rather than recomputed, the denoiser off, and raw EXR output.

---

## What this cannot promise

The eight automatic stages are argued above from Chaos's own documentation, and
every argument has a guard and a test behind it. But **none of the bridge code
has ever executed** — it runs inside 3ds Max on Windows, and until it does, the
guards are reasoning rather than evidence.

That is exactly why the reference render exists, and why it is worth doing on
the first run rather than the tenth.
