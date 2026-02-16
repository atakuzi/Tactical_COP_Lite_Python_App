
import os
import json
import time
import math
import sqlite3
import threading
from html import escape
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
from tak_bridge import TAKBridge

APP_TITLE = "Tactical COP Lite"
DB_PATH = os.getenv("COP_DB_PATH", "cop.db")
RTSP_URL = os.getenv("RTSP_URL", "").strip()
FPV_SIM_ENABLED = os.getenv("FPV_SIM_ENABLED", "true").strip().lower() in ("1", "true", "yes")

# TAK Server bridge (opt-in: set TAK_HOST to enable)
TAK_HOST = os.getenv("TAK_HOST", "").strip()
TAK_PORT = int(os.getenv("TAK_PORT", "8087"))
TAK_TLS = os.getenv("TAK_TLS", "false").strip().lower() in ("1", "true", "yes")
TAK_CERT = os.getenv("TAK_CERT", "").strip()
TAK_KEY = os.getenv("TAK_KEY", "").strip()
TAK_CA = os.getenv("TAK_CA", "").strip()
TAK_CALLSIGN = os.getenv("TAK_CALLSIGN", "COP-LITE").strip()
TAK_PUSH_INTERVAL = int(os.getenv("TAK_PUSH_INTERVAL", "30"))

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
    if tak_bridge:
        tak_bridge.start()
    yield
    if tak_bridge:
        tak_bridge.stop()

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

@app.get("/api/tak/status")
def api_tak_status():
    if tak_bridge is None:
        return {"enabled": False, "reason": "TAK_HOST not configured"}
    return {"enabled": True, **tak_bridge.status()}

# --- Simulated FPV drones ---------------------------------------------------
FPV_DRONES = [
    {"uid": "FPV-DRONE-1", "callsign": "RAVEN-11", "base_lat": 50.1109, "base_lon": 8.6821, "radius_km": 8.0, "period_s": 140.0, "phase": 0.0},
    {"uid": "FPV-DRONE-2", "callsign": "RAVEN-12", "base_lat": 50.2600, "base_lon": 8.9300, "radius_km": 10.0, "period_s": 165.0, "phase": 1.6},
    {"uid": "FPV-DRONE-3", "callsign": "RAVEN-13", "base_lat": 49.9800, "base_lon": 8.2100, "radius_km": 7.0, "period_s": 120.0, "phase": 2.9},
]


def _simulated_fpv_state(now_ts: float) -> List[Dict[str, Any]]:
    drones = []
    for spec in FPV_DRONES:
        theta = ((now_ts / spec["period_s"]) * (2.0 * math.pi)) + spec["phase"]
        radius_deg_lat = spec["radius_km"] / 111.0
        lat = spec["base_lat"] + radius_deg_lat * math.sin(theta)
        lon_scale = max(0.25, math.cos(math.radians(spec["base_lat"])))
        lon = spec["base_lon"] + (radius_deg_lat / lon_scale) * math.cos(theta)

        omega = (2.0 * math.pi) / spec["period_s"]
        speed_mps = omega * (spec["radius_km"] * 1000.0)
        heading_deg = (math.degrees(theta) + 90.0) % 360.0
        altitude_m = 120.0 + 30.0 * math.sin(theta * 1.7)
        battery_pct = max(18.0, 85.0 - ((now_ts + spec["phase"] * 31.0) % 700.0) * 0.08)

        drones.append({
            "uid": spec["uid"],
            "callsign": spec["callsign"],
            "lat": lat,
            "lon": lon,
            "heading_deg": heading_deg,
            "speed_mps": speed_mps,
            "altitude_m": altitude_m,
            "battery_pct": battery_pct,
            "stream_url": f"/video/fpv/{spec['uid']}.mjpeg",
        })
    return drones


def _upsert_simulated_fpv_tracks(now_ts: float) -> List[Dict[str, Any]]:
    drones = _simulated_fpv_state(now_ts)
    for d in drones:
        upsert_track(
            uid=d["uid"],
            side="friendly",
            layer="air",
            lat=d["lat"],
            lon=d["lon"],
            meta={
                "callsign": d["callsign"],
                "sidc": "SFAPMFQ---*****",
                "source": "simulated_fpv",
                "heading_deg": round(d["heading_deg"], 1),
                "speed_mps": round(d["speed_mps"], 1),
                "altitude_m": round(d["altitude_m"], 1),
                "battery_pct": round(d["battery_pct"], 1),
            },
        )
    return drones


@app.get("/api/fpv/drones")
def api_fpv_drones():
    if not FPV_SIM_ENABLED:
        return {"enabled": False, "drones": []}
    now_ts = time.time()
    drones = _upsert_simulated_fpv_tracks(now_ts)
    return {"enabled": True, "generated_at": utc_now_iso(), "drones": drones}

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


def _render_simulated_fpv_frame(drone: Dict[str, Any], now_ts: float) -> bytes:
    w, h = 640, 360
    img = np.zeros((h, w, 3), dtype=np.uint8)

    horizon = int(h * 0.43 + 16 * math.sin(now_ts * 0.9 + drone["heading_deg"] * 0.01))
    sky = np.linspace(70, 15, max(horizon, 1), dtype=np.uint8).reshape(-1, 1)
    img[:horizon, :, 2] = sky
    img[:horizon, :, 1] = (sky * 0.8).astype(np.uint8)
    img[:horizon, :, 0] = (sky * 0.45).astype(np.uint8)

    ground_h = h - horizon
    if ground_h > 0:
        ground = np.linspace(20, 75, ground_h, dtype=np.uint8).reshape(-1, 1)
        img[horizon:, :, 1] = ground
        img[horizon:, :, 2] = (ground * 0.3).astype(np.uint8)

    t = now_ts
    road_dx = int(70 * math.sin(t * 0.6 + drone["uid"].__hash__() % 10))
    road_center_top = (w // 2) + (road_dx // 3)
    road_center_bottom = (w // 2) + road_dx
    road_width_top = 80
    road_width_bottom = 260
    road_poly = np.array([[
        (road_center_top - road_width_top // 2, horizon + 8),
        (road_center_top + road_width_top // 2, horizon + 8),
        (road_center_bottom + road_width_bottom // 2, h - 1),
        (road_center_bottom - road_width_bottom // 2, h - 1),
    ]], dtype=np.int32)
    cv2.fillPoly(img, road_poly, (65, 65, 65))

    dash_y = horizon + 15
    while dash_y < h:
        y0 = int(dash_y + (t * 120) % 28)
        y1 = min(y0 + 10, h - 1)
        if y0 < h - 1:
            x = int(road_center_top + (road_center_bottom - road_center_top) * ((y0 - horizon) / max(1, (h - horizon))))
            cv2.line(img, (x, y0), (x, y1), (235, 235, 235), 2, cv2.LINE_AA)
        dash_y += 28

    center = (w // 2, h // 2)
    cv2.circle(img, center, 18, (0, 255, 0), 1, cv2.LINE_AA)
    cv2.line(img, (center[0] - 24, center[1]), (center[0] + 24, center[1]), (0, 255, 0), 1, cv2.LINE_AA)
    cv2.line(img, (center[0], center[1] - 24), (center[0], center[1] + 24), (0, 255, 0), 1, cv2.LINE_AA)

    osd = [
        f"{drone['callsign']} FPV LINK",
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        f"LAT {drone['lat']:+.5f}  LON {drone['lon']:+.5f}",
        f"ALT {drone['altitude_m']:.0f}m  SPD {drone['speed_mps']:.1f}m/s  HDG {drone['heading_deg']:.0f}",
        f"BAT {drone['battery_pct']:.0f}%",
    ]
    y = 24
    for line in osd:
        cv2.putText(img, line, (14, y), cv2.FONT_HERSHEY_SIMPLEX, 0.56, (0, 255, 0), 1, cv2.LINE_AA)
        y += 22

    cv2.rectangle(img, (w - 132, 12), (w - 16, 30), (35, 35, 35), -1)
    bat_w = int(108 * (drone["battery_pct"] / 100.0))
    cv2.rectangle(img, (w - 128, 16), (w - 128 + bat_w, 26), (0, 200, 0), -1)

    ok, jpg = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
    return jpg.tobytes() if ok else b""

# TAK Server bridge (only created when TAK_HOST is configured)
tak_bridge: Optional[TAKBridge] = None
if TAK_HOST:
    tak_bridge = TAKBridge(
        host=TAK_HOST,
        port=TAK_PORT,
        tls=TAK_TLS,
        cert_path=TAK_CERT,
        key_path=TAK_KEY,
        ca_path=TAK_CA,
        callsign=TAK_CALLSIGN,
        push_interval=TAK_PUSH_INTERVAL,
        upsert_fn=upsert_track,
        list_fn=list_tracks,
    )

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


@app.get("/video/fpv/{drone_uid}.mjpeg")
def video_fpv(drone_uid: str):
    if not FPV_SIM_ENABLED:
        raise HTTPException(status_code=404, detail="Simulated FPV disabled")

    valid_uids = {d["uid"] for d in FPV_DRONES}
    if drone_uid not in valid_uids:
        raise HTTPException(status_code=404, detail="Unknown drone")

    def gen():
        boundary = b"--frame"
        while True:
            now_ts = time.time()
            drones = _simulated_fpv_state(now_ts)
            drone = next((d for d in drones if d["uid"] == drone_uid), None)
            if drone is None:
                time.sleep(0.1)
                continue
            jpg = _render_simulated_fpv_frame(drone, now_ts)
            if not jpg:
                time.sleep(0.05)
                continue
            yield boundary + b"\r\n"
            yield b"Content-Type: image/jpeg\r\n"
            yield f"Content-Length: {len(jpg)}\r\n\r\n".encode("utf-8")
            yield jpg + b"\r\n"
            time.sleep(0.08)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.get("/video/pip", response_class=HTMLResponse)
def video_pip(src: str = "/video/mjpeg"):
    if not src.startswith("/video/"):
        src = "/video/mjpeg"
    safe_src = escape(src, quote=True)
    return HTMLResponse(content=f"""
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
  <img src="{safe_src}" alt="FMV"/>
</body>
</html>
""")

@app.get("/video/view", response_class=HTMLResponse)
def video_view(src: str = "/video/mjpeg"):
    return video_pip(src=src)
