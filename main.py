import asyncio
import json
import logging
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Dict, List, Optional
from uuid import uuid4

import cv2
import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from lxml import etree
from pydantic import BaseModel, Field, ValidationError, field_validator
from tak_bridge import TAKBridge
from zenoh_bridge import ZenohBridge

APP_TITLE = "Tactical COP Lite"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError:
            raise RuntimeError(f"Invalid integer for {name}: {raw!r}")
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_csv(name: str) -> List[str]:
    raw = os.getenv(name, "")
    return [v.strip() for v in raw.split(",") if v.strip()]


LOG_LEVEL = os.getenv("COP_LOG_LEVEL", "INFO").strip().upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cop_lite")

DB_PATH = os.getenv("COP_DB_PATH", "cop.db").strip() or "cop.db"
RTSP_URL = os.getenv("RTSP_URL", "").strip()
FPV_SIM_ENABLED = _env_bool("FPV_SIM_ENABLED", True)
LIVE_FEED_URL = os.getenv("LIVE_FEED_URL", "").strip()
LIVE_FEED_INTERVAL = _env_int("LIVE_FEED_INTERVAL", 5, minimum=2, maximum=300)
LIVE_FEED_TIMEOUT_S = _env_int("LIVE_FEED_TIMEOUT_S", 8, minimum=2, maximum=60)
MAX_META_BYTES = _env_int("MAX_META_BYTES", 8192, minimum=256, maximum=1000000)

# Optional access control for write endpoints.
COP_API_KEY = os.getenv("COP_API_KEY", "").strip()

# Deployment hardening controls.
ENABLE_DOCS = _env_bool("ENABLE_DOCS", True)
TRUSTED_HOSTS = _env_csv("TRUSTED_HOSTS")
CORS_ORIGINS = _env_csv("CORS_ORIGINS")

# TAK Server bridge (opt-in: set TAK_HOST to enable).
TAK_HOST = os.getenv("TAK_HOST", "").strip()
TAK_PORT = _env_int("TAK_PORT", 8087, minimum=1, maximum=65535)
TAK_TLS = _env_bool("TAK_TLS", False)
TAK_TLS_INSECURE_SKIP_VERIFY = _env_bool("TAK_TLS_INSECURE_SKIP_VERIFY", False)
TAK_CERT = os.getenv("TAK_CERT", "").strip()
TAK_KEY = os.getenv("TAK_KEY", "").strip()
TAK_CA = os.getenv("TAK_CA", "").strip()
TAK_CALLSIGN = os.getenv("TAK_CALLSIGN", "COP-LITE").strip() or "COP-LITE"
TAK_PUSH_INTERVAL = _env_int("TAK_PUSH_INTERVAL", 30, minimum=5, maximum=3600)

# Zenoh pub/sub bridge (core service).
ZENOH_CONNECT = _env_csv("ZENOH_CONNECT") or ["tcp/127.0.0.1:7447"]
ZENOH_PUB_KEYEXPR = os.getenv("ZENOH_PUB_KEYEXPR", "cop/tracks").strip() or "cop/tracks"
ZENOH_SUB_KEYEXPR = os.getenv("ZENOH_SUB_KEYEXPR", "cop/tracks").strip() or "cop/tracks"
ZENOH_PUBLISH = _env_bool("ZENOH_PUBLISH", True)
ZENOH_SUBSCRIBE = _env_bool("ZENOH_SUBSCRIBE", True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_side_layer(side: str, layer: str) -> None:
    if side not in {"friendly", "enemy", "neutral", "unknown"}:
        raise HTTPException(status_code=400, detail="Invalid side")
    if layer not in {"friendly", "enemy", "fires", "air", "ew", "other"}:
        raise HTTPException(status_code=400, detail="Invalid layer")


def _db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _db_connection()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracks (
                uid TEXT PRIMARY KEY,
                side TEXT NOT NULL,
                layer TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                updated_at TEXT NOT NULL,
                meta_json TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_updated_at ON tracks(updated_at)")
        conn.commit()
    finally:
        conn.close()


def upsert_track(
    uid: str,
    side: str,
    layer: str,
    lat: float,
    lon: float,
    meta: Dict[str, Any],
    publish: bool = True,
) -> None:
    ts = utc_now_iso()
    clean_meta = dict(meta or {})
    conn = _db_connection()
    try:
        conn.execute(
            """
            INSERT INTO tracks(uid, side, layer, lat, lon, updated_at, meta_json)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(uid) DO UPDATE SET
                side=excluded.side,
                layer=excluded.layer,
                lat=excluded.lat,
                lon=excluded.lon,
                updated_at=excluded.updated_at,
                meta_json=excluded.meta_json
            """,
            (uid, side, layer, lat, lon, ts, json.dumps(clean_meta, separators=(",", ":"))),
        )
        conn.commit()
    finally:
        conn.close()

    if publish and zenoh_bridge and clean_meta.get("source") != "zenoh":
        zenoh_bridge.publish_track(
            {
                "uid": uid,
                "side": side,
                "layer": layer,
                "lat": lat,
                "lon": lon,
                "updated_at": ts,
                "meta": clean_meta,
            }
        )


def list_tracks() -> List[Dict[str, Any]]:
    conn = _db_connection()
    try:
        rows = conn.execute("SELECT uid, side, layer, lat, lon, updated_at, meta_json FROM tracks").fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "uid": row["uid"],
                    "side": row["side"],
                    "layer": row["layer"],
                    "lat": row["lat"],
                    "lon": row["lon"],
                    "updated_at": row["updated_at"],
                    "meta": json.loads(row["meta_json"] or "{}"),
                }
            )
        return out
    finally:
        conn.close()


class TrackIn(BaseModel):
    uid: str = Field(..., min_length=1, max_length=128, description="Unique track ID")
    side: str = Field(..., description="friendly|enemy|neutral|unknown")
    layer: str = Field(..., description="friendly|enemy|fires|air|ew|other")
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    meta: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("uid")
    @classmethod
    def _uid_non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("uid cannot be blank")
        return value

    @field_validator("meta")
    @classmethod
    def _meta_bounded_and_json(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        encoded = json.dumps(value or {}, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_META_BYTES:
            raise ValueError(f"meta exceeds MAX_META_BYTES ({MAX_META_BYTES})")
        return value


class BFTBatch(BaseModel):
    tracks: List[TrackIn]


def require_write_access(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    if COP_API_KEY and x_api_key != COP_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


class LiveFeedPoller:
    def __init__(self, url: str, interval_s: int):
        self.url = url
        self.interval_s = interval_s
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_poll_at: Optional[str] = None
        self._last_success_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._ingested_total = 0

    def start(self) -> None:
        if self._running or not self.url:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="live-feed-poller")
        self._thread.start()
        log.info("Live feed poller started for %s", self.url)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": bool(self.url),
                "url": self.url,
                "interval_s": self.interval_s,
                "last_poll_at": self._last_poll_at,
                "last_success_at": self._last_success_at,
                "last_error": self._last_error,
                "ingested_total": self._ingested_total,
            }

    def _run(self) -> None:
        while self._running:
            self._poll_once()
            for _ in range(self.interval_s):
                if not self._running:
                    break
                time.sleep(1)

    def _poll_once(self) -> None:
        with self._lock:
            self._last_poll_at = utc_now_iso()
        try:
            req = urllib.request.Request(
                self.url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "tactical-cop-lite/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=LIVE_FEED_TIMEOUT_S) as resp:
                payload = json.loads(resp.read().decode("utf-8"))

            raw_tracks = payload if isinstance(payload, list) else payload.get("tracks", [])
            if not isinstance(raw_tracks, list):
                raise ValueError("live feed payload must be a list or an object with tracks[]")

            ingested = 0
            for raw in raw_tracks:
                t = TrackIn.model_validate(raw)
                _validate_side_layer(t.side, t.layer)
                meta = dict(t.meta or {})
                meta.setdefault("source", "live_feed")
                upsert_track(t.uid, t.side, t.layer, t.lat, t.lon, meta)
                ingested += 1

            with self._lock:
                self._last_success_at = utc_now_iso()
                self._last_error = None
                self._ingested_total += ingested
        except ValidationError as e:
            with self._lock:
                self._last_error = f"Validation error: {e.errors()[0].get('msg', 'invalid payload')}"
        except Exception as e:
            with self._lock:
                self._last_error = str(e)


class FrameSource:
    def __init__(self, rtsp_url: str):
        self.rtsp_url = rtsp_url
        self.lock = threading.Lock()
        self.frame_jpeg: Optional[bytes] = None
        self.running = False
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="fmv-source")
        self.thread.start()

    def stop(self) -> None:
        self.running = False
        if self.thread:
            self.thread.join(timeout=3)

    def _run(self) -> None:
        cap = None
        if self.rtsp_url:
            cap = cv2.VideoCapture(self.rtsp_url)
            if not cap.isOpened():
                log.warning("Unable to open RTSP_URL; serving generated test feed")

        t0 = time.time()
        while self.running:
            if cap is not None and cap.isOpened():
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.2)
                    continue
            else:
                w, h = 640, 360
                frame = np.zeros((h, w, 3), dtype=np.uint8)
                dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                msg = "FMV (TEST FEED)" if not self.rtsp_url else "FMV (RTSP)"
                cv2.putText(frame, msg, (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(frame, dt, (20, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
                x = int(((time.time() - t0) * 60) % w)
                cv2.rectangle(frame, (x, 150), (min(x + 80, w - 1), 220), (255, 255, 255), -1)
                time.sleep(0.03)

            ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
            if ok:
                with self.lock:
                    self.frame_jpeg = jpg.tobytes()

        if cap is not None:
            cap.release()

    def get_jpeg(self) -> Optional[bytes]:
        with self.lock:
            return self.frame_jpeg


# Simulated FPV drones.
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

        drones.append(
            {
                "uid": spec["uid"],
                "callsign": spec["callsign"],
                "lat": lat,
                "lon": lon,
                "heading_deg": heading_deg,
                "speed_mps": speed_mps,
                "altitude_m": altitude_m,
                "battery_pct": battery_pct,
                "stream_url": f"/video/fpv/{spec['uid']}.mjpeg",
            }
        )
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
    road_dx = int(70 * math.sin(t * 0.6 + (hash(drone["uid"]) % 10)))
    road_center_top = (w // 2) + (road_dx // 3)
    road_center_bottom = (w // 2) + road_dx
    road_width_top = 80
    road_width_bottom = 260
    road_poly = np.array(
        [[
            (road_center_top - road_width_top // 2, horizon + 8),
            (road_center_top + road_width_top // 2, horizon + 8),
            (road_center_bottom + road_width_bottom // 2, h - 1),
            (road_center_bottom - road_width_bottom // 2, h - 1),
        ]],
        dtype=np.int32,
    )
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


# Runtime components.
frame_source = FrameSource(RTSP_URL)

tak_bridge: Optional[TAKBridge] = None
if TAK_HOST:
    tak_bridge = TAKBridge(
        host=TAK_HOST,
        port=TAK_PORT,
        tls=TAK_TLS,
        tls_insecure_skip_verify=TAK_TLS_INSECURE_SKIP_VERIFY,
        cert_path=TAK_CERT,
        key_path=TAK_KEY,
        ca_path=TAK_CA,
        callsign=TAK_CALLSIGN,
        push_interval=TAK_PUSH_INTERVAL,
        upsert_fn=upsert_track,
        list_fn=list_tracks,
    )

live_feed_poller: Optional[LiveFeedPoller] = None
if LIVE_FEED_URL:
    live_feed_poller = LiveFeedPoller(url=LIVE_FEED_URL, interval_s=LIVE_FEED_INTERVAL)

zenoh_bridge = ZenohBridge(
    pub_keyexpr=ZENOH_PUB_KEYEXPR,
    sub_keyexpr=ZENOH_SUB_KEYEXPR,
    connect_endpoints=ZENOH_CONNECT,
    enable_publish=ZENOH_PUBLISH,
    enable_subscribe=ZENOH_SUBSCRIBE,
    upsert_fn=upsert_track,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    zenoh_bridge.start()
    if tak_bridge:
        tak_bridge.start()
    if live_feed_poller:
        live_feed_poller.start()
    log.info("Application started")
    yield
    frame_source.stop()
    if tak_bridge:
        tak_bridge.stop()
    if live_feed_poller:
        live_feed_poller.stop()
    zenoh_bridge.stop()
    log.info("Application stopped")


app = FastAPI(
    title=APP_TITLE,
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)

if TRUSTED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key"],
    )


@app.middleware("http")
async def add_request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid4()))
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000.0

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["X-Process-Time-Ms"] = f"{duration_ms:.2f}"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "app_title": APP_TITLE})


@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": utc_now_iso()}


@app.get("/readyz")
def readyz():
    try:
        conn = _db_connection()
        conn.execute("SELECT 1")
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db unavailable: {e}")
    z_status = zenoh_bridge.status()
    if not z_status.get("ready"):
        reason = z_status.get("last_error") or "zenoh not ready"
        raise HTTPException(status_code=503, detail=f"zenoh unavailable: {reason}")
    return {"status": "ready", "time": utc_now_iso()}


@app.get("/api/tracks")
def api_tracks():
    return {"tracks": list_tracks(), "server_time": utc_now_iso()}


@app.get("/api/tracks/stream")
async def api_tracks_stream(request: Request):
    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                payload = {"tracks": list_tracks(), "server_time": utc_now_iso()}
                yield f"event: tracks\ndata: {json.dumps(payload)}\n\n"
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.post("/api/tracks", dependencies=[Depends(require_write_access)])
def api_upsert(track: TrackIn):
    _validate_side_layer(track.side, track.layer)
    upsert_track(track.uid, track.side, track.layer, track.lat, track.lon, track.meta)
    return {"ok": True, "updated_at": utc_now_iso()}


@app.post("/ingest/bft", dependencies=[Depends(require_write_access)])
def ingest_bft(batch: BFTBatch):
    for t in batch.tracks:
        _validate_side_layer(t.side, t.layer)
        upsert_track(t.uid, t.side, t.layer, t.lat, t.lon, t.meta)
    return {"ok": True, "count": len(batch.tracks)}


@app.post("/tak/cot", dependencies=[Depends(require_write_access)])
async def ingest_cot(request: Request):
    raw = await request.body()
    try:
        root = etree.fromstring(raw)
        uid = root.get("uid") or root.get("id") or f"COT-{int(time.time())}"
        cot_type = root.get("type", "")
        side = "friendly" if cot_type.startswith("a-f") else "enemy" if cot_type.startswith("a-h") else "unknown"
        layer = "friendly" if side == "friendly" else "enemy" if side == "enemy" else "other"
        aff = "F" if side == "friendly" else "H" if side == "enemy" else "U"
        dim = "A" if cot_type.startswith(("a-f-A", "a-h-A")) else "G"
        sidc = f"S{aff}{dim}P------*****"
        pt = root.find(".//point")
        if pt is None:
            raise ValueError("No point element")
        lat = float(pt.get("lat"))
        lon = float(pt.get("lon"))
        meta = {
            "cot_type": cot_type,
            "how": root.get("how"),
            "time": root.get("time"),
            "start": root.get("start"),
            "stale": root.get("stale"),
            "sidc": sidc,
        }
        upsert_track(uid, side, layer, lat, lon, meta)
        return {"ok": True, "uid": uid}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid CoT: {e}")


@app.get("/tak/cot/pull")
def pull_cot():
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
        ev.set("stale", (now.replace(microsecond=0) + timedelta(seconds=60)).isoformat())
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


@app.get("/api/live_feed/status")
def api_live_feed_status():
    if live_feed_poller is None:
        return {"enabled": False, "reason": "LIVE_FEED_URL not configured"}
    return live_feed_poller.status()


@app.get("/api/zenoh/status")
def api_zenoh_status():
    return zenoh_bridge.status()


@app.get("/api/fpv/drones")
def api_fpv_drones():
    if not FPV_SIM_ENABLED:
        return {"enabled": False, "drones": []}
    now_ts = time.time()
    drones = _upsert_simulated_fpv_tracks(now_ts)
    return {"enabled": True, "generated_at": utc_now_iso(), "drones": drones}


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

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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

    return StreamingResponse(
        gen(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/video/pip", response_class=HTMLResponse)
def video_pip(src: str = "/video/mjpeg"):
    if not src.startswith("/video/"):
        src = "/video/mjpeg"
    safe_src = escape(src, quote=True)
    return HTMLResponse(
        content=f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>FMV PiP</title>
  <style>
    body {{ margin:0; background:#000; }}
    img {{ width:100vw; height:100vh; object-fit:contain; display:block; }}
  </style>
</head>
<body>
  <img src="{safe_src}" alt="FMV"/>
</body>
</html>
"""
    )


@app.get("/video/view", response_class=HTMLResponse)
def video_view(src: str = "/video/mjpeg"):
    return video_pip(src=src)
