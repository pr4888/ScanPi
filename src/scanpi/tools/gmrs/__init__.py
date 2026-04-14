"""GMRS/FRS Monitor tool — 15-channel parallel activity monitor with audio recording."""
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

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ...tools import Tool, ToolStatus
from .channels import Channel, CHANNELS_462
from .db import GmrsDB
from .monitor import GmrsMonitor, MonitorConfig

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


class GmrsTool(Tool):
    id = "gmrs"
    name = "GMRS Monitor"
    description = "15-channel parallel FRS/GMRS activity monitor with per-TX audio recording"
    needs_sdr = True

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        cfg = self.config
        data_dir = Path(cfg.get("data_dir", Path.home() / "scanpi"))
        self._db_path = data_dir / "gmrs.db"
        self._audio_dir = data_dir / "gmrs_audio"
        self._audio_dir.mkdir(parents=True, exist_ok=True)

        self._monitor_cfg = MonitorConfig(
            center_hz=int(cfg.get("center_hz", 462_637_500)),
            sample_rate=int(cfg.get("sample_rate", 2_000_000)),
            rtl_gain=float(cfg.get("gain", 40.0)),
            squelch_db=float(cfg.get("squelch_db", -30.0)),
            preroll_s=float(cfg.get("preroll_s", 1.5)),
            max_record_s=float(cfg.get("max_record_s", 120.0)),
        )
        self._channels: list[Channel] = list(CHANNELS_462)
        self._db: GmrsDB | None = None
        self._monitor: GmrsMonitor | None = None
        self._lock = threading.Lock()
        self._live: dict[int, LiveChannelState] = {}
        self._open_events: dict[int, dict] = {}
        self._started_ts: float | None = None

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self._db = GmrsDB(self._db_path)
        self._db.connect()
        # Cleanup any events that never closed (prior unclean shutdown)
        cur = self._db.conn.execute(
            "UPDATE tx_events SET end_ts = start_ts, duration_s = 0 WHERE end_ts IS NULL"
        )
        if cur.rowcount:
            log.info("cleaned %d orphan tx_events from prior runs", cur.rowcount)
        self._live = {
            ch.num: LiveChannelState(channel=ch.num, freq_hz=ch.freq_hz)
            for ch in self._channels
        }
        self._monitor = GmrsMonitor(
            self._monitor_cfg, self._channels,
            on_open=self._on_open,
            on_rssi=self._on_rssi,
            on_close=self._on_close,
            on_tick=self._on_tick,
        )
        self._monitor.start()
        self._started_ts = time.time()

    def stop(self) -> None:
        if self._monitor is not None:
            try:
                self._monitor.stop()
            except Exception:
                log.exception("GMRS monitor stop failed")
            self._monitor = None
        if self._db is not None:
            self._db.close()
            self._db = None
        self._open_events.clear()
        self._started_ts = None

    def status(self) -> ToolStatus:
        running = self._monitor is not None
        last = max((s.last_event_ts for s in self._live.values()), default=0) if running else 0
        return ToolStatus(
            running=running, healthy=True,
            last_activity_ts=last if last > 0 else None,
            message=f"{len(self._channels)} channels @ {self._monitor_cfg.center_hz/1e6:.4f} MHz" if running else "stopped",
            extra={"channels": len(self._channels),
                   "squelch_db": self._monitor_cfg.squelch_db,
                   "started_ts": self._started_ts},
        )

    def summary(self) -> dict:
        """Dashboard widget payload."""
        if not self._db:
            return {"running": False}
        since = time.time() - 24 * 3600
        stats = self._db.channel_stats(since_ts=since)
        top = stats[0] if stats else None
        return {
            "running": True,
            "top_channel": top["channel"] if top else None,
            "top_freq_mhz": None,  # filled below if top exists
            "top_count": top["tx_count"] if top else 0,
            "total_tx_24h": sum(s["tx_count"] for s in stats),
            "active_channels_24h": len(stats),
        }

    # --- monitor callbacks ---------------------------------------------

    def _on_tick(self, ch: Channel, rssi: float):
        if ch.num in self._live:
            self._live[ch.num].last_rssi = rssi

    def _on_open(self, ch: Channel, ts: float, rssi: float):
        date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        time_str = datetime.fromtimestamp(ts).strftime("%H%M%S")
        wav_path = self._audio_dir / date_str / f"ch{ch.num:02d}" / f"ch{ch.num:02d}_{time_str}_{int(ts)}.wav"
        recorder = self._monitor.recorders.get(ch.num) if self._monitor else None
        if recorder is not None:
            try:
                recorder.start_record(str(wav_path))
            except Exception:
                log.exception("start_record failed ch=%d", ch.num)
                wav_path = None
        with self._lock:
            evt_id = self._db.open_event(ch.num, ch.freq_hz, ts, rssi)
            self._open_events[ch.num] = {"id": evt_id, "path": str(wav_path) if wav_path else None}
            s = self._live[ch.num]
            s.open = True
            s.open_since = ts
            s.last_rssi = rssi
            s.peak_rssi = rssi
        log.info("OPEN  ch=%-2d  rssi=%6.1f dBFS  freq=%.4f MHz",
                 ch.num, rssi, ch.freq_hz / 1e6)

    def _on_rssi(self, ch: Channel, rssi: float):
        with self._lock:
            s = self._live[ch.num]
            s.last_rssi = rssi
            if rssi > s.peak_rssi:
                s.peak_rssi = rssi
            evt = self._open_events.get(ch.num)
            if evt:
                self._db.update_event_rssi(evt["id"], rssi)

    def _on_close(self, ch: Channel, ts: float):
        clip_path = None
        recorder = self._monitor.recorders.get(ch.num) if self._monitor else None
        if recorder is not None:
            clip_path, _ = recorder.stop_record()
        with self._lock:
            s = self._live[ch.num]
            duration = ts - (s.open_since or ts)
            s.open = False
            s.open_since = None
            s.last_event_ts = ts
            peak = s.peak_rssi
            evt = self._open_events.pop(ch.num, None)
            if evt:
                self._db.close_event(evt["id"], ts, clip_path=clip_path)
            s.peak_rssi = -120.0
        log.info("CLOSE ch=%-2d  dur=%5.1fs  peak=%6.1f dBFS", ch.num, duration, peak)

    # --- API ------------------------------------------------------------

    def api_router(self):
        r = APIRouter()

        @r.get("/live")
        def live():
            with self._lock:
                return {
                    "center_hz": self._monitor_cfg.center_hz,
                    "sample_rate": self._monitor_cfg.sample_rate,
                    "squelch_db": self._monitor_cfg.squelch_db,
                    "channels": [
                        {
                            "channel": s.channel, "freq_hz": s.freq_hz, "open": s.open,
                            "last_rssi": round(s.last_rssi, 1),
                            "peak_rssi": round(s.peak_rssi, 1),
                            "open_since": s.open_since,
                            "last_event_ts": s.last_event_ts,
                        }
                        for s in self._live.values()
                    ],
                }

        @r.get("/stats")
        def stats(hours: float = 24.0):
            since = time.time() - hours * 3600 if hours > 0 else 0.0
            if not self._db:
                return {"hours": hours, "channels": []}
            return {"hours": hours, "channels": self._db.channel_stats(since_ts=since)}

        @r.get("/recent")
        def recent(limit: int = 50):
            if not self._db:
                return {"events": []}
            return {"events": self._db.recent_events(limit)}

        @r.get("/clip/{event_id}")
        def clip(event_id: int):
            if not self._db:
                raise HTTPException(404, "db offline")
            row = self._db.conn.execute(
                "SELECT clip_path FROM tx_events WHERE id = ?", (event_id,),
            ).fetchone()
            if not row or not row["clip_path"] or not os.path.exists(row["clip_path"]):
                raise HTTPException(404, "clip not found")
            return FileResponse(row["clip_path"], media_type="audio/wav",
                                filename=Path(row["clip_path"]).name)

        return r

    def page_html(self) -> str:
        return (Path(__file__).parent / "page.html").read_text(encoding="utf-8")
