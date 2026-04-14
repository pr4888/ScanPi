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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from ...alerts import scan as scan_alerts
from ...notify import fire_webhook
from ...tools import Tool, ToolStatus
from ...retention import RetentionConfig, RetentionManager
from .channels import Channel, CHANNELS_462
from .db import GmrsDB
from .monitor import GmrsMonitor, MonitorConfig
from .transcriber import TranscribeJob, TranscriptionWorker

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
            rtl_gain=float(cfg.get("gain", 10.0)),
            squelch_db=float(cfg.get("squelch_db", -30.0)),
            preroll_s=float(cfg.get("preroll_s", 1.5)),
            max_record_s=float(cfg.get("max_record_s", 120.0)),
        )
        self._channels: list[Channel] = list(CHANNELS_462)
        # Open DB immediately so historical reads work before/after start().
        self._db: GmrsDB = GmrsDB(self._db_path)
        self._db.connect()
        self._monitor: GmrsMonitor | None = None
        self._lock = threading.Lock()
        self._live: dict[int, LiveChannelState] = {}
        self._open_events: dict[int, dict] = {}
        self._started_ts: float | None = None
        self._ch_by_num: dict[int, Channel] = {c.num: c for c in self._channels}
        self._transcriber: TranscriptionWorker | None = None
        self._transcribe_enabled = bool(cfg.get("transcribe", True))
        self._retention: RetentionManager | None = None
        self._max_age_days = float(cfg.get("max_age_days", 7.0))
        self._max_total_mb = float(cfg.get("max_total_mb", 512.0))
        self._webhook_url = cfg.get("webhook_url") or os.environ.get("SCANPI_WEBHOOK_URL", "")
        self._public_base = cfg.get("public_base_url") or os.environ.get("SCANPI_PUBLIC_URL", "")

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
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

        if self._transcribe_enabled:
            model_dir = Path(self.config.get(
                "whisper_model_dir",
                Path(self._audio_dir).parent / "models",
            ))
            model_dir.mkdir(parents=True, exist_ok=True)
            self._transcriber = TranscriptionWorker(
                on_result=self._on_transcribe_result,
                model_name=str(self.config.get("whisper_model", "tiny.en")),
                model_dir=model_dir,
                min_duration_s=float(self.config.get("whisper_min_duration_s", 0.3)),
            )
            self._transcriber.start()

        self._retention = RetentionManager(
            RetentionConfig(
                audio_dir=self._audio_dir,
                max_age_days=self._max_age_days,
                max_total_mb=self._max_total_mb,
            ),
            on_deleted=self._on_clips_deleted,
        )
        self._retention.start()

    def stop(self) -> None:
        """Stop live capture but keep DB open for historical browsing."""
        if self._transcriber is not None:
            try: self._transcriber.stop()
            except Exception: log.exception("transcriber stop failed")
            self._transcriber = None
        if self._retention is not None:
            try: self._retention.stop()
            except Exception: log.exception("retention stop failed")
            self._retention = None
        if self._monitor is not None:
            try: self._monitor.stop()
            except Exception: log.exception("GMRS monitor stop failed")
            self._monitor = None
        # DB stays open — historical reads still work while idle.
        self._open_events.clear()
        self._started_ts = None

    def _on_clips_deleted(self, paths: list[str]):
        if not self._db or not paths:
            return
        placeholders = ",".join("?" * len(paths))
        try:
            self._db.conn.execute(
                f"UPDATE tx_events SET clip_path = NULL WHERE clip_path IN ({placeholders})",
                paths,
            )
        except Exception:
            log.exception("failed to null out deleted clip_paths in DB")

    def status(self) -> ToolStatus:
        running = self._monitor is not None
        last = None
        if self._db is not None:
            last_in_mem = max((s.last_event_ts for s in self._live.values()), default=0)
            last_db = self._db.last_event_end_ts()
            last = max(filter(None, [last_in_mem if last_in_mem > 0 else None, last_db]),
                       default=None)
        healthy = True
        if running:
            msg = f"{len(self._channels)} channels @ {self._monitor_cfg.center_hz/1e6:.4f} MHz"
            started = self._started_ts or time.time()
            uptime = time.time() - started
            # GMRS is bursty — only warn after 2h with no activity (if past warmup)
            if uptime > 7200 and (not last or time.time() - last > 7200):
                healthy = False
                msg += " · ⚠ no TX in 2h"
        else:
            msg = "stopped"
        return ToolStatus(
            running=running, healthy=healthy,
            last_activity_ts=last,
            message=msg,
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
        top_freq_mhz = None
        if top is not None:
            ch = self._ch_by_num.get(top["channel"])
            if ch is not None:
                top_freq_mhz = round(ch.freq_hz / 1e6, 4)
        # Latest transcript preview (if any)
        preview = preview_ts = preview_ch = None
        row = self._db.conn.execute(
            "SELECT channel, transcript, end_ts FROM tx_events "
            "WHERE transcript IS NOT NULL AND transcript_status = 'ok' "
            "ORDER BY end_ts DESC LIMIT 1"
        ).fetchone()
        if row:
            preview = row["transcript"]
            preview_ts = row["end_ts"]
            preview_ch = row["channel"]
        return {
            "running": True,
            "top_channel": top["channel"] if top else None,
            "top_freq_mhz": top_freq_mhz,
            "top_count": top["tx_count"] if top else 0,
            "total_tx_24h": sum(s["tx_count"] for s in stats),
            "active_channels_24h": len(stats),
            "last_activity_ts": self._db.last_event_end_ts(),
            "preview": preview,
            "preview_tg": f"Ch {preview_ch}" if preview_ch else None,
            "preview_ts": preview_ts,
            "all_time_count": self._db.all_time_count(),
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
        evt_id_to_transcribe = None
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
                if clip_path and self._transcriber is not None:
                    # Mark pending so UI shows "transcribing..."
                    self._db.set_transcript(evt["id"], None, "pending")
                    evt_id_to_transcribe = evt["id"]
            s.peak_rssi = -120.0
        log.info("CLOSE ch=%-2d  dur=%5.1fs  peak=%6.1f dBFS", ch.num, duration, peak)
        if evt_id_to_transcribe is not None and clip_path:
            self._transcriber.submit(TranscribeJob(
                event_id=evt_id_to_transcribe,
                clip_path=clip_path,
                channel=ch.num,
            ))

    def _on_transcribe_result(self, event_id: int, text: str | None, status: str):
        alert_kind, alert_match = (scan_alerts(text) if status == "ok" else (None, None))
        row = None
        with self._lock:
            if self._db is not None:
                try:
                    self._db.set_transcript(event_id, text, status,
                                             alert_kind=alert_kind, alert_match=alert_match)
                except Exception:
                    log.exception("failed to write transcript for event %d", event_id)
                if alert_kind and self._webhook_url:
                    row = self._db.conn.execute(
                        "SELECT channel, freq_hz, start_ts, clip_path FROM tx_events WHERE id = ?",
                        (event_id,),
                    ).fetchone()
        if alert_kind:
            log.warning("🚨 ALERT ch=gmrs event=%d kind=%s match=%r text=%r",
                        event_id, alert_kind, alert_match, (text or "")[:100])
            if self._webhook_url and row is not None:
                clip_url = None
                if row["clip_path"] and self._public_base:
                    clip_url = f"{self._public_base.rstrip('/')}/tools/gmrs/api/clip/{event_id}"
                fire_webhook(self._webhook_url, {
                    "tool": "gmrs",
                    "event_type": "alert",
                    "alert_kind": alert_kind,
                    "alert_match": alert_match,
                    "channel": row["channel"],
                    "freq_mhz": row["freq_hz"] / 1e6,
                    "transcript": text,
                    "timestamp": row["start_ts"],
                    "clip_url": clip_url,
                })

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
        def clip(event_id: int, request: Request):
            if not self._db:
                raise HTTPException(404, "db offline")
            row = self._db.conn.execute(
                "SELECT clip_path FROM tx_events WHERE id = ?", (event_id,),
            ).fetchone()
            if not row or not row["clip_path"] or not os.path.exists(row["clip_path"]):
                raise HTTPException(404, "clip not found")
            return _serve_file_with_range(row["clip_path"], "audio/wav", request)

        @r.get("/event/{event_id}")
        def event_detail(event_id: int):
            if not self._db:
                raise HTTPException(404, "db offline")
            e = self._db.get_event(event_id)
            if not e:
                raise HTTPException(404, "event not found")
            related = self._db.conn.execute(
                "SELECT id, start_ts, duration_s, peak_rssi, transcript, transcript_status "
                "FROM tx_events WHERE channel = ? AND id != ? "
                "ORDER BY start_ts DESC LIMIT 10",
                (e["channel"], event_id),
            ).fetchall()
            e["related"] = [dict(r) for r in related]
            return e

        @r.get("/search")
        def search(q: str = "", limit: int = 200):
            if not self._db or not q:
                return {"q": q, "results": []}
            return {"q": q, "results": self._db.search(q, limit=limit)}

        @r.get("/phrases")
        def phrases(hours: int = 24, limit: int = 20):
            if not self._db:
                return {"hours": hours, "phrases": []}
            return {"hours": hours, "phrases": self._db.top_phrases(hours=hours, limit=limit)}

        @r.get("/hourly")
        def hourly(hours: int = 24):
            if not self._db:
                return {"hours": hours, "buckets": []}
            return {"hours": hours, "buckets": self._db.hourly_all(hours=hours)}

        @r.get("/export.csv")
        def export_csv():
            import csv as _csv, io as _io
            from fastapi.responses import Response as _R
            if not self._db:
                raise HTTPException(503, "db offline")
            rows = self._db.all_events()
            fields = ["id", "channel", "freq_hz", "start_ts", "end_ts", "duration_s",
                      "peak_rssi", "avg_rssi", "ctcss_hz", "ctcss_code",
                      "clip_path", "transcript", "transcript_status"]
            buf = _io.StringIO()
            w = _csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r_ in rows:
                w.writerow({k: r_.get(k, "") for k in fields})
            ts = time.strftime("%Y%m%d_%H%M%S")
            return _R(
                content=buf.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="gmrs_events_{ts}.csv"'},
            )

        return r

    def page_html(self) -> str:
        return (Path(__file__).parent / "page.html").read_text(encoding="utf-8")


def _serve_file_with_range(path: str, media_type: str, request):
    """Serve a file with HTTP Range support, using a simple in-memory Response.

    Reads the whole file once (WAV clips are tens to hundreds of KB — fine for
    RAM), then returns either the full bytes or a sliced range. Avoids any
    generator/streaming edge cases that caused inconsistent playback.
    """
    with open(path, "rb") as f:
        data = f.read()
    file_size = len(data)

    range_header = request.headers.get("range")
    if not range_header:
        return Response(
            content=data,
            media_type=media_type,
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
            },
        )

    # Parse "bytes=START-END" (either side may be empty per RFC 7233).
    try:
        units, rng = range_header.split("=", 1)
        start_s, end_s = rng.split("-", 1)
        if start_s == "" and end_s != "":
            # Suffix form: "-N" = last N bytes.
            length = int(end_s)
            start = max(0, file_size - length)
            end = file_size - 1
        else:
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
    except Exception:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
    if start >= file_size or end >= file_size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})
    length = end - start + 1

    return Response(
        content=data[start:end + 1],
        status_code=206,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        },
    )
