"""Project configuration: dataclasses mirroring the YAML schema.

A project is fully described by a config; the CLI is a thin runner over it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml


class ConfigError(ValueError):
    pass


@dataclass
class PrinterConfig:
    #: usable build volume in mm (x, y, z)
    build_volume_mm: tuple[float, float, float] = (220.0, 220.0, 250.0)
    #: keep pieces this far under the build volume on every axis
    margin_mm: float = 5.0
    #: radial clearance between pin and socket; printer/material specific,
    #: calibrate with `ubanl coupon`
    clearance_mm: float = 0.20

    @property
    def allowed_dims(self) -> np.ndarray:
        """Sorted (ascending) max piece dimensions after margin."""
        dims = np.asarray(self.build_volume_mm, dtype=float) - 2.0 * self.margin_mm
        if (dims <= 0).any():
            raise ConfigError("margin_mm leaves no usable build volume")
        return np.sort(dims)


@dataclass
class PlaneSpec:
    origin: tuple[float, float, float]
    normal: tuple[float, float, float]

    @classmethod
    def from_any(cls, raw: dict[str, Any]) -> "PlaneSpec":
        """Accept either {origin, normal} or the {axis, position} shorthand."""
        if "axis" in raw:
            axis = str(raw["axis"]).lower()
            if axis not in "xyz" or len(axis) != 1:
                raise ConfigError(f"plane axis must be x, y or z, got {axis!r}")
            i = "xyz".index(axis)
            origin = [0.0, 0.0, 0.0]
            origin[i] = float(raw["position"])
            normal = [0.0, 0.0, 0.0]
            normal[i] = 1.0
            return cls(origin=tuple(origin), normal=tuple(normal))
        try:
            origin = tuple(float(v) for v in raw["origin"])
            normal = tuple(float(v) for v in raw["normal"])
        except (KeyError, TypeError) as exc:
            raise ConfigError(f"bad plane spec {raw!r}: {exc}") from exc
        if len(origin) != 3 or len(normal) != 3:
            raise ConfigError(f"plane origin/normal must have 3 components: {raw!r}")
        if np.linalg.norm(normal) < 1e-12:
            raise ConfigError(f"plane normal may not be zero: {raw!r}")
        return cls(origin=origin, normal=normal)


@dataclass
class PlannerConfig:
    #: "auto" (greedy BSP), "grid" (axis-aligned baseline), or "manual"
    mode: str = "auto"
    #: manual mode only: the cutting planes, applied in order
    planes: list[PlaneSpec] = field(default_factory=list)
    #: safety cap on total piece count
    max_pieces: int = 200
    #: auto mode: candidate offsets swept per cut direction
    offsets_per_axis: int = 7
    #: decimate planning proxy to roughly this many faces (0 disables)
    proxy_faces: int = 60_000

    def __post_init__(self) -> None:
        if self.mode not in ("auto", "grid", "manual"):
            raise ConfigError(f"planner mode must be auto|grid|manual, got {self.mode!r}")
        if self.mode == "manual" and not self.planes:
            raise ConfigError("manual planner mode requires at least one plane")


@dataclass
class ConnectorConfig:
    enabled: bool = True
    #: only "dowel" (chamfered cylindrical pin + socket) in v1
    kind: str = "dowel"
    diameter_mm: float = 8.0
    #: how far the pin protrudes past the cut face
    length_mm: float = 8.0
    #: pins per interface, area permitting (a single pin cannot index rotation)
    min_count: int = 2
    max_count: int = 4
    #: chamfer on the pin tip and socket opening for easier starts
    chamfer_mm: float = 1.0
    #: extra socket depth left empty as a glue pocket
    bottom_gap_mm: float = 0.5
    #: keep pins at least this far from the piece wall (defaults to 2 x diameter/4)
    wall_margin_mm: float = 4.0

    def __post_init__(self) -> None:
        if self.kind != "dowel":
            raise ConfigError(f"connector kind {self.kind!r} not implemented (v1: dowel)")
        if self.diameter_mm <= 0 or self.length_mm <= 0:
            raise ConfigError("connector diameter and length must be positive")
        if self.chamfer_mm >= self.diameter_mm / 2:
            raise ConfigError("chamfer_mm must be smaller than the pin radius")
        if not (1 <= self.min_count <= self.max_count):
            raise ConfigError("need 1 <= min_count <= max_count")


@dataclass
class LabelConfig:
    enabled: bool = True
    height_mm: float = 8.0
    depth_mm: float = 0.8
    #: skip engraving rather than shrink text below this
    min_height_mm: float = 4.0


@dataclass
class OutputConfig:
    dir: str = "out"
    formats: list[str] = field(default_factory=lambda: ["stl"])
    #: write exploded-view preview HTML + assembled GLB
    preview: bool = True

    def __post_init__(self) -> None:
        for fmt in self.formats:
            if fmt not in ("stl", "3mf", "obj", "glb"):
                raise ConfigError(f"unsupported output format {fmt!r}")


@dataclass
class ProjectConfig:
    input: str = ""
    #: exactly one of target_height_mm / scale_factor (or neither = no scaling)
    target_height_mm: float | None = None
    scale_factor: float | None = None
    #: "auto" (repair ladder incl. voxel fallback), "basic", or "none"
    repair: str = "auto"
    #: voxel remesh resolution as a fraction of the bounding-box diagonal
    voxel_pitch_fraction: float = 1.0 / 200.0
    seed: int = 0
    printer: PrinterConfig = field(default_factory=PrinterConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    connectors: ConnectorConfig = field(default_factory=ConnectorConfig)
    labels: LabelConfig = field(default_factory=LabelConfig)
    output: OutputConfig = field(default_factory=OutputConfig)

    def __post_init__(self) -> None:
        if self.target_height_mm is not None and self.scale_factor is not None:
            raise ConfigError("set target_height_mm or scale_factor, not both")
        if self.repair not in ("auto", "basic", "none"):
            raise ConfigError(f"repair must be auto|basic|none, got {self.repair!r}")


def _build(cls, raw: dict[str, Any]):
    """Construct a config dataclass from a raw dict, rejecting unknown keys."""
    fields = {f for f in cls.__dataclass_fields__}
    unknown = set(raw) - fields
    if unknown:
        raise ConfigError(f"unknown key(s) in {cls.__name__}: {sorted(unknown)}")
    return cls(**raw)


def config_from_dict(raw: dict[str, Any]) -> ProjectConfig:
    raw = dict(raw)
    sections = {
        "printer": PrinterConfig,
        "planner": PlannerConfig,
        "connectors": ConnectorConfig,
        "labels": LabelConfig,
        "output": OutputConfig,
    }
    kwargs: dict[str, Any] = {}
    for key, cls in sections.items():
        if key in raw:
            sub = dict(raw.pop(key))
            if key == "printer" and "build_volume_mm" in sub:
                sub["build_volume_mm"] = tuple(float(v) for v in sub["build_volume_mm"])
            if key == "planner" and "planes" in sub:
                sub["planes"] = [PlaneSpec.from_any(p) for p in sub["planes"]]
            kwargs[key] = _build(cls, sub)
    kwargs.update(raw)
    return _build(ProjectConfig, kwargs)


def load_config(path: str | Path) -> ProjectConfig:
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level of the config must be a mapping")
    cfg = config_from_dict(raw)
    if not cfg.input:
        raise ConfigError(f"{path}: 'input' (mesh path) is required")
    # input path is relative to the config file's directory
    cfg.input = str((Path(path).parent / cfg.input).resolve())
    return cfg
