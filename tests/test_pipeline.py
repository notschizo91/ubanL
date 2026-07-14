import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import trimesh

from ubanl.cli import main
from ubanl.config import config_from_dict
from ubanl.geometry import obb_fits
from ubanl.pipeline import run


def _write_sphere(path: Path, radius: float = 50.0) -> None:
    mesh = trimesh.creation.icosphere(subdivisions=3, radius=radius)
    mesh.apply_translation([0, 0, radius])
    mesh.export(path)


def test_end_to_end(tmp_path):
    stl = tmp_path / "model.stl"
    _write_sphere(stl)
    cfg = config_from_dict(
        {
            "input": str(stl),
            "target_height_mm": 250.0,
            "printer": {"build_volume_mm": [150, 150, 150], "margin_mm": 5},
            "planner": {"mode": "auto"},
            "output": {"dir": str(tmp_path / "out"), "preview": True},
        }
    )
    result = run(cfg)
    seg = result.segmentation

    assert result.scale_applied == 2.5
    assert len(seg.pieces) >= 2
    allowed = cfg.printer.allowed_dims
    for piece in seg.piece_list():
        assert piece.mesh.is_watertight and piece.mesh.is_volume
        assert piece.label.startswith("P")
        assert obb_fits(piece.mesh, allowed)  # with connectors attached
    assert len(result.connectors.connectors) >= 1

    out = tmp_path / "out"
    manifest = json.loads((out / "manifest.json").read_text())
    assert len(manifest["pieces"]) == len(seg.pieces)
    assert manifest["assembly_order"] == [p["label"] for p in manifest["pieces"]]
    assert all(p["fits_build_volume"] for p in manifest["pieces"])
    for label in manifest["assembly_order"]:
        assert (out / f"{label}.stl").exists()
    assert (out / "assembled.glb").exists()
    assert (out / "exploded_preview.html").exists()


def test_labels_engraved(tmp_path):
    stl = tmp_path / "model.stl"
    _write_sphere(stl)
    base = {
        "input": str(stl),
        "target_height_mm": 160.0,
        "printer": {"build_volume_mm": [150, 150, 90], "margin_mm": 5},
        "output": {"dir": str(tmp_path / "a"), "preview": False},
    }
    plain = dict(base, labels={"enabled": False}, output={"dir": str(tmp_path / "a"), "preview": False})
    labeled = dict(base, output={"dir": str(tmp_path / "b"), "preview": False})
    vol_plain = sum(p.mesh.volume for p in run(config_from_dict(plain)).segmentation.piece_list())
    vol_labeled = sum(
        p.mesh.volume for p in run(config_from_dict(labeled)).segmentation.piece_list()
    )
    assert vol_labeled < vol_plain  # engraving removed material


def test_cli_info_and_coupon(tmp_path, capsys):
    stl = tmp_path / "model.stl"
    _write_sphere(stl)
    assert main(["info", str(stl)]) == 0
    out = capsys.readouterr().out
    assert "watertight:  True" in out

    assert main(["coupon", "-o", str(tmp_path / "coupon")]) == 0
    plate = trimesh.load(tmp_path / "coupon" / "coupon_plate.stl")
    pin = trimesh.load(tmp_path / "coupon" / "coupon_pin.stl")
    assert plate.is_watertight and pin.is_watertight


def test_cli_chop_flags(tmp_path, capsys):
    stl = tmp_path / "model.stl"
    _write_sphere(stl)
    code = main(
        [
            "chop",
            str(stl),
            "--target-height",
            "200",
            "--build-volume",
            "150x150x120",
            "--mode",
            "grid",
            "-o",
            str(tmp_path / "out"),
            "--no-labels",
        ]
    )
    assert code == 0
    assert (tmp_path / "out" / "manifest.json").exists()
