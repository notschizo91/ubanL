import io
import json
import time
import zipfile

import pytest
import trimesh
from fastapi.testclient import TestClient

from ubanl.server import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(work_root=tmp_path / "web")
    with TestClient(app) as c:
        yield c


def _sphere_stl_bytes(radius: float = 50.0) -> bytes:
    mesh = trimesh.creation.icosphere(subdivisions=3, radius=radius)
    mesh.apply_translation([0, 0, radius])
    return mesh.export(file_type="stl")


def _upload(client) -> str:
    res = client.post(
        "/api/upload",
        files={"file": ("sphere.stl", io.BytesIO(_sphere_stl_bytes()), "model/stl")},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["watertight"] is True
    assert abs(body["extents_mm"][0] - 100.0) < 1.0
    return body["model_id"]


def _wait_done(client, job_id: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = client.get(f"/api/jobs/{job_id}")
        assert res.status_code == 200
        j = res.json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.5)
    raise TimeoutError("job did not finish")


def test_index_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "ubanL" in res.text


def test_upload_rejects_junk(client):
    res = client.post(
        "/api/upload", files={"file": ("evil.exe", io.BytesIO(b"MZ"), "application/x-dos")}
    )
    assert res.status_code == 400
    res = client.post(
        "/api/upload", files={"file": ("bad.stl", io.BytesIO(b"not a mesh"), "model/stl")}
    )
    assert res.status_code == 400


def test_chop_flow(client):
    model_id = _upload(client)
    config = {
        "target_height_mm": 160.0,
        "printer": {"build_volume_mm": [150, 150, 90], "margin_mm": 5},
        "planner": {"mode": "grid"},
        "labels": {"enabled": False},
    }
    res = client.post("/api/chop", json={"model_id": model_id, "config": config})
    assert res.status_code == 200, res.text
    job_id = res.json()["job_id"]

    j = _wait_done(client, job_id)
    assert j["status"] == "done", j
    summary = j["summary"]
    assert len(summary["pieces"]) >= 2
    assert summary["connectors"] >= 1
    labels = [p["label"] for p in summary["pieces"]]

    # preview + piece downloads + zip
    res = client.get(f"/api/jobs/{job_id}/preview.glb")
    assert res.status_code == 200 and len(res.content) > 1000
    res = client.get(f"/api/jobs/{job_id}/pieces/{labels[0]}.stl")
    assert res.status_code == 200
    piece = trimesh.load(io.BytesIO(res.content), file_type="stl")
    assert piece.is_watertight

    res = client.get(f"/api/jobs/{job_id}/all.zip")
    assert res.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(res.content))
    names = zf.namelist()
    assert "manifest.json" in names
    for label in labels:
        assert f"{label}.stl" in names
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["assembly_order"] == labels


def test_chop_bad_config_rejected_up_front(client):
    model_id = _upload(client)
    res = client.post(
        "/api/chop",
        json={"model_id": model_id, "config": {"planner": {"mode": "bogus"}}},
    )
    assert res.status_code == 400


def test_piece_path_traversal_blocked(client):
    model_id = _upload(client)
    res = client.post("/api/chop", json={"model_id": model_id, "config": {
        "target_height_mm": 120.0,
        "printer": {"build_volume_mm": [150, 150, 90]},
        "planner": {"mode": "grid"},
        "labels": {"enabled": False},
        "connectors": {"enabled": False},
    }})
    job_id = res.json()["job_id"]
    _wait_done(client, job_id)
    res = client.get(f"/api/jobs/{job_id}/pieces/..%2f..%2fmodels")
    assert res.status_code == 404


def test_coupon_zip(client):
    res = client.get("/api/coupon.zip", params={"diameter": 6.0, "length": 8.0})
    assert res.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(res.content))
    assert set(zf.namelist()) == {"coupon_plate.stl", "coupon_pin.stl"}
    plate = trimesh.load(io.BytesIO(zf.read("coupon_plate.stl")), file_type="stl")
    assert plate.is_watertight
