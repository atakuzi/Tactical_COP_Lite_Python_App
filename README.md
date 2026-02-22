# Tactical COP Lite (Python)

Lightweight tactical common operating picture (COP) application built with FastAPI + Mapbox GL JS.

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white) ![JavaScript](https://img.shields.io/badge/JavaScript-ES6%2B-F7DF1E?logo=javascript&logoColor=black) ![HTML5](https://img.shields.io/badge/HTML5-Markup-E34F26?logo=html5&logoColor=white) ![CSS3](https://img.shields.io/badge/CSS3-Styling-1572B6?logo=css3&logoColor=white)

## What It Does
- Displays live tracks on an interactive map with MIL-STD-2525/APP-6 style symbols (`milsymbol`).
- Supports layer filtering (`friendly`, `enemy`, `fires`, `air`, `ew`, `other`).
- Persists tracks in SQLite (`cop.db` by default) with last-known-position behavior.
- Marks stale tracks in the UI (stale threshold is currently 90 seconds in `static/app.js`).
- Provides CoT ingest/export and optional TAK Server TCP bridge sync.
- Uses zenoh as the core pub/sub layer for track updates.
- Streams FMV as MJPEG (`/video/mjpeg`) from RTSP or a generated test feed.
- Includes simulated FPV drone streams with telemetry overlays and COP track integration.
- Uses Mapbox GL JS for the map display.

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

If `pip install -r requirements.txt` fails on `zenoh` (common on Windows/Python 3.14):
```powershell
# 1) Install Python 3.12 and create a dedicated venv
py -3.12 -m venv .venv312
.\.venv312\Scripts\Activate.ps1

# 2) Install app deps except zenoh
Get-Content requirements.txt | Where-Object { $_ -notmatch '^zenoh' } | Set-Content requirements_no_zenoh.txt
pip install -r requirements_no_zenoh.txt

# 3) Install Rust + MSVC build tools, then build zenoh Python client from source
winget install -e --id Rustlang.Rustup
winget install -e --id Microsoft.VisualStudio.2022.BuildTools --override "--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
pip install git+https://github.com/eclipse-zenoh/zenoh-python.git
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

Zenoh router only (run app locally):
```bash
docker compose up -d zenoh
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
- `GET /api/adsb/status`: ADS-B poller status/counters.
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
| `MAPBOX_ACCESS_TOKEN` | _(empty)_ | Mapbox public access token used by frontend map |
| `MAPBOX_STYLE` | `mapbox://styles/mapbox/dark-v11` | Mapbox style URL for the map |
| `RTSP_URL` | _(empty)_ | RTSP source; when empty, app serves generated FMV test feed |
| `FPV_SIM_ENABLED` | `true` | Enable simulated FPV drones and streams |
| `LIVE_FEED_URL` | _(empty = disabled)_ | HTTP(S) JSON endpoint polled for live tracks |
| `LIVE_FEED_INTERVAL` | `5` | Poll interval (seconds) for `LIVE_FEED_URL` |
| `LIVE_FEED_TIMEOUT_S` | `8` | HTTP timeout (seconds) for external live feed |
| `ADSB_FEED_URL` | _(empty = disabled)_ | ADS-B JSON endpoint URL (OpenSky, ADS-B Exchange-like, or dump1090-like) |
| `ADSB_FEED_INTERVAL` | `5` | Poll interval (seconds) for `ADSB_FEED_URL` |
| `ADSB_FEED_TIMEOUT_S` | `8` | HTTP timeout (seconds) for ADS-B API requests |
| `ADSB_API_KEY` | _(empty)_ | Optional API key sent as `X-API-Key` for ADS-B endpoints |

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
$env:MAPBOX_ACCESS_TOKEN="pk.your_public_token_here"
$env:RTSP_URL="rtsp://user:pass@ip/stream"
$env:LIVE_FEED_URL="http://127.0.0.1:9000/live_tracks"
$env:LIVE_FEED_INTERVAL="5"
$env:ADSB_FEED_URL="https://opensky-network.org/api/states/all"
$env:ADSB_FEED_INTERVAL="5"
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

ADS-B feed payload shapes supported:
```json
{
  "time": 1700000000,
  "states": [
    ["3c6444", "DLH2AB  ", "Germany", 1700000000, 1700000000, 8.6821, 50.1109, 10668.0, false, 230.0, 90.0]
  ]
}
```

```json
{
  "aircraft": [
    { "hex": "a8c123", "flight": "UAL123", "lat": 37.6213, "lon": -122.3790, "track": 265.4, "gs": 410.2, "alt_baro": 32000 }
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
- On Windows with Python 3.14, `pip install zenoh` may not have a compatible wheel yet.
  Use Python 3.12 (recommended) if zenoh install/import fails.
- If your package index has no prebuilt zenoh wheel, use source install:
  `pip install git+https://github.com/eclipse-zenoh/zenoh-python.git`

## Track Display Check
- Start zenoh router: `docker compose up -d zenoh`
- Start app: `uvicorn main:app --host 127.0.0.1 --port 8000`
- Verify bridge health: `GET /api/zenoh/status` should show `"ready": true`
- Inject a sample track:
  ```bash
  curl -X POST http://127.0.0.1:8000/api/tracks \
    -H "Content-Type: application/json" \
    -d '{"uid":"DEMO-TRACK-001","side":"unknown","layer":"air","lat":37.6188,"lon":-122.3754,"meta":{"callsign":"DEMO123"}}'
  ```
- Confirm ingestion: `GET /api/tracks` includes `DEMO-TRACK-001` and the map renders it.

## FMV Notes
- Browsers do not natively play RTSP directly.
- App converts RTSP -> MJPEG for browser playback.
- Without `RTSP_URL`, it emits a generated test pattern so UI can be demoed offline.

## Map Notes
- Map rendering prefers Mapbox GL JS, with `maplibre-gl` as runtime fallback.
- If WebGL engines cannot initialize, the UI falls back to Leaflet (2D) with OSM tiles.
- If both WebGL and Leaflet are unavailable, the UI falls back to an embedded OpenStreetMap iframe.
- If `MAPBOX_ACCESS_TOKEN` is set, the app attempts `MAPBOX_STYLE`.
- If token is missing, or Mapbox style load fails, the app falls back to OpenStreetMap raster tiles.

## Production Checklist
- Set `COP_API_KEY` (protects write endpoints).
- Set `TRUSTED_HOSTS` to your domain(s) and edge hostnames.
- Set `CORS_ORIGINS` only if you need browser access from other origins.
- Disable docs in production (`ENABLE_DOCS=false`).
- Ensure zenoh router/service is reachable at `ZENOH_CONNECT` before starting the app.
- Put the app behind TLS termination (reverse proxy or ingress).
- Monitor `GET /healthz` and `GET /readyz`.



