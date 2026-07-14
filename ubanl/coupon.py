"""Tolerance calibration coupon: print it, find the clearance that fits snug,
write that number into your printer profile.

Produces two solids: a plate with one socket per candidate clearance (values
engraved beside each) and a single loose pin to try in them.
"""

from __future__ import annotations

import numpy as np
import trimesh

from .config import ConnectorConfig
from .connectors import pin_solid, socket_solid
from .geometry import boolean
from .labels import text_solid

DEFAULT_CLEARANCES = (0.10, 0.15, 0.20, 0.25, 0.30)


def coupon_plate(
    cfg: ConnectorConfig, clearances: tuple[float, ...] = DEFAULT_CLEARANCES
) -> trimesh.Trimesh:
    d = cfg.diameter_mm
    pitch = d + 14.0
    depth = cfg.length_mm + cfg.bottom_gap_mm + 0.5
    floor = 2.0
    width = pitch * len(clearances) + 4.0
    height = d + 18.0
    plate = trimesh.creation.box(
        extents=[width, height, depth + floor],
        transform=trimesh.transformations.translation_matrix(
            [width / 2.0, height / 2.0, (depth + floor) / 2.0]
        ),
    )
    top = depth + floor
    cavities: list[trimesh.Trimesh] = []
    for i, clearance in enumerate(clearances):
        cx = pitch * (i + 0.5) + 2.0
        cy = height - d / 2.0 - 4.0
        socket = socket_solid(cfg, clearance=clearance)
        # socket_solid opens at its local z=0 going +z; flip it to sink into the top
        socket.apply_transform(trimesh.geometry.align_vectors([0, 0, 1], [0, 0, -1]))
        socket.apply_translation([cx, cy, top])
        cavities.append(socket)
        # tag prism spans [0, 0.7] locally; sink it so 0.6 mm engraves the top
        tag = text_solid(f"{clearance:.2f}", height=5.0, depth=0.6 + 0.1)
        tag.apply_translation([cx, 6.0, top - 0.6])
        cavities.append(tag)
    return boolean("difference", [plate, boolean("union", cavities)])


def coupon_pin(cfg: ConnectorConfig) -> trimesh.Trimesh:
    """The test pin on a grip disc, printable flat: one revolved profile."""
    r = cfg.diameter_mm / 2.0
    ch = cfg.chamfer_mm
    grip_r, grip_h = r + 4.0, 3.0
    tip = grip_h + cfg.length_mm
    profile = np.array(
        [
            [0.0, 0.0],
            [grip_r, 0.0],
            [grip_r, grip_h],
            [r, grip_h],
            [r, tip - ch],
            [r - ch, tip],
            [0.0, tip],
        ]
    )
    return trimesh.creation.revolve(profile, sections=64)


def clearance_hint(clearances: tuple[float, ...] = DEFAULT_CLEARANCES) -> str:
    return (
        "Print coupon_plate and coupon_pin, then try the pin in each socket "
        f"({', '.join(f'{c:.2f}' for c in clearances)} mm). Use the smallest "
        "clearance the pin seats into fully by hand as printer.clearance_mm."
    )
