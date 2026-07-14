"""Low-level geometry: robust booleans, planar cuts, section polygons, fit checks.

All booleans go through Manifold (guaranteed-manifold output). Every function
here is mesh-in/mesh-out and touches no files.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import shapely
import trimesh
from shapely.geometry import Polygon

BOOLEAN_ENGINE = "manifold"


class CutError(RuntimeError):
    pass


def boolean(op: str, meshes: list[trimesh.Trimesh]) -> trimesh.Trimesh:
    """Run a boolean op ('union' | 'difference' | 'intersection') via Manifold."""
    fn = getattr(trimesh.boolean, op)
    result = fn(meshes, engine=BOOLEAN_ENGINE)
    if not isinstance(result, trimesh.Trimesh) or result.is_empty:
        return trimesh.Trimesh()
    return result


def halfspace_box(origin: np.ndarray, normal: np.ndarray, size: float) -> trimesh.Trimesh:
    """A cube whose top face lies exactly on the plane, filling the -normal side."""
    normal = np.asarray(normal, dtype=float)
    normal = normal / np.linalg.norm(normal)
    rot = trimesh.geometry.align_vectors([0, 0, 1], normal)
    transform = trimesh.transformations.translation_matrix(origin - normal * size / 2.0) @ rot
    return trimesh.creation.box(extents=[size, size, size], transform=transform)


@dataclass
class SectionFrame:
    """Cross-section of a mesh at a plane, as 2D polygons plus the 2D->3D map."""

    polygons: list[Polygon]
    to_3d: np.ndarray  # 4x4, maps (x, y, 0, 1) in section frame to world
    normal: np.ndarray  # the *cut* normal in world coordinates (unit)

    @property
    def frame_flipped(self) -> bool:
        """True when the section frame's z-axis points against the cut normal."""
        return float(np.dot(self.to_3d[:3, 2], self.normal)) < 0.0

    def point_to_world(self, xy: np.ndarray) -> np.ndarray:
        p = self.to_3d @ np.array([xy[0], xy[1], 0.0, 1.0])
        return p[:3]

    @property
    def area(self) -> float:
        return float(sum(p.area for p in self.polygons))


def section_at_plane(
    mesh: trimesh.Trimesh, origin: np.ndarray, normal: np.ndarray
) -> SectionFrame | None:
    """Planar cross-section polygons (with holes) of a mesh, or None if disjoint."""
    normal = np.asarray(normal, dtype=float)
    normal = normal / np.linalg.norm(normal)
    path3d = mesh.section(plane_origin=origin, plane_normal=normal)
    if path3d is None or len(path3d.entities) == 0:
        return None
    try:
        planar, to_3d = path3d.to_2D()
        polygons = [p for p in planar.polygons_full if p is not None and p.area > 1e-9]
    except BaseException:
        # unclosed/degenerate section curves (grazing cuts): treat as no section
        return None
    if not polygons:
        return None
    return SectionFrame(polygons=polygons, to_3d=np.asarray(to_3d), normal=normal)


def split_solid(mesh: trimesh.Trimesh, min_volume: float = 1e-6) -> list[trimesh.Trimesh]:
    """Connected watertight components above a volume floor."""
    if mesh.is_empty:
        return []
    parts = mesh.split(only_watertight=True)
    if len(parts) == 0:
        parts = [mesh]
    return [p for p in parts if p.volume > min_volume]


def cut_with_plane(
    mesh: trimesh.Trimesh, origin: np.ndarray, normal: np.ndarray
) -> tuple[list[trimesh.Trimesh], list[trimesh.Trimesh]]:
    """Split a solid at a plane into (negative side, positive side) components.

    Sides are relative to `normal`; either list may be empty if the plane
    misses the mesh.
    """
    origin = np.asarray(origin, dtype=float)
    normal = np.asarray(normal, dtype=float)
    normal = normal / np.linalg.norm(normal)
    size = 4.0 * float(np.linalg.norm(mesh.extents)) + 1.0
    # recenter the box on the mesh (projected onto the plane) — a plane origin
    # far from the mesh laterally would leave parts of it outside both boxes
    center = mesh.bounds.mean(axis=0)
    origin = center - normal * float(np.dot(center - origin, normal))
    below_box = halfspace_box(origin, normal, size)
    above_box = halfspace_box(origin, -normal, size)
    below = boolean("intersection", [mesh, below_box])
    above = boolean("intersection", [mesh, above_box])
    floor = max(mesh.volume * 1e-9, 1e-9)
    return split_solid(below, floor), split_solid(above, floor)


def obb_fits(mesh: trimesh.Trimesh, allowed_dims_sorted: np.ndarray) -> bool:
    """Can the mesh be rotated to fit inside the allowed box?

    Checks the axis-aligned bounds and the minimal-volume OBB and accepts
    either: the minimal-volume OBB alone can have a *longer* max extent than
    an orientation that fits (sorted-extent comparison is exact for a given
    box orientation, axis-to-axis).
    """
    for extents in (mesh.extents, mesh.bounding_box_oriented.primitive.extents):
        if (np.sort(extents) <= allowed_dims_sorted + 1e-9).all():
            return True
    return False


def side_fit_stats(
    points: np.ndarray,
    allowed_dims_sorted: np.ndarray,
    rng: np.random.Generator | None = None,
    max_points: int = 4000,
) -> tuple[int, bool]:
    """(estimated piece count, fits already?) for one side of a candidate cut.

    Uses the minimal oriented bounding box of the point set — a world-frame
    AABB would misjudge every rotated piece.
    """
    if len(points) > max_points and rng is not None:
        points = points[rng.choice(len(points), max_points, replace=False)]
    try:
        _, extents = trimesh.bounds.oriented_bounds(points)
    except BaseException:
        extents = points.max(axis=0) - points.min(axis=0)
    extents = np.sort(np.asarray(extents))
    count = int(np.prod(np.ceil(extents / allowed_dims_sorted).clip(min=1)))
    fits = bool((extents <= allowed_dims_sorted + 1e-9).all())
    return count, fits


def placeable_region(polygons: list[Polygon], inset: float) -> shapely.geometry.base.BaseGeometry:
    """Where a connector center may go: the section inset by radius + margin."""
    region = shapely.unary_union([p.buffer(-inset) for p in polygons])
    return region


def farthest_points_in_region(
    region: shapely.geometry.base.BaseGeometry,
    count: int,
    grid_step: float,
    rng: np.random.Generator,
) -> list[np.ndarray]:
    """Pick up to `count` well-spread points inside a shapely region.

    Greedy farthest-point sampling over a jittered grid of interior candidates;
    the first point is the one deepest inside the region.
    """
    if region.is_empty or count <= 0:
        return []
    minx, miny, maxx, maxy = region.bounds
    xs = np.arange(minx, maxx + grid_step, grid_step)
    ys = np.arange(miny, maxy + grid_step, grid_step)
    if len(xs) == 0 or len(ys) == 0 or len(xs) * len(ys) > 250_000:
        # degenerate or absurdly fine grid: fall back to representative point
        p = region.representative_point()
        return [np.array([p.x, p.y])]
    grid = np.stack(np.meshgrid(xs, ys), axis=-1).reshape(-1, 2)
    grid = grid + rng.uniform(-0.25, 0.25, size=grid.shape) * grid_step
    inside = shapely.contains_xy(region, grid[:, 0], grid[:, 1])
    candidates = grid[inside]
    if len(candidates) == 0:
        p = region.representative_point()
        return [np.array([p.x, p.y])]

    boundary = region.boundary
    depth = shapely.distance(boundary, shapely.points(candidates))
    chosen = [candidates[int(np.argmax(depth))]]
    while len(chosen) < count:
        dist_to_chosen = np.min(
            np.linalg.norm(candidates[:, None, :] - np.asarray(chosen)[None, :, :], axis=-1),
            axis=1,
        )
        # spread out, but stay meaningfully inside the region
        score = dist_to_chosen + 0.5 * depth
        best = int(np.argmax(score))
        if dist_to_chosen[best] < grid_step:
            break  # region is saturated
        chosen.append(candidates[best])
    return [np.asarray(c) for c in chosen]
