"""Local web UI: upload a model, chop it, preview the pieces, download them.

Everything runs on your machine; jobs execute in a background thread and the
browser polls their status. Start it with `ubanl serve`.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import tempfile
import threading
import uuid
import zipfile
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

from .config import ConfigError, ConnectorConfig, config_from_dict
from .pipeline import load_mesh, run

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 300 * 1024 * 1024
ALLOWED_SUFFIXES = {".stl", ".obj", ".3mf", ".ply", ".off", ".glb"}


@dataclass
class Job:
    id: str
    workdir: Path
    status: str = "queued"  # queued | running | done | error
    stage: str = ""
    error: str | None = None
    summary: dict | None = None


@dataclass
class Store:
    root: Path
    models: dict[str, dict] = field(default_factory=dict)
    jobs: dict[str, Job] = field(default_factory=dict)


def _mesh_info(path: Path) -> dict:
    mesh = load_mesh(path)
    return {
        "faces": int(len(mesh.faces)),
        "extents_mm": [round(float(v), 2) for v in mesh.extents],
        "watertight": bool(mesh.is_watertight),
        "solid": bool(mesh.is_volume),
        "volume_cm3": round(float(mesh.volume) / 1000.0, 1) if mesh.is_volume else None,
    }


def _run_job(store: Store, job: Job, model_path: Path, config_raw: dict) -> None:
    try:
        job.status = "running"
        raw = dict(config_raw)
        raw["input"] = str(model_path)
        raw.setdefault("output", {})
        raw["output"]["dir"] = str(job.workdir / "out")
        raw["output"]["preview"] = True
        cfg = config_from_dict(raw)

        def on_stage(name: str) -> None:
            job.stage = name

        result = run(cfg, on_stage=on_stage)
        manifest = json.loads((job.workdir / "out" / "manifest.json").read_text())
        job.summary = {
            "pieces": manifest["pieces"],
            "assembly_order": manifest["assembly_order"],
            "connectors": len(manifest["connectors"]),
            "interfaces": len(manifest["interfaces"]),
            "warnings": manifest["warnings"],
            "scale_applied": manifest["scale_applied"],
        }
        job.status = "done"
        job.stage = "done"
    except Exception as exc:  # surfaced to the browser, not just the log
        log.exception("job %s failed", job.id)
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"


def create_app(work_root: Path | None = None) -> FastAPI:
    root = Path(work_root) if work_root else Path(tempfile.mkdtemp(prefix="ubanl-web-"))
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root=root)
    app = FastAPI(title="ubanl", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return resources.files("ubanl").joinpath("webui/index.html").read_text()

    @app.get("/vendor/{path:path}")
    def vendor(path: str):
        if ".." in path or path.startswith("/"):
            raise HTTPException(404, "no such file")
        node = resources.files("ubanl").joinpath("webui/vendor").joinpath(path)
        if not node.is_file():
            raise HTTPException(404, "no such file")
        media = "text/javascript" if path.endswith(".js") else "text/plain"
        return Response(node.read_bytes(), media_type=media,
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.post("/api/upload")
    async def upload(file: UploadFile):
        suffix = Path(file.filename or "model.stl").suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(400, f"unsupported file type {suffix!r}")
        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large")
        model_id = uuid.uuid4().hex[:12]
        model_dir = store.root / "models" / model_id
        model_dir.mkdir(parents=True)
        path = model_dir / f"model{suffix}"
        path.write_bytes(data)
        try:
            info = _mesh_info(path)
        except Exception as exc:
            shutil.rmtree(model_dir, ignore_errors=True)
            raise HTTPException(400, f"could not read mesh: {exc}") from exc
        store.models[model_id] = {"path": path, "name": file.filename, "info": info}
        return {"model_id": model_id, "name": file.filename, **info}

    @app.post("/api/chop")
    def chop(body: dict):
        model = store.models.get(body.get("model_id", ""))
        if model is None:
            raise HTTPException(404, "unknown model_id; upload a file first")
        config_raw = body.get("config", {})
        if not isinstance(config_raw, dict):
            raise HTTPException(400, "config must be an object")
        # validate config early so the browser gets a clear error immediately
        try:
            probe = dict(config_raw)
            probe["input"] = str(model["path"])
            config_from_dict(probe)
        except (ConfigError, TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        job = Job(id=uuid.uuid4().hex[:12], workdir=store.root / "jobs" / uuid.uuid4().hex[:12])
        job.workdir.mkdir(parents=True)
        store.jobs[job.id] = job
        threading.Thread(
            target=_run_job, args=(store, job, model["path"], config_raw), daemon=True
        ).start()
        return {"job_id": job.id}

    def _get_job(job_id: str) -> Job:
        job = store.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return job

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        job = _get_job(job_id)
        return {
            "status": job.status,
            "stage": job.stage,
            "error": job.error,
            "summary": job.summary,
        }

    @app.get("/api/jobs/{job_id}/preview.glb")
    def job_preview(job_id: str):
        job = _get_job(job_id)
        path = job.workdir / "out" / "assembled.glb"
        if job.status != "done" or not path.exists():
            raise HTTPException(404, "preview not ready")
        return FileResponse(path, media_type="model/gltf-binary")

    @app.get("/api/jobs/{job_id}/pieces/{name}")
    def job_piece(job_id: str, name: str):
        job = _get_job(job_id)
        path = job.workdir / "out" / name
        # no path tricks: serve only flat piece/manifest files from the out dir
        if "/" in name or "\\" in name or ".." in name or not path.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(path, filename=name)

    @app.get("/api/jobs/{job_id}/all.zip")
    def job_zip(job_id: str):
        job = _get_job(job_id)
        out = job.workdir / "out"
        if job.status != "done":
            raise HTTPException(404, "job not finished")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(out.iterdir()):
                if path.suffix in (".stl", ".3mf", ".obj", ".json", ".glb", ".html"):
                    zf.write(path, arcname=path.name)
        return Response(
            buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=ubanl_pieces.zip"},
        )

    @app.get("/api/coupon.zip")
    def coupon_zip(diameter: float = 8.0, length: float = 8.0):
        from .coupon import coupon_pin, coupon_plate

        try:
            cfg = ConnectorConfig(diameter_mm=diameter, length_mm=length)
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from exc
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("coupon_plate.stl", coupon_plate(cfg).export(file_type="stl"))
            zf.writestr("coupon_pin.stl", coupon_pin(cfg).export(file_type="stl"))
        return Response(
            buf.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=ubanl_coupon.zip"},
        )

    return app


def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False) -> None:
    import uvicorn

    app = create_app()
    if open_browser:
        import webbrowser

        threading.Timer(1.0, webbrowser.open, args=(f"http://{host}:{port}",)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
