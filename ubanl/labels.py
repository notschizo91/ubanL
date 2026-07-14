"""Piece ID engraving on cut faces.

Cut faces are flat by construction, so labeling is a shallow prism subtraction
in the interface's section frame. Labels are hidden once the statue is glued.
"""

from __future__ import annotations

import logging

import numpy as np
import shapely
import shapely.affinity
import trimesh
from shapely.geometry import Polygon

from .config import LabelConfig
from .connectors import _PROBE_MM, ConnectorReport
from .geometry import boolean, farthest_points_in_region, placeable_region
from .segmentation import Segmentation

log = logging.getLogger(__name__)


def text_polygons(text: str, height: float) -> list[Polygon]:
    """Render text to shapely polygons, scaled so glyph height == `height`,
    centered on the origin."""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath

    path = TextPath(
        (0, 0), text, size=100, prop=FontProperties(family="DejaVu Sans", weight="bold")
    )
    rings = [shapely.geometry.Polygon(p) for p in path.to_polygons() if len(p) >= 3]
    if not rings:
        return []
    # glyphs with holes (0, 8, A...) come back as separate rings: XOR them
    merged = rings[0]
    for ring in rings[1:]:
        merged = merged.symmetric_difference(ring)
    minx, miny, maxx, maxy = merged.bounds
    scale = height / max(maxy - miny, 1e-9)
    merged = shapely.affinity.scale(merged, xfact=scale, yfact=scale, origin=(0, 0))
    minx, miny, maxx, maxy = merged.bounds
    merged = shapely.affinity.translate(
        merged, xoff=-(minx + maxx) / 2.0, yoff=-(miny + maxy) / 2.0
    )
    polys = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
    return [p for p in polys if p.area > 1e-9]


def text_solid(text: str, height: float, depth: float, mirror_x: bool = False) -> trimesh.Trimesh:
    """Extruded text prism spanning z in [0, depth], centered in x/y."""
    polys = text_polygons(text, height)
    if mirror_x:
        polys = [shapely.affinity.scale(p, xfact=-1, yfact=1, origin=(0, 0)) for p in polys]
    prisms = [trimesh.creation.extrude_polygon(p, height=depth) for p in polys]
    return trimesh.util.concatenate(prisms)


def _label_spot(
    seg: Segmentation, report: ConnectorReport, piece_idx: int, inset: float
) -> tuple | None:
    """Best (interface, xy) to engrave this piece's ID: the piece's largest
    mating region, as far from its connectors as possible."""
    by_interface: dict[int, list[np.ndarray]] = {}
    for c in report.connectors:
        if piece_idx in (c.male_idx, c.female_idx):
            by_interface.setdefault(c.interface_idx, []).append(c.point_2d)
    best = None
    for iface_idx, pins in by_interface.items():
        interface = seg.interfaces[iface_idx]
        region = placeable_region(interface.section.polygons, inset)
        if region.is_empty:
            continue
        area = region.area
        if best is None or area > best[0]:
            best = (area, interface, pins, region)
    if best is None:
        return None
    _, interface, pins, region = best
    rng = np.random.default_rng(0)
    candidates = farthest_points_in_region(region, len(pins) + 4, grid_step=inset / 2, rng=rng)
    pins_arr = np.asarray(pins)
    xy = max(
        candidates,
        key=lambda q: float(np.min(np.linalg.norm(pins_arr - q, axis=1))) if len(pins_arr) else 0.0,
    )
    return interface, xy


def engrave_labels(
    seg: Segmentation, report: ConnectorReport, cfg: LabelConfig
) -> list[str]:
    """Engrave each piece's label on one of its cut faces, in place."""
    warnings: list[str] = []
    if not cfg.enabled:
        return warnings
    for piece in seg.piece_list():
        spot = _label_spot(seg, report, piece.idx, inset=cfg.height_mm)
        if spot is None:
            warnings.append(f"piece {piece.label}: no cut face with room for a label")
            continue
        interface, xy = spot
        section = interface.section
        p_world = section.point_to_world(xy)
        frame_z = section.to_3d[:3, 2]
        # which side of the section frame does this piece occupy here?
        probe = p_world + frame_z * _PROBE_MM
        on_positive = bool(piece.mesh.contains([probe])[0])
        # engrave into the piece; mirror so the text reads correctly when
        # looking at the piece's cut face from outside
        prism = text_solid(
            piece.label, cfg.height_mm, cfg.depth_mm + _PROBE_MM, mirror_x=on_positive
        )
        offset = trimesh.transformations.translation_matrix(
            [xy[0], xy[1], -_PROBE_MM if on_positive else -cfg.depth_mm]
        )
        transform = np.asarray(section.to_3d) @ offset
        prism.apply_transform(transform)
        engraved = boolean("difference", [piece.mesh, prism])
        if engraved.is_empty or not engraved.is_volume:
            warnings.append(f"piece {piece.label}: engraving failed, skipped")
            continue
        piece.mesh = engraved
    return warnings
