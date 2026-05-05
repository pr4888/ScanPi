"""MQTT publisher for ScanPi alerts.

Default broker: mqtt://localhost:1883 (override with SCANPI_MQTT_URL).

If `paho-mqtt` isn't installed OR the broker is unreachable, the publisher
becomes a logging no-op and retries connecting every 30s in a background
thread. Either way, alerts continue to land in alerts.db.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt  # type: ignore
    _MQTT_AVAILABLE = True
except ImportError:  # pragma: no cover
    mqtt = None  # type: ignore
    _MQTT_AVAILABLE = False


DEFAULT_URL = "mqtt://localhost:1883"
RETRY_INTERVAL_S = 30.0
TOPIC_ROOT = "scanpi/alerts"


@dataclass
class MQTTConfig:
    url: str = DEFAULT_URL
    client_id: str = "scanpi-alerts"
    keepalive: int = 60

    @classmethod
    def from_env(cls) -> "MQTTConfig":
        return cls(url=os.environ.get("SCANPI_MQTT_URL", DEFAULT_URL))

    def parse(self) -> tuple[str, int, str | None, str | None]:
        u = urlparse(self.url)
        host = u.hostname or "localhost"
        port = u.port or 1883
        return host, port, u.username, u.password


def _topic_for(severity: str, source: str, suffix: str | None = None) -> str:
    base = f"{TOPIC_ROOT}/{severity.lower()}/{source.lower()}"
    if suffix:
        return f"{base}/{suffix.strip('/')}"
    return base


class MQTTPublisher:
    """Best-effort MQTT publisher.

    Public API:
        start() / stop()       -- lifecycle
        publish_alert(payload, topic_suffix=None) -- non-blocking publish
        is_connected           -- bool
        last_error             -- str | None
    """

    def __init__(self, cfg: Optional[MQTTConfig] = None):
        self.cfg = cfg or MQTTConfig.from_env()
        self._client = None
        self._connected = False
        self._stop = threading.Event()
        self._connect_thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def available(self) -> bool:
        return _MQTT_AVAILABLE

    # --- lifecycle ----------------------------------------------------

    def start(self):
        if not _MQTT_AVAILABLE:
            log.warning(
                "paho-mqtt not installed — alerts will land in alerts.db only "
                "(no MQTT publish). Install with: pip install paho-mqtt"
            )
            self._last_error = "paho-mqtt not installed"
            return
        self._stop.clear()
        self._connect_thread = threading.Thread(
            target=self._connect_loop, daemon=True, name="alerts-mqtt"
        )
        self._connect_thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                log.debug("mqtt disconnect raised", exc_info=True)
        self._connected = False

    # --- internals ----------------------------------------------------

    def _connect_loop(self):
        host, port, user, pw = self.cfg.parse()
        while not self._stop.is_set():
            try:
                client = mqtt.Client(
                    client_id=self.cfg.client_id,
                    clean_session=True,
                )
                if user:
                    client.username_pw_set(user, pw or "")
                client.on_connect = self._on_connect
                client.on_disconnect = self._on_disconnect
                client.connect(host, port, keepalive=self.cfg.keepalive)
                client.loop_start()
                with self._lock:
                    self._client = client
                # Wait until we're disconnected or shut down.
                while not self._stop.is_set() and self._connected:
                    time.sleep(1.0)
                if self._stop.is_set():
                    return
                # Disconnected — fall through to retry.
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                self._connected = False
                with self._lock:
                    self._client = None
                log.warning("mqtt connect to %s:%d failed: %s — retrying in %ds",
                            host, port, e, int(RETRY_INTERVAL_S))
            # Sleep with periodic stop check.
            for _ in range(int(RETRY_INTERVAL_S)):
                if self._stop.is_set():
                    return
                time.sleep(1.0)

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            self._last_error = None
            log.info("mqtt connected to %s", self.cfg.url)
        else:
            self._connected = False
            self._last_error = f"connect rc={rc}"
            log.warning("mqtt connect failed rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            self._last_error = f"disconnect rc={rc}"
            log.warning("mqtt disconnected rc=%s", rc)

    # --- publish ------------------------------------------------------

    def publish_alert(self, payload: dict, topic_suffix: str | None = None) -> bool:
        """Publish an alert. Returns True if the publish was queued."""
        if not _MQTT_AVAILABLE:
            return False
        with self._lock:
            client = self._client
        if client is None or not self._connected:
            log.debug("mqtt not connected; skipping publish (alert still in DB)")
            return False
        severity = str(payload.get("severity", "low"))
        source = str(payload.get("source", "scanpi"))
        topic = _topic_for(severity, source, topic_suffix)
        try:
            body = json.dumps(payload, ensure_ascii=False, default=str)
            res = client.publish(topic, body, qos=1, retain=False)
            # paho.MQTTMessageInfo.rc — 0 == success.
            ok = getattr(res, "rc", 0) == 0
            if not ok:
                self._last_error = f"publish rc={getattr(res, 'rc', '?')}"
            return ok
        except Exception as e:
            self._last_error = f"publish error: {e}"
            log.exception("mqtt publish failed")
            return False
