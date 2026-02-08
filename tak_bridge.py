"""
Bidirectional TCP bridge to a TAK Server.

Receives Cursor-on-Target (CoT) events from the TAK Server stream and
upserts them into the local tracks database.  Periodically pushes local
tracks back to the TAK Server and sends a self-SA heartbeat.

The bridge is opt-in: it only starts when TAK_HOST is configured.
"""

import logging
import os
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from lxml import etree

log = logging.getLogger("tak_bridge")

SA_INTERVAL = 15  # seconds between self-SA heartbeats
MAX_BUF = 1 << 20  # 1 MB buffer limit before discard

# ---------------------------------------------------------------------------
# TAKBridge
# ---------------------------------------------------------------------------

class TAKBridge:
    def __init__(
        self,
        host: str,
        port: int,
        tls: bool = False,
        cert_path: str = "",
        key_path: str = "",
        ca_path: str = "",
        callsign: str = "COP-LITE",
        push_interval: int = 30,
        upsert_fn: Optional[Callable] = None,
        list_fn: Optional[Callable] = None,
    ):
        self.host = host
        self.port = port
        self.tls = tls
        self.cert_path = cert_path
        self.key_path = key_path
        self.ca_path = ca_path
        self.callsign = callsign
        self.push_interval = push_interval
        self._upsert_track = upsert_fn
        self._list_tracks = list_fn

        self._lock = threading.Lock()
        self._running = False
        self._connected = False
        self._sock: Optional[socket.socket] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._send_thread: Optional[threading.Thread] = None

        # status counters
        self._last_connected_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._reconnect_count = 0
        self._events_received = 0
        self._events_sent = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        if self._running:
            return
        self._running = True
        self._recv_thread = threading.Thread(
            target=self._recv_loop, daemon=True, name="tak-recv"
        )
        self._send_thread = threading.Thread(
            target=self._send_loop, daemon=True, name="tak-send"
        )
        self._recv_thread.start()
        self._send_thread.start()
        log.info("TAKBridge started -> %s:%s (TLS=%s)", self.host, self.port, self.tls)

    def stop(self):
        self._running = False
        self._close_socket()
        if self._recv_thread:
            self._recv_thread.join(timeout=3)
        if self._send_thread:
            self._send_thread.join(timeout=3)
        log.info("TAKBridge stopped")

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected,
                "host": self.host,
                "port": self.port,
                "tls": self.tls,
                "callsign": self.callsign,
                "last_connected_at": self._last_connected_at,
                "last_error": self._last_error,
                "reconnect_count": self._reconnect_count,
                "events_received": self._events_received,
                "events_sent": self._events_sent,
            }

    # -- connection ----------------------------------------------------------

    def _connect(self) -> socket.socket:
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(10)
        raw.connect((self.host, self.port))

        if self.tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            if self.ca_path:
                ctx.load_verify_locations(self.ca_path)
            else:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            if self.cert_path and self.key_path:
                ctx.load_cert_chain(certfile=self.cert_path, keyfile=self.key_path)
            raw = ctx.wrap_socket(raw, server_hostname=self.host)

        raw.settimeout(None)  # blocking for recv
        return raw

    def _close_socket(self):
        with self._lock:
            self._connected = False
            if self._sock:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    # -- receive loop --------------------------------------------------------

    def _recv_loop(self):
        backoff = 1
        while self._running:
            try:
                sock = self._connect()
                with self._lock:
                    self._sock = sock
                    self._connected = True
                    self._last_connected_at = datetime.now(timezone.utc).isoformat()
                backoff = 1
                log.info("Connected to TAK Server %s:%s", self.host, self.port)

                self._send_self_sa(sock)
                self._stream_recv(sock)

            except Exception as e:
                with self._lock:
                    self._connected = False
                    self._last_error = str(e)
                    self._reconnect_count += 1
                log.warning(
                    "TAK connection error: %s (reconnect in %ds)", e, backoff
                )

            self._close_socket()
            if not self._running:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _stream_recv(self, sock: socket.socket):
        buf = b""
        END_TAG = b"</event>"
        while self._running:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                log.info("TAK Server closed connection")
                return

            buf += chunk

            # safety: discard oversized buffer (malformed stream)
            if len(buf) > MAX_BUF:
                log.warning("Buffer exceeded %d bytes, discarding", MAX_BUF)
                buf = b""
                continue

            while END_TAG in buf:
                end_idx = buf.index(END_TAG) + len(END_TAG)
                raw_event = buf[:end_idx]
                buf = buf[end_idx:]

                start_idx = raw_event.find(b"<event")
                if start_idx < 0:
                    continue
                raw_event = raw_event[start_idx:]

                try:
                    root = etree.fromstring(raw_event)
                    self._handle_event(root)
                    with self._lock:
                        self._events_received += 1
                except etree.XMLSyntaxError as xe:
                    log.debug("Malformed CoT event, skipping: %s", xe)

    # -- event handling ------------------------------------------------------

    def _handle_event(self, root: etree._Element):
        uid = root.get("uid") or root.get("id")
        if not uid:
            return
        # skip our own heartbeat
        if uid == self.callsign:
            return

        cot_type = root.get("type", "")

        if cot_type.startswith("a-f"):
            side = "friendly"
        elif cot_type.startswith("a-h"):
            side = "enemy"
        elif cot_type.startswith("a-n"):
            side = "neutral"
        else:
            side = "unknown"

        if side == "friendly":
            layer = "friendly"
        elif side == "enemy":
            layer = "enemy"
        else:
            layer = "other"

        # derive SIDC
        aff = {"friendly": "F", "enemy": "H", "neutral": "N"}.get(side, "U")
        dim = "A" if len(cot_type) > 4 and cot_type[4:5] == "A" else "G"
        sidc = f"S{aff}{dim}P------*****"

        pt = root.find(".//point")
        if pt is None:
            return
        try:
            lat = float(pt.get("lat"))
            lon = float(pt.get("lon"))
        except (TypeError, ValueError):
            return

        # extract callsign from <detail><contact callsign="..."/>
        callsign = uid
        contact = root.find(".//detail/contact")
        if contact is not None and contact.get("callsign"):
            callsign = contact.get("callsign")

        meta = {
            "cot_type": cot_type,
            "how": root.get("how"),
            "time": root.get("time"),
            "start": root.get("start"),
            "stale": root.get("stale"),
            "sidc": sidc,
            "callsign": callsign,
            "source": "tak_server",
        }

        if self._upsert_track:
            self._upsert_track(uid, side, layer, lat, lon, meta)

    # -- send loop -----------------------------------------------------------

    def _send_loop(self):
        last_sa = 0.0
        last_push = 0.0
        while self._running:
            time.sleep(1)

            with self._lock:
                sock = self._sock
                connected = self._connected
            if not connected or sock is None:
                continue

            now = time.monotonic()

            if now - last_sa >= SA_INTERVAL:
                try:
                    self._send_self_sa(sock)
                    last_sa = now
                except Exception as e:
                    log.debug("Error sending self-SA: %s", e)

            if now - last_push >= self.push_interval:
                try:
                    self._push_local_tracks(sock)
                    last_push = now
                except Exception as e:
                    log.debug("Error pushing tracks: %s", e)

    # -- self-SA heartbeat ---------------------------------------------------

    def _send_self_sa(self, sock: socket.socket):
        now = datetime.now(timezone.utc)
        stale = now + timedelta(seconds=30)
        fmt = "%Y-%m-%dT%H:%M:%SZ"

        ev = etree.Element("event")
        ev.set("version", "2.0")
        ev.set("uid", self.callsign)
        ev.set("type", "a-f-G-U-C")
        ev.set("how", "h-g-i-g-o")
        ev.set("time", now.strftime(fmt))
        ev.set("start", now.strftime(fmt))
        ev.set("stale", stale.strftime(fmt))

        pt = etree.SubElement(ev, "point")
        pt.set("lat", "0.0")
        pt.set("lon", "0.0")
        pt.set("hae", "0")
        pt.set("ce", "9999999")
        pt.set("le", "9999999")

        detail = etree.SubElement(ev, "detail")
        contact = etree.SubElement(detail, "contact")
        contact.set("callsign", self.callsign)

        group = etree.SubElement(detail, "__group")
        group.set("name", "Cyan")
        group.set("role", "HQ")

        takv = etree.SubElement(detail, "takv")
        takv.set("os", "COP-Lite")
        takv.set("version", "1.0.0")
        takv.set("device", "server")
        takv.set("platform", "Tactical COP Lite")

        sock.sendall(etree.tostring(ev, xml_declaration=True, encoding="UTF-8"))

    # -- push local tracks ---------------------------------------------------

    def _push_local_tracks(self, sock: socket.socket):
        if not self._list_tracks:
            return
        tracks = self._list_tracks()
        if not tracks:
            return

        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        stale_ts = (now + timedelta(seconds=self.push_interval + 15)).strftime(fmt)
        ts = now.strftime(fmt)
        sent = 0

        for t in tracks:
            # skip tracks that came from TAK Server (prevent echo loop)
            if t.get("meta", {}).get("source") == "tak_server":
                continue

            side = t["side"]
            if side == "friendly":
                default_type = "a-f-G-U-C"
            elif side == "enemy":
                default_type = "a-h-G-U-C"
            elif side == "neutral":
                default_type = "a-n-G-U-C"
            else:
                default_type = "a-u-G-U-C"

            ev = etree.Element("event")
            ev.set("version", "2.0")
            ev.set("uid", t["uid"])
            ev.set("type", t.get("meta", {}).get("cot_type", default_type))
            ev.set("how", "m-g")
            ev.set("time", ts)
            ev.set("start", ts)
            ev.set("stale", stale_ts)

            pt = etree.SubElement(ev, "point")
            pt.set("lat", str(t["lat"]))
            pt.set("lon", str(t["lon"]))
            pt.set("hae", "0")
            pt.set("ce", "25")
            pt.set("le", "25")

            detail = etree.SubElement(ev, "detail")
            contact = etree.SubElement(detail, "contact")
            contact.set("callsign", t.get("meta", {}).get("callsign", t["uid"]))

            sock.sendall(etree.tostring(ev, xml_declaration=True, encoding="UTF-8"))
            sent += 1

        if sent:
            with self._lock:
                self._events_sent += sent
