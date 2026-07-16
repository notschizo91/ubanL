"""End-to-end pipeline: load -> repair -> scale -> segment -> connect -> label -> export."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import trimesh

from .config import ProjectConfig
from .connectors import ConnectorReport, apply_connectors, place_connectors
from .labels import engrave_labels
from .repair import ensure_solid
from .report import assign_labels, write_outputs
from .segmentation import Segmentation, segment

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    segmentation: Segmentation
    connectors: ConnectorReport
    scale_applied: float
    files: list[Path] = field(default_factory=list)

    @property
    def warnings(self) -> list[str]:
        return self.segmentation.warnings + self.connectors.warnings


def load_mesh(path: str | Path) -> trimesh.Trimesh:
    loaded = trimesh.load(str(path), force="mesh")
    if not isinstance(loaded, trimesh.Trimesh) or loaded.is_empty:
        raise ValueError(f"{path}: no triangle geometry found")
    return loaded


def scale_to_target(mesh: trimesh.Trimesh, cfg: ProjectConfig) -> float:
    if cfg.target_height_mm is not None:
        current = float(mesh.extents[2])
        if current <= 0:
            raise ValueError("mesh has zero height; cannot scale to target height")
        factor = cfg.target_height_mm / current
    elif cfg.scale_factor is not None:
        factor = float(cfg.scale_factor)
    else:
        factor = 1.0
    if factor != 1.0:
        mesh.apply_scale(factor)
    return factor


def run(cfg: ProjectConfig, on_stage=None) -> RunResult:
    """Run the pipeline; `on_stage(name)` is called as each stage starts."""

    def stage(name: str) -> None:
        if on_stage is not None:
            on_stage(name)

    stage("loading")
    log.info("loading %s", cfg.input)
    mesh = load_mesh(cfg.input)
    stage("repairing")
    mesh = ensure_solid(mesh, mode=cfg.repair, voxel_pitch_fraction=cfg.voxel_pitch_fraction)
    scale_applied = scale_to_target(mesh, cfg)
    log.info(
        "model: %.0f x %.0f x %.0f mm, %.0f cm3 (scale %.3f)",
        *mesh.extents, mesh.volume / 1000.0, scale_applied,
    )

    stage("segmenting")
    seg = segment(mesh, cfg.planner, cfg.printer, cfg.connectors, seed=cfg.seed)
    log.info("segmented into %d piece(s), %d interface(s)", len(seg.pieces), len(seg.interfaces))

    volume_error = abs(sum(p.mesh.volume for p in seg.piece_list()) - mesh.volume) / mesh.volume
    if volume_error > 1e-3:
        seg.warnings.append(f"volume drift after cutting: {volume_error:.2%}")

    stage("placing connectors")
    connectors = place_connectors(seg, cfg.connectors, cfg.printer, seed=cfg.seed)
    apply_connectors(seg, connectors, cfg.connectors, cfg.printer)
    log.info("placed %d connector(s)", len(connectors.connectors))

    stage("engraving labels")
    order = assign_labels(seg)
    label_warnings = engrave_labels(seg, connectors, cfg.labels)
    seg.warnings.extend(label_warnings)

    stage("exporting")
    files = write_outputs(seg, connectors, cfg, order, scale_applied)
    for warning in seg.warnings + connectors.warnings:
        log.warning("%s", warning)
    log.info("wrote %d file(s) to %s", len(files), cfg.output.dir)
    return RunResult(seg, connectors, scale_applied, files)
