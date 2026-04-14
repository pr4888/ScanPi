"""Glue: monitor callbacks → SQLite + audio recording + Heimdall forwarding."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .channels import Channel
from .db import GmrsDB
from .monitor import GmrsMonitor, MonitorConfig, default_channels

log = logging.getLogger(__name__)


@dataclass
class LiveChannelState:
    channel: int
    freq_hz: int
    open: bool = False
    last_rssi: float = -120.0
    peak_rssi: float = -120.0
    open_since: float | None = None
    last_event_ts: float = 0.0


@dataclass
class GmrsService:
    db_path: Path
    audio_dir: Path
    cfg: MonitorConfig = field(default_factory=MonitorConfig)
    channels: list[Channel] = field(default_factory=default_channels)
    # Heimdall forwarding (optional — None = disabled)
    gateway_url: str | None = None
    gateway_token: str | None = None
    keeper_id: str = "scanpi"

    def __post_init__(self):
        self.audio_dir = Path(self.audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.db = GmrsDB(self.db_path)
        self.db.connect()
        self._cleanup_orphans()
        self._lock = threading.Lock()
        self.live: dict[int, LiveChannelState] = {
            ch.num: LiveChannelState(channel=ch.num, freq_hz=ch.freq_hz)
            for ch in self.channels
        }
        self._open_events: dict[int, dict] = {}  # channel -> {"id":..., "start":..., "path":...}
        self.monitor = GmrsMonitor(
            self.cfg, self.channels,
            on_open=self._handle_open,
            on_rssi=self._handle_rssi,
            on_close=self._handle_close,
            on_tick=self._handle_tick,
        )

    def _cleanup_orphans(self):
        """Mark any open-but-never-closed events from prior runs as orphaned."""
        cur = self.db.conn.execute(
            "UPDATE tx_events SET end_ts = start_ts, duration_s = 0 "
            "WHERE end_ts IS NULL"
        )
        if cur.rowcount:
            log.info("cleaned %d orphan tx_events from prior runs", cur.rowcount)

    def start(self):
        self.monitor.start()

    def stop(self):
        self.monitor.stop()
        self.db.close()

    # --- monitor callbacks -------------------------------------------------

    def _handle_tick(self, ch: Channel, rssi: float):
        self.live[ch.num].last_rssi = rssi

    def _handle_open(self, ch: Channel, ts: float, rssi: float):
        # Arm audio recorder
        date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        time_str = datetime.fromtimestamp(ts).strftime("%H%M%S")
        ch_dir = self.audio_dir / date_str / f"ch{ch.num:02d}"
        wav_path = ch_dir / f"ch{ch.num:02d}_{time_str}_{int(ts)}.wav"
        recorder = self.monitor.recorders.get(ch.num)
        if recorder is not None:
            try:
                recorder.start_record(str(wav_path))
            except Exception:
                log.exception("failed to start recording ch=%d", ch.num)
                wav_path = None
        else:
            wav_path = None

        with self._lock:
            evt_id = self.db.open_event(ch.num, ch.freq_hz, ts, rssi)
            self._open_events[ch.num] = {"id": evt_id, "start": ts,
                                         "path": str(wav_path) if wav_path else None}
            s = self.live[ch.num]
            s.open = True
            s.open_since = ts
            s.last_rssi = rssi
            s.peak_rssi = rssi
            log.info("OPEN  ch=%-2d  rssi=%6.1f dBFS  freq=%.4f MHz  wav=%s",
                     ch.num, rssi, ch.freq_hz / 1e6, wav_path.name if wav_path else "none")

    def _handle_rssi(self, ch: Channel, rssi: float):
        with self._lock:
            s = self.live[ch.num]
            s.last_rssi = rssi
            if rssi > s.peak_rssi:
                s.peak_rssi = rssi
            evt = self._open_events.get(ch.num)
            if evt:
                self.db.update_event_rssi(evt["id"], rssi)

    def _handle_close(self, ch: Channel, ts: float):
        # Stop recorder
        clip_path = None
        dur_audio = 0.0
        recorder = self.monitor.recorders.get(ch.num)
        if recorder is not None:
            clip_path, dur_audio = recorder.stop_record()

        with self._lock:
            s = self.live[ch.num]
            duration = ts - (s.open_since or ts)
            s.open = False
            s.open_since = None
            s.last_event_ts = ts
            peak = s.peak_rssi
            evt = self._open_events.pop(ch.num, None)
            if evt:
                self.db.close_event(evt["id"], ts, clip_path=clip_path)
            s.peak_rssi = -120.0
            log.info("CLOSE ch=%-2d  dur=%5.1fs  peak=%6.1f dBFS  audio=%.1fs",
                     ch.num, duration, peak, dur_audio)

        # Fire-and-forget forwarder to Heimdall gateway (on close only)
        if self.gateway_url and self.gateway_token and duration > 0:
            payload = {
                "source": "scanpi-gmrs",
                "keeper": self.keeper_id,
                "channel": ch.num,
                "freq_mhz": ch.freq_hz / 1e6,
                "start_ts": ts - duration,
                "end_ts": ts,
                "duration_s": round(duration, 2),
                "peak_rssi": round(peak, 2),
                "clip_path": clip_path,
            }
            threading.Thread(
                target=self._forward_to_gateway,
                args=(payload,),
                daemon=True,
            ).start()

    def _forward_to_gateway(self, payload: dict):
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self.gateway_url.rstrip('/')}/v1/gmrs/event",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.gateway_token}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status >= 400:
                    log.warning("gateway POST %d: %s", resp.status, resp.read()[:200])
        except urllib.error.URLError as e:
            log.warning("gateway forward failed: %s", e)
        except Exception:
            log.exception("gateway forward crashed")

    # --- read API ---------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "center_hz": self.cfg.center_hz,
                "sample_rate": self.cfg.sample_rate,
                "squelch_db": self.cfg.squelch_db,
                "gateway_enabled": bool(self.gateway_url and self.gateway_token),
                "channels": [
                    {
                        "channel": s.channel, "freq_hz": s.freq_hz, "open": s.open,
                        "last_rssi": round(s.last_rssi, 1),
                        "peak_rssi": round(s.peak_rssi, 1),
                        "open_since": s.open_since,
                        "last_event_ts": s.last_event_ts,
                    }
                    for s in self.live.values()
                ],
            }

    def stats(self, hours: float = 24.0) -> list[dict]:
        since = time.time() - hours * 3600 if hours > 0 else 0.0
        return self.db.channel_stats(since_ts=since)

    def recent(self, limit: int = 50) -> list[dict]:
        return self.db.recent_events(limit)

    def clip_path(self, event_id: int) -> str | None:
        """Return the clip path for a given event_id if it exists and is readable."""
        row = self.db.conn.execute(
            "SELECT clip_path FROM tx_events WHERE id = ?", (event_id,),
        ).fetchone()
        if not row or not row["clip_path"]:
            return None
        p = row["clip_path"]
        return p if os.path.exists(p) else None
