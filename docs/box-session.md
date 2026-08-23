# On-box session runbook

Everything that can be built and verified without 3ds Max is done. This is what
to run when you get to the machines, in order, and what each answer changes.

Budget roughly **30 minutes on the 256 GB box**. Steps 1–2 need no Max at all.

---

## Step 0 — before anything (2 min, on the Mac)

Copy one `.max` into `fixtures/` and run:

```bash
cd ~/Desktop/maxrescue && ./.venv/bin/python -m maxrescue.app.cli xray fixtures/scene.max
```

This needs no Max, no Windows, no plugins. It answers immediately:

- where the weight actually sits — one dominant object, or spread across many
- every plugin DLL the scene requires (so a missing one is known up front)
- whether Forest Pack / RailClone / tyFlow are present, which decides how much
  of Task 22 is real work
- whether anything scripted is embedded

**Also send me the node resolution rate.** If it is high, the tier-3 parser is
correct for Max 2026. If it is low, the number tells me exactly which chunk
layouts changed and I can fix them without guessing.

Then:

```bash
./.venv/bin/python -m maxrescue.app.cli plan fixtures/scene.max --ceiling-gb 70
```

---

## Step 1 — the spikes (20 min, 256 GB box)

Copy the repo to the box. From an elevated PowerShell:

```powershell
cd C:\path\to\maxrescue
$env:MAXRESCUE_ALLOW_FULL_OPEN = "1"     # authorises S3, which opens the whole scene
pwsh scripts\run_spikes.ps1 -Target "D:\jobs\villa\villa.max" -TimeoutSec 3600
```

Output lands in `spike-out\spike_report.json`. **Send me that file** — it is the
whole point of the session.

### What each spike decides

| Spike | Question | What the answer changes |
|---|---|---|
| **S0** | Max version, batch mode, psutil, renderer | Sanity. If `target_exists` is false, nothing else ran. |
| **S2** | Do three independent memory sources agree? | Whether any number the tool reports can be trusted. Also proves which array-index convention `getMAXMemoryInfo()` uses — currently undocumented, so the script dumps the raw array. |
| **S1** ⚠️ | **Does a selective `mergeMaxFile` deserialise the whole file?** | **The architecture.** `MERGE_SELECTIVE` → batching works, build Phase 3 as planned. `MERGE_FULL` → batching cannot bound peak RAM; the 256 GB in-session path becomes primary and P3 gets rewritten before a line of it is written. |
| **S3** ⚠️ | Where is the 170 GB — geometry or textures? | Whether the eight render-identical stages can reach 70 GB alone. If textures dominate, the opt-in Bitmap→VRayBitmap stage moves from optional to required. Your 22 GB open-to-open variance hints at textures. |

### If it fails

The launcher names every documented `3dsmaxbatch` exit code, so read its warning
first. The common ones:

- **`-6` out of memory** — expected on the 128 GB box for S3; not on the 256 GB one.
- **`-7` could not create a graphics device** — headless or RDP display issue.
- **`-8` license** — batch licensing on this entitlement; note it, it matters for CI.
- **`-130`** — Max reported an error; the listener log at `spike-out\_batch_2026.log` has it.

Sections are flushed to JSON **after each one**, so even a crash mid-battery
leaves everything that already ran. Send the partial file.

---

## Step 2 — the four open questions

These I cannot answer from here and they change the plan:

1. **Does Vantage run on the same 128 GB box as V-Ray?** If yes, Live Link holds
   a second copy of geometry and textures in host RAM, and the 70 GB budget must
   come down.
2. **Is the scene animated**, or is it stills / camera-only? Animated objects
   narrow the collapse and proxy stages considerably.
3. **What is the file's size on disk?** With the S1 numbers this gives the real
   disk→RAM multiplier, which replaces the governor's ×12 prior with a measured
   figure.
4. **Which of Forest Pack / RailClone / tyFlow / Phoenix are in use?** The X-ray
   in step 0 answers this from the DLL directory without opening the file.

---

## What happens next

- **S1 returns `MERGE_SELECTIVE`** → I build Phase 3 (merge primitives, batch
  loop) against real API behaviour, then the reduction stages.
- **S1 returns `MERGE_FULL`** → we revise the plan first. The 256 GB in-session
  path still gets you a lightened file; it just does not remove the big-box
  dependency, and I would say so plainly rather than build around it.

Either way the X-ray and the governor stand — neither depends on the answer.
