"""
Zenoh pub/sub bridge for track updates.

Publishes local upserts to zenoh and subscribes to remote updates, then
upserts those into the local DB.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("zenoh_bridge")


class ZenohBridge:
    def __init__(
        self,
        pub_keyexpr: str,
        sub_keyexpr: str,
        connect_endpoints: Optional[List[str]] = None,
        enable_publish: bool = True,
        enable_subscribe: bool = True,
        upsert_fn: Optional[Callable] = None,
    ):
        self.pub_keyexpr = pub_keyexpr
        self.sub_keyexpr = sub_keyexpr
        self.connect_endpoints = connect_endpoints or []
        self.enable_publish = enable_publish
        self.enable_subscribe = enable_subscribe
        self._upsert_track = upsert_fn

        self._lock = threading.Lock()
        self._running = False
        self._ready = False
        self._session = None
        self._publisher = None
        self._subscriber = None
        self._zenoh = None
        self._last_error: Optional[str] = None
        self._last_connected_at: Optional[str] = None
        self._published_total = 0
        self._received_total = 0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            import zenoh  # type: ignore

            self._zenoh = zenoh
            config = zenoh.Config()
            if self.connect_endpoints:
                try:
                    endpoints_json = json.dumps(self.connect_endpoints)
                    if hasattr(config, "insert_json5"):
                        config.insert_json5("connect/endpoints", endpoints_json)
                except Exception:
                    pass

            self._session = zenoh.open(config)

            if self.enable_publish:
                self._publisher = self._session.declare_publisher(self.pub_keyexpr)
            if self.enable_subscribe:
                self._subscriber = self._session.declare_subscriber(self.sub_keyexpr, self._on_sample)

            with self._lock:
                self._ready = True
                self._last_connected_at = datetime.now(timezone.utc).isoformat()
                self._last_error = None

            log.info(
                "ZenohBridge started (pub=%s sub=%s) pub_keyexpr=%s sub_keyexpr=%s",
                self.enable_publish,
                self.enable_subscribe,
                self.pub_keyexpr,
                self.sub_keyexpr,
            )
        except Exception as e:
            with self._lock:
                self._running = False
                self._ready = False
                self._last_error = str(e)
            log.error("ZenohBridge failed to start: %s", e)
            raise RuntimeError(f"ZenohBridge failed to start: {e}") from e

    def stop(self) -> None:
        self._running = False
        with self._lock:
            self._ready = False
        try:
            if self._subscriber is not None and hasattr(self._subscriber, "undeclare"):
                self._subscriber.undeclare()
        except Exception:
            pass
        try:
            if self._publisher is not None and hasattr(self._publisher, "undeclare"):
                self._publisher.undeclare()
        except Exception:
            pass
        try:
            if self._session is not None:
                if hasattr(self._session, "close"):
                    self._session.close()
                elif hasattr(self._session, "undeclare"):
                    self._session.undeclare()
        except Exception:
            pass
        self._session = None
        self._publisher = None
        self._subscriber = None

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "running": self._running,
                "ready": self._ready,
                "publish_enabled": self.enable_publish,
                "subscribe_enabled": self.enable_subscribe,
                "pub_keyexpr": self.pub_keyexpr,
                "sub_keyexpr": self.sub_keyexpr,
                "connect_endpoints": self.connect_endpoints,
                "last_connected_at": self._last_connected_at,
                "last_error": self._last_error,
                "published_total": self._published_total,
                "received_total": self._received_total,
            }

    def publish_track(self, track: Dict[str, Any]) -> None:
        if not self.enable_publish:
            return
        if not self._running or not self._ready or self._publisher is None:
            return
        try:
            payload = json.dumps(track, separators=(",", ":")).encode("utf-8")
            self._publisher.put(payload)
            with self._lock:
                self._published_total += 1
                self._last_error = None
        except Exception as e:
            with self._lock:
                self._last_error = str(e)
            log.debug("Zenoh publish error: %s", e)

    def _sample_to_bytes(self, sample: Any) -> bytes:
        payload = getattr(sample, "payload", sample)
        if hasattr(payload, "to_bytes"):
            return payload.to_bytes()
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload)
        return str(payload).encode("utf-8")

    def _on_sample(self, sample: Any) -> None:
        if not self._upsert_track:
            return
        try:
            raw = self._sample_to_bytes(sample)
            obj = json.loads(raw.decode("utf-8"))
            uid = str(obj["uid"]).strip()
            side = str(obj["side"]).strip()
            layer = str(obj["layer"]).strip()
            lat = float(obj["lat"])
            lon = float(obj["lon"])
            meta = dict(obj.get("meta") or {})
            meta["source"] = "zenoh"
            self._upsert_track(uid, side, layer, lat, lon, meta, publish=False)
            with self._lock:
                self._received_total += 1
                self._last_error = None
        except Exception as e:
            with self._lock:
                self._last_error = str(e)
            log.debug("Zenoh consume error: %s", e)
