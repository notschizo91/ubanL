"""Repair ladder: cheapest fix first, voxel remesh as the last resort.

Everything downstream (booleans, volume math, connectors) assumes a watertight
manifold solid; this module either delivers one or raises with a diagnostic.
"""

from __future__ import annotations

import logging

import numpy as np
import trimesh

log = logging.getLogger(__name__)


class RepairError(RuntimeError):
    pass


def _is_solid(mesh: trimesh.Trimesh) -> bool:
    return bool(mesh.is_watertight and mesh.is_volume)


def basic_repair(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """trimesh built-ins: merge, drop degenerates, fix winding, fill holes."""
    mesh = mesh.copy()
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    trimesh.repair.fix_normals(mesh)
    if not mesh.is_watertight:
        trimesh.repair.fill_holes(mesh)
    if mesh.is_volume and mesh.volume < 0:
        mesh.invert()
    return mesh


def voxel_remesh(mesh: trimesh.Trimesh, pitch: float) -> trimesh.Trimesh:
    """Rebuild a guaranteed shell by voxelizing + marching cubes (loses detail)."""
    voxels = mesh.voxelized(pitch=pitch).fill()
    rebuilt = voxels.marching_cubes
    rebuilt = basic_repair(rebuilt)
    return rebuilt


def ensure_solid(
    mesh: trimesh.Trimesh, mode: str = "auto", voxel_pitch_fraction: float = 1.0 / 200.0
) -> trimesh.Trimesh:
    """Return a watertight manifold version of the mesh, or raise RepairError.

    mode: "none" (validate only), "basic" (no voxel fallback), "auto" (full ladder).
    """
    if mesh.is_empty or len(mesh.faces) == 0:
        raise RepairError("mesh is empty")
    if _is_solid(mesh):
        return mesh
    if mode == "none":
        raise RepairError(
            "mesh is not a watertight solid (watertight="
            f"{mesh.is_watertight}, volume={mesh.is_volume}) and repair is disabled"
        )

    repaired = basic_repair(mesh)
    if _is_solid(repaired):
        log.info("repaired mesh with basic filters")
        return repaired
    if mode == "basic":
        raise RepairError("basic repair failed to produce a watertight solid")

    pitch = float(np.linalg.norm(mesh.extents)) * voxel_pitch_fraction
    log.warning(
        "basic repair failed; voxel-remeshing at pitch %.3f mm (detail below this is lost)",
        pitch,
    )
    rebuilt = voxel_remesh(mesh, pitch)
    if _is_solid(rebuilt):
        return rebuilt
    raise RepairError("voxel remesh fallback also failed; the input mesh is unusable")
