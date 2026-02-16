# Tactical COP Lite (Python)

Lightweight tactical common operating picture (COP) application built with FastAPI + Leaflet.

## What It Does
- Displays live tracks on a Leaflet map with MIL-STD-2525/APP-6 style symbols (`milsymbol`).
- Supports layer filtering (`friendly`, `enemy`, `fires`, `air`, `ew`, `other`).
- Persists tracks in SQLite (`cop.db` by default) with last-known-position behavior.
- Marks stale tracks in the UI (stale threshold is currently 90 seconds in `static/app.js`).
- Provides CoT ingest/export and optional TAK Server TCP bridge sync.
- Uses zenoh as the core pub/sub layer for track updates.
- Streams FMV as MJPEG (`/video/mjpeg`) from RTSP or a generated test feed.
- Includes simulated FPV drone streams with telemetry overlays and COP track integration.

## Runtime + Dependencies
- Python 3.10+ (3.12 recommended for production)
- FastAPI / Uvicorn / Gunicorn
- OpenCV + NumPy (for FMV frame generation/transcoding)
- lxml (CoT XML parsing/serialization)

Install:
```bash
python -m venv .venv
```

Windows (PowerShell):
```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

macOS/Linux:
```bash
source .venv/bin/activate
pip install -r requirements.txt
```

## Run Modes
Development:
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Production (Linux/container):
```bash
gunicorn -c gunicorn_conf.py main:app
```

Docker:
```bash
docker build -t tactical-cop-lite .
docker run --rm -p 8000:8000 -v $(pwd)/data:/data --env-file .env tactical-cop-lite
```

Docker Compose (app + zenoh core service):
```bash
docker compose up --build
```

Production config template:
```bash
cp .env.example .env
```

Open:
- `http://localhost:8000`

Health checks:
- `GET /healthz`
- `GET /readyz` (requires DB and zenoh ready)

## API
- `GET /api/tracks`: list all tracks + server UTC time.
- `GET /api/tracks/stream`: Server-Sent Events (SSE) stream with live track snapshots (`event: tracks`).
- `POST /api/tracks`: upsert one track (validates `side`, `layer`, lat/lon, and meta size).
- `POST /ingest/bft`: ingest batch JSON (`{"tracks":[...]}`).
- `POST /tak/cot`: ingest one CoT XML event.
- `GET /tak/cot/pull`: export all tracks as CoT XML events.
- `GET /api/tak/status`: TAK bridge status/counters.
- `GET /api/live_feed/status`: external live-feed poller status/counters.
- `GET /api/zenoh/status`: zenoh bridge status/counters.
- `GET /video/mjpeg`: MJPEG stream endpoint.
- `GET /api/fpv/drones`: returns simulated FPV drone list and stream URLs, and updates drone tracks in COP.
- `GET /video/fpv/{drone_uid}.mjpeg`: simulated per-drone FPV MJPEG stream.
- `GET /video/pip`: minimal PiP HTML page.
- `GET /video/view`: alias of `/video/pip`.

Example track upsert:
```bash
curl -X POST http://localhost:8000/api/tracks \
  -H "Content-Type: application/json" \
  -H "X-API-Key: change-me" \
  -d '{"uid":"FRD-001","side":"friendly","layer":"friendly","lat":50.1109,"lon":8.6821,"meta":{"callsign":"ALPHA 1"}}'
```

Note: `X-API-Key` is only required if `COP_API_KEY` is set.

## Environment Variables

Core:

| Variable | Default | Purpose |
|---|---|---|
| `COP_DB_PATH` | `cop.db` | SQLite DB file path |
| `COP_LOG_LEVEL` | `INFO` | App log level |
| `COP_API_KEY` | _(empty)_ | Optional API key required for write endpoints |
| `ENABLE_DOCS` | `true` | Enable or disable `/docs`, `/redoc`, and OpenAPI |
| `TRUSTED_HOSTS` | _(empty)_ | Comma-separated allowed hosts (enables host-header protection) |
| `CORS_ORIGINS` | _(empty)_ | Comma-separated allowed CORS origins |
| `MAX_META_BYTES` | `8192` | Max serialized `meta` payload size per track |
| `RTSP_URL` | _(empty)_ | RTSP source; when empty, app serves generated FMV test feed |
| `FPV_SIM_ENABLED` | `true` | Enable simulated FPV drones and streams |
| `LIVE_FEED_URL` | _(empty = disabled)_ | HTTP(S) JSON endpoint polled for live tracks |
| `LIVE_FEED_INTERVAL` | `5` | Poll interval (seconds) for `LIVE_FEED_URL` |
| `LIVE_FEED_TIMEOUT_S` | `8` | HTTP timeout (seconds) for external live feed |

TAK bridge (enabled when `TAK_HOST` is set):

| Variable | Default | Purpose |
|---|---|---|
| `TAK_HOST` | _(empty = disabled)_ | TAK Server host/IP |
| `TAK_PORT` | `8087` | TAK TCP port (`8087` plain, often `8089` TLS) |
| `TAK_TLS` | `false` | Enable TLS |
| `TAK_TLS_INSECURE_SKIP_VERIFY` | `false` | Disable TLS certificate verification (not recommended) |
| `TAK_CERT` | _(empty)_ | Client certificate path (mTLS) |
| `TAK_KEY` | _(empty)_ | Client key path (mTLS) |
| `TAK_CA` | _(empty)_ | CA certificate path |
| `TAK_CALLSIGN` | `COP-LITE` | Self-SA callsign sent by bridge |
| `TAK_PUSH_INTERVAL` | `30` | Seconds between local track pushes to TAK |

zenoh bridge (core service):

| Variable | Default | Purpose |
|---|---|---|
| `ZENOH_CONNECT` | `tcp/127.0.0.1:7447` | Comma-separated zenoh endpoints |
| `ZENOH_PUB_KEYEXPR` | `cop/tracks` | Key expression used for publishing track updates |
| `ZENOH_SUB_KEYEXPR` | `cop/tracks` | Key expression subscribed for incoming track updates |
| `ZENOH_PUBLISH` | `true` | Publish local updates to zenoh |
| `ZENOH_SUBSCRIBE` | `true` | Subscribe to remote updates from zenoh |

PowerShell examples:
```powershell
$env:COP_API_KEY="change-me"
$env:TRUSTED_HOSTS="localhost,127.0.0.1"
$env:ENABLE_DOCS="false"
$env:RTSP_URL="rtsp://user:pass@ip/stream"
$env:LIVE_FEED_URL="http://127.0.0.1:9000/live_tracks"
$env:LIVE_FEED_INTERVAL="5"
$env:TAK_HOST="192.168.1.100"
$env:TAK_PORT="8087"
$env:TAK_CALLSIGN="MY-COP"
$env:ZENOH_CONNECT="tcp/127.0.0.1:7447"
```

External live feed payload shape:
```json
{
  "tracks": [
    {
      "uid": "LIVE-001",
      "side": "friendly",
      "layer": "air",
      "lat": 50.12,
      "lon": 8.67,
      "meta": { "callsign": "EAGLE 1" }
    }
  ]
}
```

## TAK Bridge Notes
- Bridge starts automatically on app startup only if `TAK_HOST` is configured.
- Receives CoT from TAK stream and upserts local tracks.
- Pushes local tracks back to TAK on interval.
- Sends periodic self-SA heartbeat.
- Avoids echo loops by not re-pushing tracks marked with `meta.source == "tak_server"`.

## Zenoh Bridge Notes
- Bridge starts automatically on app startup.
- If zenoh cannot initialize, app startup fails (fail-fast).
- Publishes local track updates to `ZENOH_PUB_KEYEXPR`.
- Subscribes for incoming updates on `ZENOH_SUB_KEYEXPR`.
- Incoming zenoh updates are tagged with `meta.source == "zenoh"` and are not re-published.

## FMV Notes
- Browsers do not natively play RTSP directly.
- App converts RTSP -> MJPEG for browser playback.
- Without `RTSP_URL`, it emits a generated test pattern so UI can be demoed offline.

## Production Checklist
- Set `COP_API_KEY` (protects write endpoints).
- Set `TRUSTED_HOSTS` to your domain(s) and edge hostnames.
- Set `CORS_ORIGINS` only if you need browser access from other origins.
- Disable docs in production (`ENABLE_DOCS=false`).
- Ensure zenoh router/service is reachable at `ZENOH_CONNECT` before starting the app.
- Put the app behind TLS termination (reverse proxy or ingress).
- Monitor `GET /healthz` and `GET /readyz`.
