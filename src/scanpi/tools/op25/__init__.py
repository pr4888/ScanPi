"""P25 Trunking tool — wraps OP25 multi_rx.py + audio capture as a ScanPi Tool."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ...alerts import scan as scan_alerts
from ...notify import fire_webhook
from ...tools import Tool, ToolStatus
from ...retention import RetentionConfig, RetentionManager
from ..gmrs import _serve_file_with_range  # reuse range-aware audio serving
from ..gmrs.transcriber import TranscribeJob, TranscriptionWorker
from .bridge import ActiveCall, OP25Bridge, BridgeConfig
from .channels import classify, load_talkgroups
from .db import OP25DB

log = logging.getLogger(__name__)


class OP25Tool(Tool):
    id = "op25"
    name = "P25 Trunking"
    description = "Listen to trunked P25 systems (police/fire/EMS) via OP25 + whisper transcription"
    needs_sdr = True

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        cfg = self.config
        data_dir = Path(cfg.get("data_dir", Path.home() / "scanpi"))
        self._db_path = data_dir / "op25.db"
        self._audio_dir = data_dir / "op25_audio"
        self._log_path = data_dir / "logs" / "op25.log"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

        self._op25_dir = Path(cfg.get(
            "op25_dir",
            Path.home() / "op25" / "op25" / "gr-op25_repeater" / "apps",
        ))
        self._config_json = str(cfg.get("op25_config", "clmrn_cfg.json"))
        self._tg_tsv_rel = str(cfg.get("talkgroups_tsv", "clmrn_talkgroups.tsv"))
        # OP25 sends decoded voice to the channel's `destination` UDP port.
        # clmrn_cfg.json uses 2345; override via config if your setup differs.
        self._udp_port = int(cfg.get("udp_port", 2345))

        self._bridge_cfg = BridgeConfig(
            op25_dir=self._op25_dir,
            config_json=self._config_json,
            log_path=self._log_path,
            audio_dir=self._audio_dir,
            udp_port=self._udp_port,
        )
        # Open DB immediately so historical reads work before/after start().
        self._db: OP25DB = OP25DB(self._db_path)
        self._db.connect()
        self._bridge: OP25Bridge | None = None
        self._transcriber: TranscriptionWorker | None = None
        self._talkgroups: dict[int, dict] = {}
        self._retention: RetentionManager | None = None
        self._max_age_days = float(cfg.get("max_age_days", 7.0))
        self._max_total_mb = float(cfg.get("max_total_mb", 1024.0))
        self._webhook_url = cfg.get("webhook_url") or os.environ.get("SCANPI_WEBHOOK_URL", "")
        self._public_base = cfg.get("public_base_url") or os.environ.get("SCANPI_PUBLIC_URL", "")

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if not self._op25_dir.exists():
            raise RuntimeError(
                f"OP25 not found at {self._op25_dir} — install per "
                "https://github.com/boatbod/op25 before activating this tool"
            )
        orphaned = self._db.orphan_cleanup()
        if orphaned:
            log.info("cleaned %d orphan p25_calls from prior run", orphaned)

        # Load talkgroups (best-effort)
        tg_path = self._op25_dir / self._tg_tsv_rel
        self._talkgroups = load_talkgroups(tg_path)
        log.info("loaded %d talkgroups from %s", len(self._talkgroups), tg_path)

        # Migration: retroactive keyword alert scan on already-transcribed
        # calls that were captured before the alert system existed.
        unscanned = self._db.conn.execute(
            "SELECT id, transcript FROM p25_calls "
            "WHERE transcript IS NOT NULL AND transcript_status = 'ok' "
            "AND alert_kind IS NULL"
        ).fetchall()
        flagged = 0
        for row in unscanned:
            kind, match = scan_alerts(row["transcript"])
            if kind:
                self._db.conn.execute(
                    "UPDATE p25_calls SET alert_kind = ?, alert_match = ? WHERE id = ?",
                    (kind, match, row["id"]),
                )
                flagged += 1
        if unscanned:
            log.info("retroactive alert scan: %d rows, %d flagged", len(unscanned), flagged)

        # Migration: earlier versions wrote priority ('1','2',...) into the
        # category column. Re-classify any rows whose category is a plain
        # number or empty using the current tg_name + classifier.
        bad = self._db.conn.execute(
            "SELECT id, tgid, tg_name, category FROM p25_calls "
            "WHERE category IS NULL OR category = '' OR category GLOB '[0-9]*'"
        ).fetchall()
        if bad:
            for row in bad:
                name = row["tg_name"] or ""
                tg = self._talkgroups.get(row["tgid"])
                new_cat = (tg or {}).get("category") or classify(name)
                self._db.conn.execute(
                    "UPDATE p25_calls SET category = ? WHERE id = ?",
                    (new_cat, row["id"]),
                )
            log.info("re-classified %d legacy rows with bad category", len(bad))

        self._bridge = OP25Bridge(
            self._bridge_cfg,
            on_call_open=self._open_call,
            on_call_close=self._close_call,
        )
        self._bridge.start()

        # Audio retention / disk budget
        self._retention = RetentionManager(
            RetentionConfig(
                audio_dir=self._audio_dir,
                max_age_days=self._max_age_days,
                max_total_mb=self._max_total_mb,
            ),
            on_deleted=self._on_clips_deleted,
        )
        self._retention.start()

        # Transcription — reuse GMRS worker pattern
        if bool(self.config.get("transcribe", True)):
            model_dir = Path(self.config.get(
                "whisper_model_dir",
                self._audio_dir.parent / "models",
            ))
            model_dir.mkdir(parents=True, exist_ok=True)
            self._transcriber = TranscriptionWorker(
                on_result=self._on_transcribe_result,
                model_name=str(self.config.get("whisper_model", "tiny.en")),
                model_dir=model_dir,
            )
            self._transcriber.start()

    def stop(self) -> None:
        """Stop the live capture pipeline but keep the DB open so
        historical calls remain browsable via the API while the tool
        is idle (user may swap back, or just want to view yesterday's
        data while the other tool holds the SDR).
        """
        if self._transcriber is not None:
            try: self._transcriber.stop()
            except Exception: log.exception("transcriber stop failed")
            self._transcriber = None
        if self._retention is not None:
            try: self._retention.stop()
            except Exception: log.exception("retention stop failed")
            self._retention = None
        if self._bridge is not None:
            try: self._bridge.stop()
            except Exception: log.exception("bridge stop failed")
            self._bridge = None
        # DB stays open — historical reads still work while idle.

    def _on_clips_deleted(self, paths: list[str]):
        """Null out clip_path for rows whose WAV was pruned."""
        if not self._db or not paths:
            return
        # SQLite IN (...) with parameter list
        placeholders = ",".join("?" * len(paths))
        try:
            self._db.conn.execute(
                f"UPDATE p25_calls SET clip_path = NULL WHERE clip_path IN ({placeholders})",
                paths,
            )
        except Exception:
            log.exception("failed to null out deleted clip_paths in DB")

    # --- callbacks from bridge -----------------------------------------

    def _open_call(self, ac: ActiveCall) -> int:
        tg = self._talkgroups.get(ac.tgid)
        name = tg["name"] if tg else f"TG-{ac.tgid}"
        category = tg["category"] if (tg and tg.get("category")) else classify(name)
        return self._db.open_call(ac.tgid, name, category, ac.rid, ac.freq_mhz, ac.start_ts)

    def _close_call(self, call_id: int, end_ts: float, clip_path: str | None):
        if self._db is None:
            return
        self._db.close_call(call_id, end_ts, clip_path=clip_path)
        if clip_path and self._transcriber is not None:
            self._db.set_transcript(call_id, None, "pending")
            # Pull tgid for context in log
            self._transcriber.submit(TranscribeJob(
                event_id=call_id,
                clip_path=clip_path,
                channel=0,  # not used for P25
            ))

    def _on_transcribe_result(self, call_id: int, text: str | None, status: str):
        if self._db is None:
            return
        alert_kind, alert_match = (scan_alerts(text) if status == "ok" else (None, None))
        try:
            self._db.set_transcript(call_id, text, status,
                                     alert_kind=alert_kind, alert_match=alert_match)
        except Exception:
            log.exception("failed to write transcript for call %d", call_id)
        if alert_kind:
            log.warning("🚨 ALERT ch=op25 call=%d kind=%s match=%r text=%r",
                        call_id, alert_kind, alert_match, (text or "")[:100])
            # Fire webhook notification if configured.
            if self._webhook_url:
                row = self._db.conn.execute(
                    "SELECT tgid, tg_name, category, freq_mhz, start_ts, clip_path "
                    "FROM p25_calls WHERE id = ?", (call_id,),
                ).fetchone()
                if row:
                    clip_url = None
                    if row["clip_path"] and self._public_base:
                        clip_url = f"{self._public_base.rstrip('/')}/tools/op25/api/clip/{call_id}"
                    fire_webhook(self._webhook_url, {
                        "tool": "op25",
                        "event_type": "alert",
                        "alert_kind": alert_kind,
                        "alert_match": alert_match,
                        "tgid": row["tgid"],
                        "tg_name": row["tg_name"],
                        "category": row["category"],
                        "freq_mhz": row["freq_mhz"],
                        "transcript": text,
                        "timestamp": row["start_ts"],
                        "clip_url": clip_url,
                    })

    # --- status / summary -----------------------------------------------

    def status(self) -> ToolStatus:
        running = self._bridge is not None
        last = self._db.last_call_end_ts() if self._db else None
        healthy = True
        if running and self._bridge:
            snap = self._bridge.snapshot()
            # Health: decoder running + recent activity (or just started)
            if not snap.get("running"):
                healthy = False
                msg = "decoder died — watchdog will respawn"
            else:
                msg = f"active calls: {len(snap['active_calls'])}"
                if snap.get("cc_freq_mhz"):
                    msg += f" · cc {snap['cc_freq_mhz']:.5f} MHz"
                started = snap.get("started_at") or time.time()
                uptime = time.time() - started
                # Warn if no calls in 15m AND uptime >15m (i.e., past warmup)
                if uptime > 900 and (not last or time.time() - last > 900):
                    healthy = False
                    msg += " · ⚠ no calls in 15m (check signal?)"
                if snap.get("restart_count", 0) > 0:
                    msg += f" · restarts: {snap['restart_count']}"
        else:
            msg = "stopped"
        return ToolStatus(
            running=running, healthy=healthy,
            last_activity_ts=last,
            message=msg,
            extra={"op25_dir": str(self._op25_dir), "config": self._config_json,
                   "talkgroups_loaded": len(self._talkgroups)},
        )

    def summary(self) -> dict:
        if not self._db:
            return {"running": False}
        since = time.time() - 24 * 3600
        stats = self._db.talkgroup_stats(since_ts=since)
        top = stats[0] if stats else None
        # Latest transcribed call — tiny preview on the dashboard card
        preview = None
        preview_ts = None
        preview_tg = None
        row = self._db.conn.execute(
            "SELECT tg_name, transcript, end_ts FROM p25_calls "
            "WHERE transcript IS NOT NULL AND transcript_status = 'ok' "
            "ORDER BY end_ts DESC LIMIT 1"
        ).fetchone()
        if row:
            preview = row["transcript"]
            preview_ts = row["end_ts"]
            preview_tg = row["tg_name"]
        return {
            "running": True,
            "total_calls_24h": sum(s["call_count"] for s in stats),
            "active_tgs_24h": len(stats),
            "top_tg": top["tgid"] if top else None,
            "top_tg_name": top["tg_name"] if top else None,
            "top_count": top["call_count"] if top else 0,
            "last_activity_ts": self._db.last_call_end_ts(),
            "preview": preview,
            "preview_tg": preview_tg,
            "preview_ts": preview_ts,
            "alert_counts": self._db.alert_counts_24h(),
            "all_time_count": self._db.all_time_count(),
        }

    # --- API ------------------------------------------------------------

    def api_router(self):
        r = APIRouter()

        @r.get("/live")
        def live():
            if self._bridge is None:
                return {"running": False, "active_calls": []}
            return self._bridge.snapshot()

        @r.get("/stats")
        def stats(hours: float = 24.0):
            if not self._db:
                return {"hours": hours, "talkgroups": []}
            since = time.time() - hours * 3600 if hours > 0 else 0.0
            return {"hours": hours, "talkgroups": self._db.talkgroup_stats(since_ts=since)}

        @r.get("/recent")
        def recent(limit: int = 50):
            if not self._db:
                return {"calls": []}
            return {"calls": self._db.recent(limit)}

        @r.get("/talkgroups")
        def talkgroups():
            return {"count": len(self._talkgroups),
                    "talkgroups": list(self._talkgroups.values())}

        @r.get("/clip/{call_id}")
        def clip(call_id: int, request: Request):
            if not self._db:
                raise HTTPException(404, "db offline")
            row = self._db.conn.execute(
                "SELECT clip_path FROM p25_calls WHERE id = ?", (call_id,),
            ).fetchone()
            if not row or not row["clip_path"] or not os.path.exists(row["clip_path"]):
                raise HTTPException(404, "clip not found")
            return _serve_file_with_range(row["clip_path"], "audio/wav", request)

        @r.get("/call/{call_id}")
        def call_detail(call_id: int):
            if not self._db:
                raise HTTPException(404, "db offline")
            c = self._db.get_call(call_id)
            if not c:
                raise HTTPException(404, "call not found")
            # Related recent calls on the same TG (for context)
            related = self._db.conn.execute(
                "SELECT id, start_ts, duration_s, transcript, transcript_status "
                "FROM p25_calls WHERE tgid = ? AND id != ? "
                "ORDER BY start_ts DESC LIMIT 10",
                (c["tgid"], call_id),
            ).fetchall()
            c["related"] = [dict(r) for r in related]
            return c

        @r.get("/search")
        def search(q: str = "", limit: int = 200):
            if not self._db:
                return {"q": q, "results": []}
            if not q:
                return {"q": q, "results": []}
            return {"q": q, "results": self._db.search(q, limit=limit)}

        @r.get("/phrases")
        def phrases(hours: int = 24, limit: int = 20):
            if not self._db:
                return {"hours": hours, "phrases": []}
            return {"hours": hours, "phrases": self._db.top_phrases(hours=hours, limit=limit)}

        @r.get("/alerts")
        def alerts(limit: int = 20):
            if not self._db:
                return {"alerts": [], "counts_24h": {}}
            return {
                "alerts": self._db.recent_alerts(limit),
                "counts_24h": self._db.alert_counts_24h(),
            }

        @r.get("/hourly")
        def hourly(hours: int = 24):
            if not self._db:
                return {"hours": hours, "buckets": []}
            return {"hours": hours, "buckets": self._db.hourly_activity(hours=hours)}

        @r.get("/export.csv")
        def export_csv():
            import csv
            import io
            from fastapi.responses import Response as _R
            if not self._db:
                raise HTTPException(503, "db offline")
            rows = self._db.all_calls()
            fields = ["id", "tgid", "tg_name", "category", "rid", "freq_mhz",
                       "start_ts", "end_ts", "duration_s",
                       "clip_path", "transcript", "transcript_status"]
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k, "") for k in fields})
            ts = time.strftime("%Y%m%d_%H%M%S")
            return _R(
                content=buf.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="op25_calls_{ts}.csv"'},
            )

        return r

    def page_html(self) -> str:
        return (Path(__file__).parent / "page.html").read_text(encoding="utf-8")
