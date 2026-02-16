# Tactical COP Lite (Python)

Lightweight tactical common operating picture (COP) demo built with FastAPI + Leaflet.

## What It Does
- Displays live tracks on a Leaflet map with MIL-STD-2525/APP-6 style symbols (`milsymbol`).
- Supports layer filtering (`friendly`, `enemy`, `fires`, `air`, `ew`, `other`).
- Persists tracks in SQLite (`cop.db` by default) with last-known-position behavior.
- Marks stale tracks in the UI (stale threshold is currently 90 seconds in `static/app.js`).
- Provides CoT ingest/export and optional TAK Server TCP bridge sync.
- Streams FMV as MJPEG (`/video/mjpeg`) from RTSP or a generated test feed.
- Includes simulated FPV drone streams with telemetry overlays and COP track integration.

## Runtime + Dependencies
- Python 3.10+ recommended
- FastAPI / Uvicorn
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

Run:
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open:
- `http://localhost:8000`

## API
- `GET /api/tracks`: list all tracks + server UTC time.
- `POST /api/tracks`: upsert one track (validates `side` and `layer`).
- `POST /ingest/bft`: ingest batch JSON (`{"tracks":[...]}`).
- `POST /tak/cot`: ingest one CoT XML event.
- `GET /tak/cot/pull`: export all tracks as CoT XML events.
- `GET /api/tak/status`: TAK bridge status/counters.
- `GET /video/mjpeg`: MJPEG stream endpoint.
- `GET /api/fpv/drones`: returns simulated FPV drone list and stream URLs, and updates drone tracks in COP.
- `GET /video/fpv/{drone_uid}.mjpeg`: simulated per-drone FPV MJPEG stream.
- `GET /video/pip`: minimal PiP HTML page.
- `GET /video/view`: alias of `/video/pip`.

Example track upsert:
```bash
curl -X POST http://localhost:8000/api/tracks \
  -H "Content-Type: application/json" \
  -d '{"uid":"FRD-001","side":"friendly","layer":"friendly","lat":50.1109,"lon":8.6821,"meta":{"callsign":"ALPHA 1"}}'
```

## Environment Variables

Core:

| Variable | Default | Purpose |
|---|---|---|
| `COP_DB_PATH` | `cop.db` | SQLite DB file path |
| `RTSP_URL` | _(empty)_ | RTSP source; when empty, app serves generated FMV test feed |
| `FPV_SIM_ENABLED` | `true` | Enable simulated FPV drones and streams |

TAK bridge (enabled when `TAK_HOST` is set):

| Variable | Default | Purpose |
|---|---|---|
| `TAK_HOST` | _(empty = disabled)_ | TAK Server host/IP |
| `TAK_PORT` | `8087` | TAK TCP port (`8087` plain, often `8089` TLS) |
| `TAK_TLS` | `false` | Enable TLS |
| `TAK_CERT` | _(empty)_ | Client certificate path (mTLS) |
| `TAK_KEY` | _(empty)_ | Client key path (mTLS) |
| `TAK_CA` | _(empty)_ | CA certificate path |
| `TAK_CALLSIGN` | `COP-LITE` | Self-SA callsign sent by bridge |
| `TAK_PUSH_INTERVAL` | `30` | Seconds between local track pushes to TAK |

PowerShell examples:
```powershell
$env:RTSP_URL="rtsp://user:pass@ip/stream"
$env:TAK_HOST="192.168.1.100"
$env:TAK_PORT="8087"
$env:TAK_CALLSIGN="MY-COP"
```

## TAK Bridge Notes
- Bridge starts automatically on app startup only if `TAK_HOST` is configured.
- Receives CoT from TAK stream and upserts local tracks.
- Pushes local tracks back to TAK on interval.
- Sends periodic self-SA heartbeat.
- Avoids echo loops by not re-pushing tracks marked with `meta.source == "tak_server"`.

## FMV Notes
- Browsers do not natively play RTSP directly.
- App converts RTSP -> MJPEG for simple browser display.
- Without `RTSP_URL`, it emits a generated test pattern so UI can be demoed offline.

## Security
This is a demo baseline. For production use, add:
- Authentication/authorization
- Input rate limiting and stricter schema validation
- Transport security hardening and credential management
- Audit logging and data protection controls
