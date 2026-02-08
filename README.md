# Tactical COP Lite (Python)

A lightweight **Tactical COP Lite** demo built with **FastAPI + Leaflet**.

## Capabilities
- Live **friendly / enemy** overlays (Leaflet map)
- Simple **layer toggles** (friendly, enemy, fires, air, EW)
- **Last-known-position persistence** with **stale highlighting** when updates stop
- **SQLite** storage
- **MIL-STD-2525 / APP-6** symbology via milsymbol (standardized NATO icons)
- Basic integrations:
  - **TAK CoT ingest** (`POST /tak/cot`) – accepts a Cursor-on-Target XML event
  - **BFT ingest** (`POST /ingest/bft`) – accepts JSON track updates
  - **TAK Server TCP bridge** – persistent bidirectional sync with a TAK Server
- **Picture-in-picture FMV** via **MJPEG** endpoint (`/video/mjpeg`)
  - If `RTSP_URL` is set, it will attempt to pull from RTSP using OpenCV.
  - If not set, it serves a generated test feed so you can demo the UI.

> Note: Browsers don't natively play RTSP. This app converts RTSP -> MJPEG for simple viewing.
> For production-grade FMV (SRT/WebRTC), you'd add a media server (e.g., Janus/GStreamer/WebRTC gateway).

## Quick start
1. Create a venv and install deps:
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
```

2. Run:
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

3. Open:
- http://localhost:8000

## Optional: connect an RTSP feed
Set an environment variable:
```bash
export RTSP_URL="rtsp://user:pass@ip/stream"
```

## API essentials
- `GET /api/tracks` -> all tracks
- `POST /api/tracks` -> upsert a track (JSON)
- `POST /tak/cot` -> ingest CoT XML
- `POST /ingest/bft` -> ingest BFT-like JSON list
- `GET /video/mjpeg` -> MJPEG stream for FMV panel
- `GET /api/tak/status` -> TAK bridge connection status

## Example: post a track
```bash
curl -X POST http://localhost:8000/api/tracks \
  -H "Content-Type: application/json" \
  -d '{"uid":"FRD-001","side":"friendly","lat":50.1109,"lon":8.6821,"layer":"friendly","meta":{"callsign":"ALPHA 1"}}'
```

## Optional: connect to a TAK Server

The app includes a persistent TCP bridge that auto-syncs tracks with a TAK Server.
Set `TAK_HOST` to enable it.

**Plaintext (port 8087):**
```bash
export TAK_HOST="192.168.1.100"
export TAK_PORT="8087"
export TAK_CALLSIGN="MY-COP"
```

**TLS / mTLS (port 8089):**
```bash
export TAK_HOST="tak.example.com"
export TAK_PORT="8089"
export TAK_TLS="true"
export TAK_CERT="/path/to/client.pem"
export TAK_KEY="/path/to/client.key"
export TAK_CA="/path/to/ca.pem"
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `TAK_HOST` | _(empty = disabled)_ | TAK Server hostname/IP |
| `TAK_PORT` | `8087` | TCP port (8087=plain, 8089=TLS) |
| `TAK_TLS` | `false` | Enable TLS |
| `TAK_CERT` | | Client cert path (mTLS) |
| `TAK_KEY` | | Client key path |
| `TAK_CA` | | CA cert path |
| `TAK_CALLSIGN` | `COP-LITE` | Self-SA identity on the TAK network |
| `TAK_PUSH_INTERVAL` | `30` | Seconds between pushing local tracks to TAK |

Check connection status: `GET /api/tak/status`

## Security note
This is a demo. For real deployments, add:
- AuthN/AuthZ (OIDC), network ZT controls
- Signed data feeds, rate limits
- Input validation + schema enforcement
- Auditing/logging, at-rest encryption, etc.