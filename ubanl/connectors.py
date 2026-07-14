"""Dowel connectors: placement on cut interfaces, pin/socket solids, application.

Placement works on the 2D section polygons of each interface: inset by the pin
radius plus a wall margin, spread points by farthest-point sampling, then map
back to 3D and resolve which final piece hosts the pin (male) and which the
socket (female) by point containment — this stays correct even when later cuts
subdivided the pieces an interface originally separated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import shapely
import trimesh

from .config import ConnectorConfig, PrinterConfig
from .geometry import boolean, farthest_points_in_region, placeable_region, split_solid
from .segmentation import Interface, Segmentation

log = logging.getLogger(__name__)

#: how far off the plane to probe when deciding which piece is on which side
_PROBE_MM = 0.5


@dataclass
class PlacedConnector:
    interface_idx: int
    point_2d: np.ndarray  # in the interface's section frame
    point_world: np.ndarray
    axis_world: np.ndarray  # pin protrudes from the male piece along this
    male_idx: int
    female_idx: int


@dataclass
class ConnectorReport:
    connectors: list[PlacedConnector] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def pin_solid(cfg: ConnectorConfig, sections: int = 48) -> trimesh.Trimesh:
    """Chamfered pin along +Z: embedded below z=0, protruding to z=length."""
    r = cfg.diameter_mm / 2.0
    ch = cfg.chamfer_mm
    embed = cfg.length_mm  # bury as much as protrudes; harmless inside material
    profile = np.array(
        [
            [0.0, -embed],
            [r, -embed],
            [r, cfg.length_mm - ch],
            [r - ch, cfg.length_mm],
            [0.0, cfg.length_mm],
        ]
    )
    return trimesh.creation.revolve(profile, sections=sections)


def socket_solid(
    cfg: ConnectorConfig, clearance: float, sections: int = 48
) -> trimesh.Trimesh:
    """Socket cavity along +Z: pierces the face at z=0, chamfered opening."""
    radius = cfg.diameter_mm / 2.0 + clearance
    ch = cfg.chamfer_mm
    depth = cfg.length_mm + clearance + cfg.bottom_gap_mm
    profile = np.array(
        [
            [0.0, -_PROBE_MM],
            [radius + ch, -_PROBE_MM],
            [radius, ch],
            [radius, depth],
            [0.0, depth],
        ]
    )
    return trimesh.creation.revolve(profile, sections=sections)


def _target_count(area: float, cfg: ConnectorConfig) -> int:
    by_area = int(np.sqrt(area) / (4.0 * cfg.diameter_mm)) + 1
    return int(np.clip(by_area, cfg.min_count, cfg.max_count))


def _pieces_containing(
    pieces: list, points: np.ndarray, tol: float = 1e-6
) -> list[int | None]:
    """For each probe point, the idx of the (unique) piece containing it."""
    out: list[int | None] = [None] * len(points)
    for piece in pieces:
        lo, hi = piece.mesh.bounds
        near = np.all((points > lo - tol) & (points < hi + tol), axis=1)
        if not near.any():
            continue
        idx = np.flatnonzero(near)
        inside = piece.mesh.contains(points[idx])
        for i in idx[inside]:
            out[i] = piece.idx
    return out


def _oriented(solid: trimesh.Trimesh, point: np.ndarray, axis: np.ndarray) -> trimesh.Trimesh:
    transform = (
        trimesh.transformations.translation_matrix(point)
        @ trimesh.geometry.align_vectors([0, 0, 1], axis)
    )
    placed = solid.copy()
    placed.apply_transform(transform)
    return placed


def place_connectors(
    seg: Segmentation, cfg: ConnectorConfig, printer: PrinterConfig, seed: int = 0
) -> ConnectorReport:
    """Decide connector locations and male/female assignment; mutates nothing."""
    report = ConnectorReport()
    if not cfg.enabled:
        return report
    rng = np.random.default_rng(seed)
    pieces = seg.piece_list()
    inset = cfg.diameter_mm / 2.0 + cfg.wall_margin_mm

    for interface in seg.interfaces:
        section = interface.section
        region = placeable_region(section.polygons, inset)
        if region.is_empty:
            report.warnings.append(
                f"interface {interface.idx}: cross-section too small for any "
                f"{cfg.diameter_mm} mm pin; pieces there must be aligned by hand"
            )
            continue
        count = _target_count(section.area, cfg)
        points_2d = farthest_points_in_region(
            region, count, grid_step=cfg.diameter_mm / 2.0, rng=rng
        )
        placed_here = 0
        for xy in points_2d:
            p_world = section.point_to_world(xy)
            axis = interface.normal
            probes = np.array([p_world - axis * _PROBE_MM, p_world + axis * _PROBE_MM])
            male_idx, female_idx = _pieces_containing(pieces, probes)
            if male_idx is None or female_idx is None or male_idx == female_idx:
                continue
            # a later cut may have moved a wall close to this point: require the
            # pin's footprint (plus half the margin) to stay inside both pieces
            ring_r = cfg.diameter_mm / 2.0 + cfg.wall_margin_mm / 2.0
            angles = np.linspace(0, 2 * np.pi, 8, endpoint=False)
            ring_2d = xy + ring_r * np.stack([np.cos(angles), np.sin(angles)], axis=1)
            ring_world = np.array([section.point_to_world(q) for q in ring_2d])
            ok = True
            for offset, want in ((-axis * _PROBE_MM, male_idx), (axis * _PROBE_MM, female_idx)):
                owners = _pieces_containing(pieces, ring_world + offset)
                if any(o != want for o in owners):
                    ok = False
                    break
            if not ok:
                continue
            report.connectors.append(
                PlacedConnector(
                    interface_idx=interface.idx,
                    point_2d=np.asarray(xy),
                    point_world=p_world,
                    axis_world=axis.copy(),
                    male_idx=male_idx,
                    female_idx=female_idx,
                )
            )
            placed_here += 1
        if placed_here == 0:
            report.warnings.append(
                f"interface {interface.idx}: no valid connector position found; "
                "pieces there must be aligned by hand"
            )
        elif placed_here == 1:
            report.warnings.append(
                f"interface {interface.idx}: only one pin fits — rotation is not "
                "indexed there, mark the pieces before gluing"
            )
    return report


def apply_connectors(
    seg: Segmentation, report: ConnectorReport, cfg: ConnectorConfig, printer: PrinterConfig
) -> None:
    """Union pins into male pieces and carve sockets from female pieces, in place."""
    if not report.connectors:
        return
    pin = pin_solid(cfg)
    socket = socket_solid(cfg, clearance=printer.clearance_mm)
    adds: dict[int, list[trimesh.Trimesh]] = {}
    subs: dict[int, list[trimesh.Trimesh]] = {}
    for c in report.connectors:
        adds.setdefault(c.male_idx, []).append(_oriented(pin, c.point_world, c.axis_world))
        subs.setdefault(c.female_idx, []).append(
            _oriented(socket, c.point_world, c.axis_world)
        )
    for idx, piece in seg.pieces.items():
        mesh = piece.mesh
        if idx in adds:
            mesh = boolean("union", [mesh, *adds[idx]])
        if idx in subs:
            mesh = boolean("difference", [mesh, boolean("union", subs[idx])])
        if idx in adds or idx in subs:
            bodies = split_solid(mesh, min_volume=mesh.volume * 1e-9)
            if len(bodies) != 1:
                seg.warnings.append(
                    f"piece {idx}: connector booleans produced {len(bodies)} bodies; "
                    "keeping the largest"
                )
                bodies.sort(key=lambda b: b.volume, reverse=True)
            piece.mesh = bodies[0] if bodies else mesh
