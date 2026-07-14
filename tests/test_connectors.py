import numpy as np
import trimesh

from ubanl.config import ConnectorConfig, PlaneSpec, PlannerConfig, PrinterConfig
from ubanl.connectors import apply_connectors, pin_solid, place_connectors, socket_solid
from ubanl.segmentation import segment

PRINTER = PrinterConfig(build_volume_mm=(220.0, 220.0, 250.0), clearance_mm=0.2)


def _two_piece(sphere_100):
    planner = PlannerConfig(
        mode="manual", planes=[PlaneSpec.from_any({"axis": "z", "position": 50.0})]
    )
    return segment(sphere_100, planner, PRINTER, ConnectorConfig())


def test_pin_and_socket_solids():
    cfg = ConnectorConfig()
    pin = pin_solid(cfg)
    socket = socket_solid(cfg, clearance=0.2)
    assert pin.is_watertight and pin.is_volume
    assert socket.is_watertight and socket.is_volume
    # pin protrudes exactly length_mm above z=0
    assert abs(pin.bounds[1][2] - cfg.length_mm) < 1e-6
    # socket is deeper than the pin protrudes and wider than the pin
    assert socket.bounds[1][2] > cfg.length_mm
    pin_r = pin.bounds[1][0]
    deep = socket.vertices[:, 2] > cfg.chamfer_mm + 1e-6  # below the opening chamfer
    socket_wall_r = np.linalg.norm(socket.vertices[deep][:, :2], axis=1).max()
    assert socket_wall_r >= pin_r + 0.2 - 1e-6


def test_place_and_apply(sphere_100):
    seg = _two_piece(sphere_100)
    cfg = ConnectorConfig(min_count=2, max_count=4)
    report = place_connectors(seg, cfg, PRINTER)
    assert len(report.connectors) >= 2
    males = {c.male_idx for c in report.connectors}
    females = {c.female_idx for c in report.connectors}
    assert len(males) == 1 and len(females) == 1 and males != females

    vol_before = {i: p.mesh.volume for i, p in seg.pieces.items()}
    apply_connectors(seg, report, cfg, PRINTER)
    male_idx, female_idx = next(iter(males)), next(iter(females))
    assert seg.pieces[male_idx].mesh.volume > vol_before[male_idx]  # pins added
    assert seg.pieces[female_idx].mesh.volume < vol_before[female_idx]  # sockets carved
    for piece in seg.piece_list():
        assert piece.mesh.is_watertight and piece.mesh.is_volume


def test_assembled_clearance(sphere_100):
    """Male piece must not intersect the female piece when assembled in place."""
    seg = _two_piece(sphere_100)
    cfg = ConnectorConfig()
    report = place_connectors(seg, cfg, PRINTER)
    apply_connectors(seg, report, cfg, PRINTER)
    male = seg.pieces[report.connectors[0].male_idx].mesh
    female = seg.pieces[report.connectors[0].female_idx].mesh
    overlap = trimesh.boolean.intersection([male, female], engine="manifold")
    overlap_volume = 0.0 if overlap.is_empty else overlap.volume
    assert overlap_volume < 1.0  # mm^3; exact zero modulo tessellation dust


def test_disabled_connectors(sphere_100):
    seg = _two_piece(sphere_100)
    report = place_connectors(seg, ConnectorConfig(enabled=False), PRINTER)
    assert report.connectors == []
