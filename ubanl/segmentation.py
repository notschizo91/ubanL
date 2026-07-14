"""Segmentation: drive cutting planes until every piece fits the build volume.

Three planners share one cut executor:

- manual: planes straight from the config, applied in order to everything they cross
- grid:   axis-aligned planes, guaranteed feasible, the baseline auto must beat
- auto:   greedy BSP — repeatedly split the worst oversized piece with the best
          candidate plane (feasible connectors, fewest projected pieces, smallest
          cross-section, best balance)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import trimesh

from .config import ConnectorConfig, PlannerConfig, PrinterConfig
from .geometry import (
    CutError,
    SectionFrame,
    side_fit_stats,
    cut_with_plane,
    obb_fits,
    placeable_region,
    section_at_plane,
)

log = logging.getLogger(__name__)

WORLD_AXES = np.eye(3)


@dataclass
class Piece:
    idx: int
    mesh: trimesh.Trimesh
    label: str = ""


@dataclass
class Interface:
    """One applied cut on one piece: the mating surface between its children."""

    idx: int
    origin: np.ndarray
    normal: np.ndarray
    section: SectionFrame


@dataclass
class Segmentation:
    pieces: dict[int, Piece]
    interfaces: list[Interface]
    warnings: list[str] = field(default_factory=list)

    def piece_list(self) -> list[Piece]:
        return list(self.pieces.values())


class _State:
    def __init__(self, mesh: trimesh.Trimesh):
        self.pieces: dict[int, Piece] = {}
        self.interfaces: list[Interface] = []
        self.warnings: list[str] = []
        self._next = 0
        self.add(mesh)

    def add(self, mesh: trimesh.Trimesh) -> Piece:
        piece = Piece(idx=self._next, mesh=mesh)
        self.pieces[self._next] = piece
        self._next += 1
        return piece

    def apply_cut(self, piece: Piece, origin: np.ndarray, normal: np.ndarray) -> bool:
        """Split one piece at a plane; returns False if the plane misses it."""
        section = section_at_plane(piece.mesh, origin, normal)
        if section is None:
            return False
        neg, pos = cut_with_plane(piece.mesh, origin, normal)
        if not neg or not pos:
            return False
        del self.pieces[piece.idx]
        for child in (*neg, *pos):
            self.add(child)
        self.interfaces.append(
            Interface(
                idx=len(self.interfaces),
                origin=np.asarray(origin, dtype=float),
                normal=np.asarray(normal, dtype=float) / np.linalg.norm(normal),
                section=section,
            )
        )
        return True

    def result(self) -> Segmentation:
        return Segmentation(self.pieces, self.interfaces, self.warnings)


def _connector_inset(connectors: ConnectorConfig) -> float:
    return connectors.diameter_mm / 2.0 + connectors.wall_margin_mm


def planning_dims(printer: PrinterConfig, connectors: ConnectorConfig) -> np.ndarray:
    """Allowed piece dims during planning: pins protrude past the cut face,
    so reserve their length on every axis."""
    reserve = connectors.length_mm if connectors.enabled else 0.0
    dims = printer.allowed_dims - reserve
    if (dims <= 0).any():
        raise CutError("connector length leaves no usable build volume")
    return dims


def segment(
    mesh: trimesh.Trimesh,
    planner: PlannerConfig,
    printer: PrinterConfig,
    connectors: ConnectorConfig,
    seed: int = 0,
) -> Segmentation:
    allowed = planning_dims(printer, connectors)
    if planner.mode == "manual":
        return _segment_manual(mesh, planner)
    if planner.mode == "grid":
        return _segment_grid(mesh, planner, allowed)

    auto_seg = _segment_auto(mesh, planner, allowed, connectors)
    # the greedy search must never lose to the dumb baseline: when it exceeds
    # the analytic floor, run the grid plan too and keep the better result
    floor = int(
        np.prod(
            np.ceil(
                np.sort(mesh.bounding_box_oriented.primitive.extents) / allowed
            ).clip(min=1)
        )
    )
    if len(auto_seg.pieces) > floor:
        grid_seg = _segment_grid(mesh, planner, allowed)
        if len(grid_seg.pieces) < len(auto_seg.pieces):
            grid_seg.warnings.append(
                f"auto plan ({len(auto_seg.pieces)} pieces) lost to the grid "
                f"baseline ({len(grid_seg.pieces)}); using the grid plan"
            )
            return grid_seg
    return auto_seg


# --------------------------------------------------------------------------- manual


def _segment_manual(mesh: trimesh.Trimesh, planner: PlannerConfig) -> Segmentation:
    state = _State(mesh)
    for spec in planner.planes:
        origin = np.asarray(spec.origin, dtype=float)
        normal = np.asarray(spec.normal, dtype=float)
        hit = False
        for piece in list(state.pieces.values()):
            hit |= state.apply_cut(piece, origin, normal)
        if not hit:
            state.warnings.append(f"manual plane {spec} does not intersect the model")
    return state.result()


# ----------------------------------------------------------------------------- grid


def _grid_planes(
    mesh: trimesh.Trimesh, allowed_sorted: np.ndarray
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Axis-aligned planes; the largest model axis gets the largest build dim."""
    extents = mesh.extents
    order = np.argsort(extents)  # model axes, ascending extent
    allowed = np.empty(3)
    allowed[order] = allowed_sorted  # match rank to rank
    planes = []
    for axis in range(3):
        n_div = int(np.ceil(extents[axis] / allowed[axis]))
        lo = mesh.bounds[0][axis]
        for k in range(1, n_div):
            origin = mesh.bounds.mean(axis=0).copy()
            origin[axis] = lo + extents[axis] * k / n_div
            planes.append((origin, WORLD_AXES[axis].copy()))
    return planes


def _segment_grid(
    mesh: trimesh.Trimesh, planner: PlannerConfig, allowed: np.ndarray
) -> Segmentation:
    state = _State(mesh)
    for origin, normal in _grid_planes(mesh, allowed):
        for piece in list(state.pieces.values()):
            state.apply_cut(piece, origin, normal)
        if len(state.pieces) > planner.max_pieces:
            raise CutError(f"grid plan exceeds max_pieces={planner.max_pieces}")
    return state.result()


# ----------------------------------------------------------------------------- auto


def _proxy(mesh: trimesh.Trimesh, target_faces: int) -> trimesh.Trimesh:
    """Decimated stand-in for candidate scoring; falls back to the full mesh."""
    if target_faces <= 0 or len(mesh.faces) <= target_faces:
        return mesh
    try:
        return mesh.simplify_quadric_decimation(face_count=target_faces)
    except BaseException as exc:
        log.debug("decimation unavailable (%s); scoring on the full mesh", exc)
        return mesh


def _candidate_normals(mesh: trimesh.Trimesh) -> list[np.ndarray]:
    normals = [WORLD_AXES[i].copy() for i in range(3)]
    obb_transform = mesh.bounding_box_oriented.primitive.transform
    for i in range(3):
        axis = obb_transform[:3, i]
        if all(abs(float(np.dot(axis, n))) < 0.99 for n in normals):
            normals.append(axis / np.linalg.norm(axis))
    return normals


def _score_candidate(
    proxy: trimesh.Trimesh,
    origin: np.ndarray,
    normal: np.ndarray,
    allowed: np.ndarray,
    inset: float,
    need_connectors: bool,
) -> tuple | None:
    """Lexicographic score (lower is better), or None if the cut is unusable."""
    section = section_at_plane(proxy, origin, normal)
    if section is None:
        return None
    if need_connectors and placeable_region(section.polygons, inset).is_empty:
        return None
    signed = (proxy.vertices - origin) @ normal
    neg_pts = proxy.vertices[signed < 0]
    pos_pts = proxy.vertices[signed >= 0]
    if len(neg_pts) < 4 or len(pos_pts) < 4:
        return None
    # reject near-tangent "shaving" cuts: the piece-count estimate is myopic
    # and would otherwise happily peel thin wedges off curved surfaces forever
    span_lo, span_hi = float(signed.min()), float(signed.max())
    span = span_hi - span_lo
    min_side = min(-span_lo, span_hi)
    if min_side < min(0.25 * span, 0.5 * allowed[-1]):
        return None
    rng = np.random.default_rng(0)
    count_neg, fits_neg = side_fit_stats(neg_pts, allowed, rng)
    count_pos, fits_pos = side_fit_stats(pos_pts, allowed, rng)
    # spatial balance: prefer cutting near the middle of the span (vertex
    # counts are useless here — boolean cut caps are very dense)
    imbalance = abs(span_hi + span_lo) / max(span, 1e-9)
    return (count_neg + count_pos, -(fits_neg + fits_pos), section.area, imbalance)


def _best_cut(
    piece: Piece,
    planner: PlannerConfig,
    allowed: np.ndarray,
    connectors: ConnectorConfig,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Candidate planes for one oversized piece, best first."""
    proxy = _proxy(piece.mesh, planner.proxy_faces)
    inset = _connector_inset(connectors)
    scored: list[tuple[tuple, np.ndarray, np.ndarray]] = []
    for normal in _candidate_normals(proxy):
        projections = proxy.vertices @ normal
        lo, hi = projections.min(), projections.max()
        # spatially even offsets (never vertex quantiles: cut caps are dense and
        # would drag every candidate next to an existing cut face) ...
        fractions = np.linspace(0.15, 0.85, planner.offsets_per_axis)
        offsets = list(lo + (hi - lo) * fractions)
        # ... plus grid-style offsets at build-volume multiples from either end,
        # so auto can always reproduce a maximal-slab (grid) solution
        a_max = allowed[-1]
        k = 1
        while lo + k * a_max < hi:
            offsets.extend([lo + k * a_max, hi - k * a_max])
            k += 1
        # never cut within a few mm of either face: grazing cuts make slivers
        end_margin = max(2.0, 0.02 * (hi - lo))
        usable = [o for o in offsets if lo + end_margin < o < hi - end_margin]
        for offset in np.unique(np.round(usable, 6)):
            origin = normal * offset
            score = _score_candidate(
                proxy, origin, normal, allowed, inset, connectors.enabled
            )
            if score is not None:
                scored.append((score, origin, normal))
    scored.sort(key=lambda item: item[0])
    candidates = [(origin, normal) for _, origin, normal in scored]
    # last resort: mid-plane of the longest axis, connector feasibility ignored
    longest = int(np.argmax(piece.mesh.extents))
    fallback_origin = piece.mesh.bounds.mean(axis=0)
    candidates.append((fallback_origin, WORLD_AXES[longest].copy()))
    return candidates


def _segment_auto(
    mesh: trimesh.Trimesh,
    planner: PlannerConfig,
    allowed: np.ndarray,
    connectors: ConnectorConfig,
) -> Segmentation:
    state = _State(mesh)
    while True:
        oversized = [p for p in state.pieces.values() if not obb_fits(p.mesh, allowed)]
        if not oversized:
            break
        if len(state.pieces) >= planner.max_pieces:
            raise CutError(
                f"auto planner hit max_pieces={planner.max_pieces} with oversized "
                "pieces remaining; check build volume and units"
            )
        piece = max(oversized, key=lambda p: p.mesh.volume)
        for origin, normal in _best_cut(piece, planner, allowed, connectors):
            if state.apply_cut(piece, origin, normal):
                break
        else:
            raise CutError(
                f"no candidate plane could split oversized piece {piece.idx}; "
                "the model may be thinner than the connector margin everywhere"
            )
    return state.result()
