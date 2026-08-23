# MaxRescue

> Plan: **[tasks/plan.md](tasks/plan.md)** · Checklist: **[tasks/todo.md](tasks/todo.md)**
> · Traps: **[docs/gotchas.md](docs/gotchas.md)** · Contract: **[CLAUDE.md](CLAUDE.md)**

Headless memory reduction for 3ds Max scenes too large to work with — **without
changing a rendered pixel**.

Reads the `.max` container directly (OLE2, no Max involved), then drives
`3dsmaxbatch.exe` to rebuild the scene into an empty session in batches under a
memory governor, applying only render-identical operations.

**v1 target:** one real archviz scene, **170–192 GB → ≤70 GB** open RSS, on a
128 GB / 32 GB-VRAM box, with V-Ray 7 renders bit-identical to the original.
Pipeline: 3ds Max 2026 · V-Ray 7 · Chaos Vantage 3.3.1.

---

## What works today: `maxrescue xray`

Reads a `.max` file and tells you what is in it. **Needs no 3ds Max, no Windows,
no plugins** — it parses the container itself, so it works on a file that
crashes on open.

```bash
python -m maxrescue.app.cli xray /path/to/scene.max
```

Machine-readable output:

```bash
python -m maxrescue.app.cli xray /path/to/scene.max --json report.json
```

It answers:

- **Why won't this open?** — one dominant object, spread bloat, a damaged chunk
  tree, or a known malware payload.
- **Which plugins does it need?** — every DLL the file declares, so a missing
  plugin is identified before burning a 45-second Max launch.
- **Is anything scripted embedded in it?** — ALC / CRP / ADSL / PhysXPluginMfx /
  MSCPROP signatures, plus MAXScript classes and unsafe-command tokens.
- **Where is the weight?** — per-class histogram and the heaviest single objects.

Exit codes: `0` fine · `2` unreadable file · `3` known payload signature found.

### What it deliberately does not tell you

**In-memory weight.** Every byte figure is an *on-disk* chunk weight. The
multiplier from disk bytes to RAM depends on class, modifier-stack depth and
instancing, and has not been calibrated against a real scene yet — spike S3 in
the plan measures it on the box. A made-up RAM figure would be the most quotable
and least true number in the report.

**Safety.** A clean signature report means "no *known* signature", never "this
file is safe". Autodesk's own Scene Security Tools carry the same limitation.

---

## Ready for the box

Everything buildable without 3ds Max is done. **[docs/box-session.md](docs/box-session.md)**
is the runbook: what to run, in what order, and what each answer changes.

```powershell
$env:MAXRESCUE_ALLOW_FULL_OPEN = "1"
pwsh scripts\run_spikes.ps1 -Target "D:\jobs\villa\villa.max"
```

## Not built yet

`rescue` and `verify` arrive with phases 3–5 of [the plan](tasks/plan.md). They
are absent rather than stubbed — a subcommand that exists but does nothing is
worse than one that does not exist.

The whole batch-merge design rests on one undocumented behaviour: whether a
*selective* `mergeMaxFile` deserialises the entire file. That is spike S1, and
it is deliberately the second task in the plan.

---

## Development

```bash
python3 -m venv .venv && ./.venv/bin/python -m pip install -e ".[dev]"
./.venv/bin/python -m pytest
```

The `xray/` and `core/` packages are pure — no `pymxs`, no Qt — and a test
enforces it, so the entire suite runs on macOS in well under a second with no
3ds Max anywhere. `maxbridge/` is the only place Max is allowed to exist.

Sibling of MaxSlim / MaxOptimizer / MaxGaffer / MaxDirector; the hardened safety
machinery (backup, undo transactions, material guard, planner guards) is
vendored from those repos rather than rewritten.
