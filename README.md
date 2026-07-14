# ubanL

**Turn a small STL into a large statue.**

ubanL is a planned open tool in the spirit of [LuBan](https://www.luban3d.com/): it takes a
3D model that is larger than your printer's build volume (or a small model you want to
scale way up), automatically segments it into printable pieces, generates interlocking
connectors (pins, tenons, dovetails) at every cut so the pieces self-align during
assembly, and exports the pieces along with a numbered assembly guide.

## Status

🚧 **Planning phase — no code yet.**

The full technical plan, architecture, algorithm design, and roadmap live in
[`docs/PLAN.md`](docs/PLAN.md).

## Planned pipeline at a glance

```
STL in ──▶ repair ──▶ scale ──▶ cut planning ──▶ boolean cuts ──▶ connectors
                                                                      │
STL(s) out ◀── assembly guide ◀── labeling ◀── hollowing (opt.) ◀─────┘
```

## Planned v1 capabilities

- Load STL/OBJ/3MF, repair to watertight, scale to a target height
- Automatic segmentation so every piece fits a configured build volume
- Manual cut planes for full control when you want it
- Auto-generated connectors (dowel pins first; tenons/dovetails later) with
  configurable clearance
- Piece numbering engraved on interior cut faces
- Per-piece STL export + JSON assembly manifest + 3D exploded-view preview
- A printable calibration coupon to dial in connector tolerance for your printer

See [`docs/PLAN.md`](docs/PLAN.md) for the why behind every choice.
