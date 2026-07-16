# ubanL

**Turn a small STL into a large statue.**

ubanL is an open tool in the spirit of [LuBan](https://www.luban3d.com/): it takes a
3D model that is larger than your printer's build volume (or a small model you want to
scale way up), segments it into printable pieces, generates interlocking dowel
connectors at every cut so the pieces self-align during assembly, and exports the
pieces along with a numbered assembly guide and an exploded 3D preview.

```
STL in ──▶ repair ──▶ scale ──▶ cut planning ──▶ boolean cuts ──▶ connectors
                                                                      │
STL(s) out ◀── assembly guide ◀── ID engraving ◀──────────────────────┘
```

## Install

```bash
pip install -e .          # from a checkout
pip install -e .[fast]    # + faster planning on huge meshes (decimation)
```

## Quick start — web UI

```bash
ubanl serve
```

Opens a local web app at `http://127.0.0.1:8000`: drag in a model, pick a target
height and printer, hit **Chop it**, inspect the pieces in an interactive exploded
3D view, and download individual STLs or everything as a zip (three.js is bundled —
no internet needed). The calibration coupon is one click away in the sidebar.

## Quick start — CLI

```bash
# what am I working with?
ubanl info figurine.stl

# scale to a 1 m statue, chop for a 220x220x250 printer, add pins + labels
ubanl chop figurine.stl --target-height 1000 --build-volume 220x220x250 -o out/

# dial in the pin fit for your printer/material first (one-time)
ubanl coupon -o coupon/    # print both parts, pick the snuggest clearance
ubanl chop figurine.stl --target-height 1000 --clearance 0.15 -o out/
```

Outputs in `out/`:

- `P01.stl`, `P02.stl`, … — one file per piece, named in assembly order
- `manifest.json` — piece sizes, weights, connector positions, assembly order
- `exploded_preview.html` — interactive 3D exploded view (open in a browser)
- `assembled.glb` — the assembled statue as one scene

Every piece carries its ID engraved on a cut face (hidden after gluing), and every
joint gets chamfered dowel pins and matching sockets with your configured clearance.

## Project config

Anything the flags can do (and more) lives in a YAML project file — see
[`examples/statue.yaml`](examples/statue.yaml):

```bash
ubanl chop examples/statue.yaml
```

## Planners

| mode | what it does |
|---|---|
| `auto` (default) | Greedy BSP search: minimizes piece count, keeps every joint big enough for pins, avoids sliver cuts; falls back to the grid plan if that's ever better |
| `grid` | Axis-aligned cuts at build-volume intervals; predictable and always feasible |
| `manual` | You list the planes (`{axis: z, position: 400}` or point + normal) |

## Status

v0.1 implements the plan's milestones M0–M2 plus the first pass of M3 (auto planner)
and M4 (exploded preview). See [`docs/PLAN.md`](docs/PLAN.md) for the full design and
what's next (tenon/dovetail connectors, seam-aware planning, hollowing, plate packing).

## Development

```bash
pip install -e .[dev]
pytest            # property tests: watertightness, volume conservation, fit, clearance
```
