"""Outbound webhook notifier for ScanPi alerts.

Fires a single HTTP POST to a user-configured URL whenever a transcript
matches a keyword alert. Keep this dead simple — a single JSON body. If
the user wants fancy Discord / Slack / Home Assistant / ntfy formatting,
they can run a small adapter service; this module just emits the raw
event.

Configured via the env var SCANPI_WEBHOOK_URL (read per tool at startup)
or by passing webhook_url in the tool config.

Payload schema:
{
    "tool": "op25" | "gmrs",
    "event_type": "alert",
    "alert_kind": "fire" | "violence" | ...,
    "alert_match": "shots fired",
    "tgid": 23701,          # OP25 only
    "tg_name": "Stonington PD",
    "category": "police",
    "channel": 1,           # GMRS only
    "freq_mhz": 462.5625,
    "transcript": "...",
    "timestamp": 1700000000.0,
    "clip_url": "http://host:8080/tools/op25/api/clip/123"
}
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


def fire_webhook(url: str, payload: dict, timeout: float = 5.0) -> None:
    """POST payload to url. Non-blocking — spawns a thread and returns.

    Failures are logged, never raised. Webhook is best-effort.
    """
    if not url:
        return
    def _do():
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json",
                         "User-Agent": "ScanPi/0.3"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status >= 400:
                    log.warning("webhook %s returned %d", url, resp.status)
        except urllib.error.URLError as e:
            log.warning("webhook %s failed: %s", url, e)
        except Exception:
            log.exception("webhook crashed")
    threading.Thread(target=_do, name="scanpi-webhook", daemon=True).start()
