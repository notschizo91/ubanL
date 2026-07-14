import numpy as np
import pytest

from ubanl.config import ConnectorConfig, PlaneSpec, PlannerConfig, PrinterConfig
from ubanl.geometry import obb_fits
from ubanl.segmentation import segment

SMALL_PRINTER = PrinterConfig(build_volume_mm=(120.0, 120.0, 120.0), margin_mm=5.0)


def _check_all_fit(seg, printer):
    allowed = printer.allowed_dims
    for piece in seg.piece_list():
        assert piece.mesh.is_watertight and piece.mesh.is_volume
        assert obb_fits(piece.mesh, allowed), (
            f"piece {piece.idx} extents "
            f"{np.sort(piece.mesh.bounding_box_oriented.primitive.extents)}"
        )


def _check_volume(seg, mesh):
    total = sum(p.mesh.volume for p in seg.piece_list())
    assert abs(total - mesh.volume) / mesh.volume < 1e-3


@pytest.mark.parametrize("mode", ["grid", "auto"])
def test_planners_produce_fitting_pieces(capsule_tall, mode):
    planner = PlannerConfig(mode=mode)
    seg = segment(capsule_tall, planner, SMALL_PRINTER, ConnectorConfig())
    assert len(seg.pieces) >= 4  # ~400 mm tall vs 110 mm allowed
    _check_all_fit(seg, SMALL_PRINTER)
    _check_volume(seg, capsule_tall)
    assert len(seg.interfaces) >= len(seg.pieces) - 1


def test_auto_not_worse_than_grid(capsule_tall):
    grid = segment(capsule_tall, PlannerConfig(mode="grid"), SMALL_PRINTER, ConnectorConfig())
    auto = segment(capsule_tall, PlannerConfig(mode="auto"), SMALL_PRINTER, ConnectorConfig())
    assert len(auto.pieces) <= len(grid.pieces)


def test_manual_plane(sphere_100):
    planner = PlannerConfig(
        mode="manual", planes=[PlaneSpec.from_any({"axis": "z", "position": 50.0})]
    )
    seg = segment(sphere_100, planner, SMALL_PRINTER, ConnectorConfig())
    assert len(seg.pieces) == 2
    assert len(seg.interfaces) == 1
    _check_volume(seg, sphere_100)


def test_manual_plane_miss_warns(sphere_100):
    planner = PlannerConfig(
        mode="manual", planes=[PlaneSpec.from_any({"axis": "z", "position": 900.0})]
    )
    seg = segment(sphere_100, planner, SMALL_PRINTER, ConnectorConfig())
    assert len(seg.pieces) == 1
    assert seg.warnings
