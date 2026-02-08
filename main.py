
import os
import json
import time
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, Any, Dict, List

import numpy as np
import cv2
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from lxml import etree

APP_TITLE = "Tactical COP Lite"
DB_PATH = os.getenv("COP_DB_PATH", "cop.db")
RTSP_URL = os.getenv("RTSP_URL", "").strip()

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                uid TEXT PRIMARY KEY,
                side TEXT NOT NULL,
                layer TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                updated_at TEXT NOT NULL,
                meta_json TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()

def upsert_track(uid: str, side: str, layer: str, lat: float, lon: float, meta: Dict[str, Any]) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT INTO tracks(uid, side, layer, lat, lon, updated_at, meta_json)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(uid) DO UPDATE SET
                side=excluded.side,
                layer=excluded.layer,
                lat=excluded.lat,
                lon=excluded.lon,
                updated_at=excluded.updated_at,
                meta_json=excluded.meta_json
        """, (uid, side, layer, lat, lon, utc_now_iso(), json.dumps(meta or {})))
        conn.commit()
    finally:
        conn.close()

def list_tracks() -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute("SELECT uid, side, layer, lat, lon, updated_at, meta_json FROM tracks")
        rows = cur.fetchall()
        out = []
        for uid, side, layer, lat, lon, updated_at, meta_json in rows:
            out.append({
                "uid": uid,
                "side": side,
                "layer": layer,
                "lat": lat,
                "lon": lon,
                "updated_at": updated_at,
                "meta": json.loads(meta_json or "{}")
            })
        return out
    finally:
        conn.close()

class TrackIn(BaseModel):
    uid: str = Field(..., description="Unique track ID")
    side: str = Field(..., description="friendly|enemy|neutral|unknown")
    layer: str = Field(..., description="friendly|enemy|fires|air|ew|other")
    lat: float
    lon: float
    meta: Dict[str, Any] = Field(default_factory=dict)

class BFTBatch(BaseModel):
    tracks: List[TrackIn]

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title=APP_TITLE, lifespan=lifespan)

BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "app_title": APP_TITLE})

@app.get("/api/tracks")
def api_tracks():
    return {"tracks": list_tracks(), "server_time": utc_now_iso()}

@app.post("/api/tracks")
def api_upsert(track: TrackIn):
    # Basic validation for demo (keep simple)
    if track.side not in {"friendly", "enemy", "neutral", "unknown"}:
        raise HTTPException(status_code=400, detail="Invalid side")
    if track.layer not in {"friendly", "enemy", "fires", "air", "ew", "other"}:
        raise HTTPException(status_code=400, detail="Invalid layer")
    upsert_track(track.uid, track.side, track.layer, track.lat, track.lon, track.meta)
    return {"ok": True, "updated_at": utc_now_iso()}

@app.post("/ingest/bft")
def ingest_bft(batch: BFTBatch):
    for t in batch.tracks:
        upsert_track(t.uid, t.side, t.layer, t.lat, t.lon, t.meta)
    return {"ok": True, "count": len(batch.tracks)}

# --- TAK Cursor-on-Target (CoT) ingest (very minimal) ---
# Expects an <event ...><point lat=".." lon=".."/></event>
@app.post("/tak/cot")
async def ingest_cot(request: Request):
    raw = await request.body()
    try:
        root = etree.fromstring(raw)
        uid = root.get("uid") or root.get("id") or f"COT-{int(time.time())}"
        cot_type = root.get("type", "")
        side = "friendly" if cot_type.startswith("a-f") else "enemy" if cot_type.startswith("a-h") else "unknown"
        # map side to layer for display
        layer = "friendly" if side == "friendly" else "enemy" if side == "enemy" else "other"
        # derive a default MIL-STD-2525C SIDC from the CoT type
        aff = "F" if side == "friendly" else "H" if side == "enemy" else "U"
        dim = "A" if cot_type.startswith(("a-f-A", "a-h-A")) else "G"
        sidc = f"S{aff}{dim}P------*****"
        pt = root.find(".//point")
        if pt is None:
            raise ValueError("No point element")
        lat = float(pt.get("lat"))
        lon = float(pt.get("lon"))
        meta = {"cot_type": cot_type, "how": root.get("how"), "time": root.get("time"), "start": root.get("start"), "stale": root.get("stale"), "sidc": sidc}
        upsert_track(uid, side, layer, lat, lon, meta)
        return {"ok": True, "uid": uid}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid CoT: {e}")

@app.get("/tak/cot/pull")
def pull_cot():
    # Simple CoT export of all tracks (demo only)
    tracks = list_tracks()
    events = []
    now = datetime.now(timezone.utc)
    for t in tracks:
        ev = etree.Element("event")
        ev.set("version", "2.0")
        ev.set("uid", t["uid"])
        ev.set("type", "a-f-G-U-C" if t["side"] == "friendly" else "a-h-G-U-C" if t["side"] == "enemy" else "b-m-p-s-m")
        ev.set("how", "m-g")
        ts = now.isoformat()
        ev.set("time", ts)
        ev.set("start", ts)
        ev.set("stale", (now.replace(microsecond=0)).isoformat())
        pt = etree.SubElement(ev, "point")
        pt.set("lat", str(t["lat"]))
        pt.set("lon", str(t["lon"]))
        pt.set("hae", "0")
        pt.set("ce", "25")
        pt.set("le", "25")
        events.append(etree.tostring(ev, pretty_print=True))
    xml = b"\n".join(events) if events else b""
    return Response(content=xml, media_type="application/xml")

# --- MJPEG video streaming (RTSP -> MJPEG) ---
class FrameSource:
    def __init__(self, rtsp_url: str):
        self.rtsp_url = rtsp_url
        self.lock = threading.Lock()
        self.frame_jpeg: Optional[bytes] = None
        self.running = False
        self.thread: Optional[threading.Thread] = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        cap = None
        if self.rtsp_url:
            cap = cv2.VideoCapture(self.rtsp_url)
        t0 = time.time()
        while self.running:
            if cap is not None and cap.isOpened():
                ok, frame = cap.read()
                if not ok:
                    # backoff and retry
                    time.sleep(0.2)
                    continue
            else:
                # Generate a simple test pattern (works out of the box)
                w, h = 640, 360
                frame = np.zeros((h, w, 3), dtype=np.uint8)
                dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                msg = "FMV (TEST FEED)" if not self.rtsp_url else "FMV (RTSP)"
                cv2.putText(frame, msg, (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(frame, dt, (20, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
                # moving bar
                x = int(((time.time() - t0) * 60) % w)
                cv2.rectangle(frame, (x, 150), (min(x + 80, w-1), 220), (255, 255, 255), -1)
                time.sleep(0.03)

            # Encode JPEG
            ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
            if ok:
                with self.lock:
                    self.frame_jpeg = jpg.tobytes()

        if cap is not None:
            cap.release()

    def get_jpeg(self) -> Optional[bytes]:
        with self.lock:
            return self.frame_jpeg

frame_source = FrameSource(RTSP_URL)

@app.get("/video/mjpeg")
def video_mjpeg():
    frame_source.start()

    def gen():
        boundary = b"--frame"
        while True:
            jpg = frame_source.get_jpeg()
            if jpg is None:
                time.sleep(0.05)
                continue
            yield boundary + b"\r\n"
            yield b"Content-Type: image/jpeg\r\n"
            yield f"Content-Length: {len(jpg)}\r\n\r\n".encode("utf-8")
            yield jpg + b"\r\n"
            time.sleep(0.05)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.get("/video/pip", response_class=HTMLResponse)
def video_pip():
    return HTMLResponse(content="""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>FMV PiP</title>
  <style>
    body { margin:0; background:#000; }
    img { width:100vw; height:100vh; object-fit:contain; display:block; }
  </style>
</head>
<body>
  <img src="/video/mjpeg" alt="FMV"/>
</body>
</html>
""")

@app.get("/video/view", response_class=HTMLResponse)
def video_view():
    return video_pip()
