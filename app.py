from __future__ import annotations

import json
import queue
import csv
import random
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
CONFIG = yaml.safe_load((CONFIG_PATH if CONFIG_PATH.exists() else ROOT / "config.example.yaml").read_text())
DATA = (ROOT / CONFIG.get("data_dir", "data")).resolve()
ORIGINALS, RESULTS = DATA / "originals", DATA / "results"
for folder in (ORIGINALS, RESULTS): folder.mkdir(parents=True, exist_ok=True)
DB = DATA / "inspection.sqlite3"
PREDICTOR = None
PREDICTOR_ERROR: str | None = None
PREDICTOR_LOCK = threading.Lock()
WORK_QUEUE: queue.Queue[str] = queue.Queue()

app = FastAPI(title="Drone Crack Inspection")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS images (
          id TEXT PRIMARY KEY, filename TEXT NOT NULL, received_at TEXT NOT NULL, captured_at TEXT,
          status TEXT NOT NULL, metadata TEXT NOT NULL, measurement TEXT, error TEXT
        )""")


def serialize(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["metadata"] = json.loads(d["metadata"])
    d["measurement"] = json.loads(d["measurement"]) if d["measurement"] else None
    d["original_url"] = f"/api/images/{d['id']}/original"
    d["overlay_url"] = f"/api/images/{d['id']}/overlay" if d["status"] == "done" else None
    d["mask_url"] = f"/api/images/{d['id']}/mask" if d["status"] == "done" else None
    return d


def get_predictor() -> CrackPredictor:
    global PREDICTOR, PREDICTOR_ERROR
    with PREDICTOR_LOCK:
        if PREDICTOR is None:
            try:
                # Torch/MPS can take a long time to initialise on macOS. Do
                # not hold dashboard startup hostage to it; load only when
                # the first received image reaches the inference worker.
                from .pipeline import CrackPredictor, InferenceConfig
                PREDICTOR = CrackPredictor(InferenceConfig(
                    checkpoint=(ROOT / CONFIG["checkpoint"]).resolve(), device=CONFIG.get("device", "auto"),
                    threshold=float(CONFIG.get("threshold", 0.1)), tile_size=int(CONFIG.get("tile_size", 448)),
                    tile_overlap=float(CONFIG.get("tile_overlap", .25)), min_component_px=int(CONFIG.get("min_component_px", 20)),
                ))
                PREDICTOR_ERROR = None
            except Exception as exc:
                PREDICTOR_ERROR = str(exc)
                raise
        return PREDICTOR


def process(image_id: str) -> None:
    with db() as con:
        row = con.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        con.execute("UPDATE images SET status = 'processing' WHERE id = ?", (image_id,))
    try:
        metadata = json.loads(row["metadata"])
        image_path = ORIGINALS / image_id / row["filename"]
        image, mask, measurement = get_predictor().predict(image_path, metadata.get("gsd_mm_per_px"))
        from .pipeline import write_visuals
        write_visuals(image, mask, measurement, RESULTS / image_id)
        with db() as con:
            con.execute("UPDATE images SET status='done', measurement=? WHERE id=?", (json.dumps(measurement), image_id))
    except Exception as exc:
        with db() as con:
            con.execute("UPDATE images SET status='failed', error=? WHERE id=?", (str(exc), image_id))


def worker() -> None:
    """One worker preserves upload order and avoids multiple GPU/MPS models competing."""
    while True:
        image_id = WORK_QUEUE.get()
        try:
            process(image_id)
        finally:
            WORK_QUEUE.task_done()


@app.on_event("startup")
def startup() -> None:
    init_db()
    # A computer restart must not leave previously accepted images invisible forever.
    with db() as con:
        con.execute("UPDATE images SET status='queued', error=NULL WHERE status='processing'")
        pending = con.execute("SELECT id FROM images WHERE status='queued' ORDER BY received_at").fetchall()
    for row in pending:
        WORK_QUEUE.put(row["id"])
    threading.Thread(target=worker, name="inference-worker", daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return (ROOT / "static" / "index.html").read_text()


@app.get("/api/health")
def health():
    return {"status": "ok", "model_loaded": PREDICTOR is not None, "model_error": PREDICTOR_ERROR}


@app.get("/api/settings")
def settings():
    return {"live_stream_url": CONFIG.get("live_stream_url", ""), "threshold": CONFIG.get("threshold", 0.1)}


@app.post("/api/images", status_code=202)
async def upload_image(file: UploadFile = File(...), metadata: str = Form("{}")):
    try:
        meta = json.loads(metadata)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "metadata must be valid JSON") from exc
    if not file.filename or Path(file.filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(415, "Upload a JPG, PNG, or WebP image")
    image_id = str(uuid.uuid4())
    safe_name = Path(file.filename).name
    destination = ORIGINALS / image_id / safe_name
    destination.parent.mkdir(parents=True)
    with destination.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    received = datetime.now(timezone.utc).isoformat()
    with db() as con:
        con.execute("INSERT INTO images VALUES (?, ?, ?, ?, 'queued', ?, NULL, NULL)",
                    (image_id, safe_name, received, meta.get("captured_at"), json.dumps(meta)))
    WORK_QUEUE.put(image_id)
    return {"image_id": image_id, "status": "queued"}


@app.post("/api/demo/test-set", status_code=202)
def enqueue_test_demo(count: int = 5):
    """Copy a deterministic sample of test images into the ordinary inference queue."""
    count = min(max(count, 1), 20)
    dataset = (ROOT / CONFIG.get("demo_dataset_dir", "../Dataset/final_merged_dataset_v1")).resolve()
    manifest = dataset / CONFIG.get("demo_manifest", "manifest_v2.csv")
    if not manifest.is_file():
        raise HTTPException(404, f"Demo manifest not found: {manifest}")
    with manifest.open(newline="") as f:
        candidates = [row for row in csv.DictReader(f) if row.get("split") == "test"]
    if len(candidates) < count:
        raise HTTPException(400, f"Only {len(candidates)} test images are available")
    selected = random.Random(42).sample(candidates, count)
    ids, now = [], datetime.now(timezone.utc).isoformat()
    with db() as con:
        for row in selected:
            source = dataset / row["image_path"]
            if not source.is_file():
                continue
            image_id = str(uuid.uuid4())
            filename = f"DEMO_{Path(row['image_path']).name}"
            target = ORIGINALS / image_id / filename
            target.parent.mkdir(parents=True)
            shutil.copy2(source, target)
            metadata = {"captured_at": now, "source_dataset": row.get("source_dataset"), "demo": True}
            con.execute("INSERT INTO images VALUES (?, ?, ?, ?, 'queued', ?, NULL, NULL)",
                        (image_id, filename, now, now, json.dumps(metadata)))
            ids.append(image_id)
    for image_id in ids:
        WORK_QUEUE.put(image_id)
    return {"queued": len(ids), "image_ids": ids}


@app.get("/api/images")
def list_images(limit: int = 100):
    with db() as con:
        rows = con.execute("SELECT * FROM images ORDER BY received_at DESC LIMIT ?", (min(max(limit, 1), 500),)).fetchall()
    return [serialize(r) for r in rows]


@app.get("/api/images/{image_id}")
def image_status(image_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if not row: raise HTTPException(404, "image not found")
    return serialize(row)


def served_file(image_id: str, kind: str):
    with db() as con:
        row = con.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if not row: raise HTTPException(404, "image not found")
    paths = {
        "original": ORIGINALS / image_id / row["filename"],
        "overlay": RESULTS / image_id / "overlay.jpg",
        "mask": RESULTS / image_id / "mask.png",
    }
    if not paths[kind].is_file(): raise HTTPException(404, f"{kind} not ready")
    return FileResponse(paths[kind])


@app.get("/api/images/{image_id}/original")
def original(image_id: str): return served_file(image_id, "original")

@app.get("/api/images/{image_id}/overlay")
def overlay(image_id: str): return served_file(image_id, "overlay")

@app.get("/api/images/{image_id}/mask")
def mask(image_id: str): return served_file(image_id, "mask")
