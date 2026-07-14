"""Outputs: piece labels & assembly order, manifest.json, exploded HTML preview."""

from __future__ import annotations

import colorsys
import json
import logging
from dataclasses import asdict
from pathlib import Path

import numpy as np
import trimesh

from .config import ProjectConfig
from .connectors import ConnectorReport
from .geometry import obb_fits
from .segmentation import Segmentation

log = logging.getLogger(__name__)

#: solid PLA density; real filament use depends on infill, treat as upper bound
PLA_G_PER_CM3 = 1.24


def assign_labels(seg: Segmentation) -> list[int]:
    """Stable bottom-up assembly order; labels P01, P02, ... follow it."""
    pieces = seg.piece_list()
    order = sorted(
        pieces,
        key=lambda p: (
            round(float(p.mesh.bounds[0][2]), 3),
            round(float(p.mesh.centroid[2]), 3),
            round(float(p.mesh.centroid[0]), 3),
            round(float(p.mesh.centroid[1]), 3),
        ),
    )
    pad = max(2, len(str(len(order))))
    for i, piece in enumerate(order):
        piece.label = f"P{i + 1:0{pad}d}"
    return [p.idx for p in order]


def piece_colors(count: int) -> list[list[int]]:
    """Evenly spread distinct colors (golden-angle hue walk)."""
    colors = []
    for i in range(count):
        hue = (i * 0.61803398875) % 1.0
        r, g, b = colorsys.hsv_to_rgb(hue, 0.55, 0.92)
        colors.append([int(r * 255), int(g * 255), int(b * 255), 255])
    return colors


def build_manifest(
    seg: Segmentation,
    report: ConnectorReport,
    cfg: ProjectConfig,
    order: list[int],
    scale_applied: float,
) -> dict:
    allowed = cfg.printer.allowed_dims
    pieces = []
    for idx in order:
        piece = seg.pieces[idx]
        mesh = piece.mesh
        volume_cm3 = float(mesh.volume) / 1000.0
        pieces.append(
            {
                "label": piece.label,
                "obb_extents_mm": np.sort(
                    mesh.bounding_box_oriented.primitive.extents
                ).round(2).tolist(),
                "fits_build_volume": obb_fits(mesh, allowed),
                "volume_cm3": round(volume_cm3, 2),
                "solid_pla_grams_max": round(volume_cm3 * PLA_G_PER_CM3, 1),
                "watertight": bool(mesh.is_watertight),
            }
        )
    label_of = {idx: seg.pieces[idx].label for idx in seg.pieces}
    connectors = [
        {
            "interface": c.interface_idx,
            "male": label_of[c.male_idx],
            "female": label_of[c.female_idx],
            "position_mm": np.asarray(c.point_world).round(2).tolist(),
            "axis": np.asarray(c.axis_world).round(4).tolist(),
        }
        for c in report.connectors
    ]
    interfaces = [
        {
            "id": iface.idx,
            "origin_mm": iface.origin.round(2).tolist(),
            "normal": iface.normal.round(4).tolist(),
            "area_mm2": round(iface.section.area, 1),
        }
        for iface in seg.interfaces
    ]
    return {
        "generator": "ubanl 0.1.0",
        "scale_applied": round(scale_applied, 6),
        "config": asdict(cfg),
        "pieces": pieces,
        "interfaces": interfaces,
        "connectors": connectors,
        "assembly_order": [label_of[idx] for idx in order],
        "assembly_note": "Glue bottom-up in assembly_order; pins self-align each joint.",
        "warnings": seg.warnings + report.warnings,
    }


def _scene(seg: Segmentation, order: list[int], explode: float) -> trimesh.Scene:
    center = np.mean([seg.pieces[i].mesh.centroid for i in order], axis=0)
    colors = piece_colors(len(order))
    scene = trimesh.Scene()
    for color, idx in zip(colors, order):
        piece = seg.pieces[idx]
        mesh = piece.mesh.copy()
        mesh.visual.face_colors = color
        mesh.apply_translation((mesh.centroid - center) * explode)
        scene.add_geometry(mesh, node_name=piece.label, geom_name=piece.label)
    return scene


def write_outputs(
    seg: Segmentation,
    report: ConnectorReport,
    cfg: ProjectConfig,
    order: list[int],
    scale_applied: float,
) -> list[Path]:
    out_dir = Path(cfg.output.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for idx in order:
        piece = seg.pieces[idx]
        for fmt in cfg.output.formats:
            path = out_dir / f"{piece.label}.{fmt}"
            piece.mesh.export(path)
            written.append(path)

    manifest = build_manifest(seg, report, cfg, order, scale_applied)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    written.append(manifest_path)

    if cfg.output.preview:
        assembled = _scene(seg, order, explode=0.0)
        glb_path = out_dir / "assembled.glb"
        assembled.export(glb_path)
        written.append(glb_path)
        try:
            from trimesh.viewer import notebook

            html = notebook.scene_to_html(_scene(seg, order, explode=0.35))
            html_path = out_dir / "exploded_preview.html"
            html_path.write_text(html)
            written.append(html_path)
        except BaseException as exc:  # preview must never sink the run
            log.warning("could not write HTML preview: %s", exc)
    return written
