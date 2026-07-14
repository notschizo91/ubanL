# ubanL — Technical Plan

> Status: **planning document, pre-code**. This is the design we intend to build against.
> Everything here is open to revision before the first line of code.

## 1. Vision

Take any watertight-ish 3D model and make it printable at *any* size on *any* printer:

1. **Scale** a small STL to statue size (e.g., a 120 mm figurine → 1.2 m statue).
2. **Segment** it into pieces that each fit the printer's build volume.
3. **Connect** the pieces with auto-generated joinery (pins/sockets, tenons, dovetails)
   so assembly is self-aligning and glue-ready.
4. **Guide** the user: numbered pieces, an assembly order, and a 3D exploded preview.

The reference product is [LuBan](https://www.luban3d.com/) (closed-source freeware,
originally out of MIT/SUTD research). ubanL aims at LuBan's core "Make It BIG" workflow
as an open, scriptable, automatable tool.

## 2. Prior art (what we're learning from)

| Tool / work | What it does | What we take from it |
|---|---|---|
| **LuBan** | Interactive cutting planes; plug / dowel / terrace / pyramid connectors with depth + tolerance control; residual-part detection; part numbering + assembly sequence | The feature bar for v1/v2. Its connector-type menu and tolerance controls are the UX to match. |
| **Chopper** (Luo, Baran, Rusinkiewicz, Matusik — SIGGRAPH Asia 2012) | BSP partitioning of meshes into printable parts, optimizing part count, connector feasibility, structural soundness, seam unobtrusiveness | The algorithmic blueprint for our automatic cut planner (Stage C, Milestone 3). |
| **Dapper** (Chen et al. 2015) | Decomposition *and* packing jointly optimized | Informs the later "pack pieces onto plates" milestone. |
| **PrusaSlicer Cut tool** (2.5+) | Single-plane cuts with dowel pins and snap connectors inside a slicer | Proof that dowel pins + clearance presets are the pragmatic default connector. |
| **pychop3d** | Open-source Python implementation of Chopper on top of trimesh | Validates our stack choice; we'll study it, not depend on it. |
| **Split3r** (2025, commercial) | Recent low-cost LuBan alternative | Evidence there's real demand; watch its UX decisions. |
| **Meshmixer** (discontinued) | Manual plane cuts, no connectors | Cautionary tale: manual-only cutting without joinery is not enough. |

## 3. Target users & core user stories

- **The statue maker**: "I bought a 100 mm miniature STL. I want it 1 m tall on my
  250 mm³ printer. Give me pieces, pins, and an order to glue them in."
- **The prop/cosplay builder**: "Segment this helmet so seams land where I'll sand
  anyway, and keep piece count low."
- **The print farm**: "Scriptable CLI: same model, three printer profiles, batch output."

Explicit v1 persona: **FDM printers, PLA/PETG, glued assembly**. Resin support
(drain holes, hollowing priorities) is planned but not v1-blocking.

## 4. Scope

### In scope (v1 = Milestones 0–3)

- STL/OBJ/3MF input; repaired, watertight, manifold internal representation
- Uniform scaling to target height / scale factor
- Segmentation: manual planes (config-specified) **and** automatic planning
- Robust boolean cutting with guaranteed-manifold output
- Connectors: cylindrical dowel pin + socket with configurable clearance, depth, count
- Piece ID engraving on cut faces
- Per-piece STL export, JSON assembly manifest, self-contained HTML exploded preview
- Printable tolerance calibration coupon generator
- CLI + YAML project config

### Later (v2+)

- Connector types: tenon (rectangular plug), pyramid/terrace (LuBan parity), dovetail
  (slide-in, mechanical hold without glue)
- Hollowing with wall thickness + drainage holes (resin workflow)
- Seam-aware planning (hide cuts in concave/low-visibility regions)
- Build-plate packing of pieces; print-orientation suggestion (flat cut face down)
- Interactive GUI (web viewer with draggable cut planes)
- Internal structures for very large statues: alignment ribs, threaded-rod channels

### Out of scope (not this project)

- Slicing / G-code (that's the slicer's job)
- Sculpting or mesh editing beyond repair
- Lithophanes, vase mode, and the rest of LuBan's side features

## 5. The pipeline

Deterministic stages; each stage's output is cacheable so users can iterate on
downstream parameters (e.g., re-tune connector clearance) without re-running planning.

```
 A. Ingest      load STL/OBJ/3MF → repair → validate watertight/manifold → units
 B. Scale       target height or scale factor; report volume/mass/filament estimate
 C. Plan        choose cut planes (manual list, grid, or BSP optimizer)
 D. Cut         apply planes via robust booleans → piece meshes + interface records
 E. Connect     per interface: place N connectors; union male half, subtract
                clearance-dilated female half
 F. Finish      engrave piece IDs on cut faces; (v2) hollow + drain holes
 G. Report      per-piece STL/3MF, manifest.json, exploded-view HTML, assembly order
```

### Stage A — Ingest & repair

- Loader: `trimesh` (handles STL/OBJ/3MF/PLY out of the box).
- Validation: watertight, consistent winding, no self-intersections that break booleans.
- Repair ladder (try cheapest first):
  1. trimesh built-ins (fill holes, fix normals, merge vertices)
  2. PyMeshLab repair filters
  3. **Fallback: voxel/SDF remesh** — guaranteed to produce a manifold shell at the
     cost of detail; user opts in with a resolution parameter.
- Hard requirement: everything downstream assumes a manifold mesh. If repair fails,
  stop with a diagnostic, never emit garbage pieces.

### Stage B — Scale

- Input: `target_height_mm` or `scale_factor`. Uniform only.
- Side outputs users care about: scaled bounding box, per-piece estimates later
  (volume → filament grams via density), sanity warning if any *feature* (e.g., a
  sword blade) will be thinner than N perimeters at target scale.

### Stage C — Cut planning (the interesting part)

Three planners behind one interface, shipped in this order:

1. **Manual** (M1): user lists planes in config (`point + normal` or named presets
   like `z=140`). This unblocks real use immediately and is the debugging harness for
   everything else.
2. **Grid** (M1): axis-aligned planes at intervals ≤ build volume; trivially correct,
   often ugly. It is the baseline the optimizer must beat.
3. **BSP optimizer** (M3, Chopper-style):
   - Search space: binary space partition; at each step, pick the largest
     still-oversized piece and evaluate candidate planes (normals from a uniform
     sphere sampling ~O(100) + offsets swept along each normal).
   - **Beam search** (beam width ~10) rather than greedy, since early cuts constrain
     later ones.
   - Objective = weighted sum, all terms normalized 0–1:
     - `part_count` — fewer pieces is better
     - `utilization` — pieces should use the build volume, not shave slivers
     - `connector_feasibility` — every interface cross-section must fit at least
       one connector with margin (hard constraint below a threshold)
     - `fragility` — penalize planes that cross thin features (estimated via local
       thickness / SDF sampling)
     - `seam_visibility` (v2) — prefer seams in concave creases and low-curvature
       regions (ambient-occlusion proxy)
     - `symmetry` (v2) — bonus for cuts respecting detected symmetry planes
   - Feasibility check per candidate piece = oriented-bounding-box fit into the build
     volume (allowing rotation), not just AABB — diagonal placement is free capacity.

### Stage D — Boolean cutting

- Engine: **Manifold** (`manifold3d`) via trimesh's boolean interface. Chosen because
  it guarantees manifold output and is fast enough to run inside the planner's inner
  loop; fallback engines (Blender, OpenSCAD) stay available through trimesh if needed.
- Performance rule: the *planner* (Stage C) evaluates candidates on a **decimated
  proxy** (~50–100k faces); final cuts (Stage D) run once on the full-resolution mesh.
- Each cut records an **interface**: the pair of piece IDs plus the planar
  cross-section polygon(s) (with holes) in the plane's 2D frame. Interfaces drive
  connectors, labeling, and the assembly graph.

### Stage E — Connector generation

The heart of the "fit them together" requirement.

- **v1 connector: cylindrical dowel pin + socket.** Round pins are rotation-tolerant,
  print cleanly both vertically and as separate pieces, and are what PrusaSlicer
  standardized on. Parameters: diameter, length, count, clearance, chamfer.
- **Placement algorithm** per interface:
  1. Take the cross-section polygon(s) from Stage D (as `shapely` polygons).
  2. Inset by `pin_radius + wall_margin` (default margin ≈ 2× nozzle width) →
     the *placeable region*.
  3. If the region is empty → interface is pin-infeasible → warn, or in auto-planning
     mode this plane was already rejected in Stage C.
  4. Place N pins maximizing pairwise distance (farthest-point sampling on the region;
     medial-axis seeding for skinny regions). Two pins minimum per interface when area
     allows — one pin allows rotation.
  5. **Male side**: union a chamfered cylinder protruding `pin_length`.
     **Female side**: subtract a cylinder dilated by `clearance` and deepened by a
     small bottom gap (glue pocket, default 0.5 mm).
- **Tolerance model**: single `clearance` parameter, default 0.20 mm for FDM,
  overridable per material/printer profile. Because "the right clearance" is
  printer-specific, v1 ships a **calibration coupon generator**: a small plate with
  sockets at 0.10/0.15/0.20/0.25/0.30 mm and one pin; the user prints it, finds the
  best fit, writes the number in their profile. This one feature eliminates the #1
  support question every segmentation tool gets.
- **v2 connectors**: rectangular tenon (indexes rotation with one connector),
  pyramid/terrace (LuBan parity, easy to print, self-centering), dovetail rail
  (mechanical hold for pieces that shouldn't rely on glue alone).
- **Orientation rule**: connector axes are always normal to the cut plane, so the flat
  cut face can be printed bed-down and the pin prints as clean concentric circles.

### Stage F — Finish

- **Labeling (v1)**: engrave piece ID (e.g., `A3`) 0.6 mm deep on a cut face — cut
  faces are flat by construction, so text boolean is trivial and the label is hidden
  after assembly. Font rendered to polygons via a vendored single-stroke font or
  `matplotlib` text-path (build-time decision).
- **Hollowing (v2)**: inward offset via SDF/voxel grid (OpenVDB or `pysdf`-style
  approach), wall thickness parameter, automatic drain holes for resin (lowest point
  per piece in print orientation).

### Stage G — Reporting & export

- Per-piece `STL` (default) and `3MF` (keeps piece names/metadata).
- `manifest.json`: pieces (ID, bbox, volume, est. filament), interfaces (piece pair,
  connector list with positions), assembly order.
- **Assembly order**: topological order over the interface graph, bottom-up along the
  statue's Z by default; ties broken by piece size (big pieces first = stable base).
- **Exploded preview**: single self-contained HTML file (three.js + embedded glTF,
  pieces offset along their cut normals, slider to collapse/explode, click a piece to
  see its ID and neighbors). No server needed — double-click to open.

## 6. Architecture

### Shape: Python library + CLI

```
ubanl/
  io/         load, save, formats, units
  repair/     validation, repair ladder, voxel-remesh fallback
  plan/       planners: manual, grid, bsp; objective terms; proxy decimation
  cut/        boolean execution, interface extraction
  connect/    connector types, placement, tolerance profiles, calibration coupon
  finish/     labeling, (v2) hollowing
  report/     manifest, exploded HTML, assembly ordering
  cli.py      `ubanl chop model.stl --config project.yaml`
  config.py   pydantic models for project + printer profiles
tests/
  golden/     small reference meshes (bunny, benchy-like) + snapshot outputs
docs/
```

- **Why Python**: the entire required ecosystem is mature there — `trimesh`
  (mesh IO/ops), `manifold3d` (robust booleans, native-speed), `shapely` (2D
  cross-section work), `numpy`/`scipy` (sampling, search), `pymeshlab` (repair).
  The compute-heavy pieces (booleans, SDF) run in native extensions, so Python is
  orchestration, not bottleneck. A C++ or Rust core would delay an MVP by months for
  no v1 benefit; Manifold's WASM build keeps a future browser port open regardless.
- **Core boundary rule**: the library never touches the filesystem except in `io/` and
  `report/`; everything else is mesh-in/mesh-out pure functions. This keeps the door
  open for a server or GUI wrapper without refactoring.
- **Config-first UX**: a `project.yaml` fully describes a job (input, target size,
  printer profile, planner choice, connector params, manual planes if any). The CLI is
  a thin runner. Printer profiles are shareable snippets.

### Key data model

```python
CutPlane(origin, normal, id)
Interface(plane_id, piece_a, piece_b, sections: list[Polygon2D])
Connector(interface_id, kind, position, axis, params)      # kind: dowel | tenon | ...
Piece(id, mesh, source_interfaces, oriented_bbox, label_pos)
Project(mesh_src, scale, printer: BuildVolume+ToleranceProfile,
        planner, connectors, pieces, assembly_order)
```

### Determinism & caching

Same config + same input ⇒ byte-identical output (fixed RNG seeds in sampling).
Stages cache to a `.ubanl/` work dir keyed by config-hash so parameter iteration is
fast — critical because users will re-run with tweaked clearances many times.

## 7. Milestones

Each milestone ends in something a user can actually run.

| M | Deliverable | Acceptance criteria |
|---|---|---|
| **M0** | Scaffold: package, CLI skeleton, config schema, CI, golden test meshes | `ubanl info model.stl` prints dims/watertightness; CI green |
| **M1** | Manual + grid cutting, no connectors | 100 mm bunny → 400 mm target on a 220³ profile yields pieces that each OBB-fit the volume; volumes sum to original ±0.1 % |
| **M2** | Dowel connectors + calibration coupon + labels + manifest | Two-piece cut assembles in-CAD with exact clearance; coupon STL prints valid; every piece labeled |
| **M3** | BSP auto-planner (beam search, feasibility + part-count + fragility terms) | Auto plan ≤ grid plan's piece count on the golden set, never infeasible connectors; plans a 500k-face mesh in < 5 min on a laptop |
| **M4** | Exploded HTML preview + assembly ordering | Single-file HTML opens offline, explode slider works, order is bottom-up valid |
| **M5** | v2 connectors (tenon, pyramid), seam-visibility term, printer profile library | Seam-aware plan visibly prefers creases on test heads/busts |
| **M6** | Hollowing + drain holes (resin), plate packing | Hollowed piece maintains min wall thickness; packing fits M3 output on fewer plates than naive |

v1 ships after **M4**.

## 8. Testing strategy

- **Property tests on every pipeline run** (cheap, run on golden meshes in CI):
  - every output piece is watertight & manifold
  - every piece OBB-fits the build volume
  - Σ piece volumes = input volume ± ε (before connectors), and after connectors the
    male/female volume delta matches connector math
  - every interface has ≥ 1 connector or an explicit waiver flag
  - male pin ⊖ female socket ≥ clearance everywhere (sampled SDF check)
- **Golden snapshots**: manifest.json diffs on fixed seeds catch behavioral drift.
- **The physical test** (manual, per release): print the calibration coupon and a
  two-piece sphere on a real printer; pins must fit at nominal clearance.

## 9. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Dirty input meshes break booleans | High — most downloaded STLs are dirty | Repair ladder ending in voxel-remesh fallback; refuse-with-diagnostic rather than corrupt output |
| BSP search too slow on big meshes | Medium | Plan on decimated proxy; beam width and normal-sample count are tunable; grid planner always available |
| Connector fit varies by printer | Certain | Calibration coupon + per-profile clearance; conservative default |
| Thin features (swords, fingers) sliced into fragile slivers | Medium | Fragility term in objective; minimum-piece-thickness warning in report |
| Scope creep toward a GUI before the core is solid | Medium | GUI is explicitly post-v1; exploded HTML preview covers 80 % of the "can I see it?" need |
| trimesh 5.x API churn | Low | Pin versions; boolean calls isolated in `cut/` behind one adapter |

## 10. Open questions (answer before M2)

1. **Printer target**: default profile 220×220×250 FDM? Resin priority level?
2. **Threaded inserts / bolts**: statue makers often want M3-insert bosses at
   interfaces for demountable assembly — v2 candidate, worth confirming demand.
3. **Piece-count vs seam-placement priority**: default objective weights need a user
   opinion (fewest pieces vs prettiest seams).
4. **License**: MIT/Apache-2.0 for the tool; confirm no reuse of LuBan assets/naming
   beyond inspiration.

## 11. Explicit non-goals for v1 (so we ship)

- No GUI, no slicing, no supports, no multi-material, no seam painting UI,
  no automatic scaling from a photo, no cloud anything.
