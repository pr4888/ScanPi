"""Alerts tool — watchlist matching + MQTT publish for ScanPi.

Polls gmrs.db and op25.db (READ-ONLY, sqlite ?mode=ro URI) every few seconds,
runs new transcripts through the watchlist matcher, and publishes hits to
MQTT (scanpi/alerts/<severity>/<source>) plus alerts.db for history.

Integration in app_v3.py:
    # 1. import
    from .tools.alerts import AlertsTool
    # 2. register (after gmrs / op25)
    registry.register(AlertsTool(config={"data_dir": str(data_dir)}))
    # 3. nothing else — needs_sdr=False, coordinator starts non-SDR tools.

The tool gracefully handles missing source DBs (the file simply doesn't
exist yet on a fresh Pi) and missing paho-mqtt (alerts still hit the DB).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from ...tools import Tool, ToolStatus
from .api import make_router
from .db import AlertsDB
from .matcher import Hit, aggregate_severity, match_transcript
from .publisher import MQTTConfig, MQTTPublisher
from .watchlist import Watchlist

log = logging.getLogger(__name__)


# Try profile.feature_enabled, fall back to env var.
def _feature_enabled(name: str, default: bool = True) -> bool:
    try:
        from ...profile import feature_enabled  # type: ignore
        return bool(feature_enabled(name))
    except Exception:
        import os
        env = os.environ.get(f"SCANPI_FEATURE_{name.upper()}")
        if env is None:
            return default
        return env.strip().lower() in ("1", "true", "yes", "on")


# ----------------------------------------------------------- source schemas


class _SourceConfig:
    """Per-source poll + URL config."""
    __slots__ = ("source", "db_path", "table", "id_col", "ts_col",
                 "transcript_col", "channel_col", "audio_url_fn", "channel_label_fn")

    def __init__(self, source, db_path, table, id_col, ts_col,
                 transcript_col, channel_col, audio_url_fn, channel_label_fn):
        self.source = source
        self.db_path = db_path
        self.table = table
        self.id_col = id_col
        self.ts_col = ts_col
        self.transcript_col = transcript_col
        self.channel_col = channel_col
        self.audio_url_fn = audio_url_fn
        self.channel_label_fn = channel_label_fn


def _audio_url_gmrs(row: dict) -> str | None:
    return f"/tools/gmrs/api/clip/{row['id']}" if row.get("clip_path") else None


def _audio_url_op25(row: dict) -> str | None:
    return f"/tools/op25/api/clip/{row['id']}" if row.get("clip_path") else None


def _channel_label_gmrs(row: dict) -> str:
    ch = row.get("channel")
    return f"Ch {ch}" if ch is not None else "GMRS"


def _channel_label_op25(row: dict) -> str:
    name = row.get("tg_name") or ""
    tgid = row.get("tgid")
    if name and tgid is not None:
        return f"{name} (TG {tgid})"
    if tgid is not None:
        return f"TG {tgid}"
    return "OP25"


class AlertsTool(Tool):
    id = "alerts"
    name = "Alerts"
    description = "Watchlist matching + MQTT push notifications for transcripts"
    needs_sdr = False

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        cfg = self.config
        self._data_dir = Path(cfg.get("data_dir", Path.home() / "scanpi"))
        self._data_dir.mkdir(parents=True, exist_ok=True)

        # Own DB
        self.db = AlertsDB(self._data_dir / "alerts.db")
        self.db.connect()

        # Watchlist
        watchlist_path = Path(cfg.get("watchlist_path",
                                      self._data_dir / "watchlist.yaml"))
        self.watchlist = Watchlist(watchlist_path)

        # MQTT
        mqtt_url = cfg.get("mqtt_url")
        self._mqtt_enabled = _feature_enabled("mqtt_alerts", True)
        self.publisher = MQTTPublisher(MQTTConfig(url=mqtt_url) if mqtt_url
                                       else MQTTConfig.from_env())

        # Source DB locations — relative to same data_dir.
        self._sources = [
            _SourceConfig(
                source="gmrs",
                db_path=self._data_dir / "gmrs.db",
                table="tx_events",
                id_col="id",
                ts_col="end_ts",
                transcript_col="transcript",
                channel_col="channel",
                audio_url_fn=_audio_url_gmrs,
                channel_label_fn=_channel_label_gmrs,
            ),
            _SourceConfig(
                source="op25",
                db_path=self._data_dir / "op25.db",
                table="p25_calls",
                id_col="id",
                ts_col="end_ts",
                transcript_col="transcript",
                channel_col="tgid",
                audio_url_fn=_audio_url_op25,
                channel_label_fn=_channel_label_op25,
            ),
        ]

        # Worker state
        self._poll_interval_s = float(cfg.get("poll_interval_s", 4.0))
        self._backfill_hours = float(cfg.get("backfill_hours", 24.0))
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._last_poll_ts: float | None = None
        self._started_ts: float | None = None
        self._running = False
        self._cursor: dict[str, int] = {s.source: 0 for s in self._sources}

        # FastAPI router (built once, reused on every status query)
        self._router = make_router(self)

    # --- lifecycle ----------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._stop.clear()
        if self._mqtt_enabled:
            self.publisher.start()
        self._backfill()
        self._worker = threading.Thread(
            target=self._poll_loop, daemon=True, name="alerts-poll"
        )
        self._worker.start()
        self._started_ts = time.time()
        self._running = True
        log.info("AlertsTool started (poll=%.1fs, sources=%d, mqtt=%s)",
                 self._poll_interval_s, len(self._sources),
                 "on" if self._mqtt_enabled else "off")

    def stop(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=5.0)
            self._worker = None
        try:
            self.publisher.stop()
        except Exception:
            log.exception("publisher stop failed")
        self._running = False
        # Keep DB open so historical reads work.

    def status(self) -> ToolStatus:
        counts = self.db.counts()
        last = self._db_last_alert_ts()
        msg_parts = []
        if self._mqtt_enabled:
            if self.publisher.available:
                msg_parts.append("mqtt connected" if self.publisher.is_connected
                                 else "mqtt offline")
            else:
                msg_parts.append("mqtt n/a")
        msg_parts.append(f"{counts['total']} alerts ({counts['unacked']} unacked)")
        healthy = True
        if self._mqtt_enabled and self.publisher.available and not self.publisher.is_connected:
            healthy = False
        return ToolStatus(
            running=self._running, healthy=healthy,
            last_activity_ts=last, message=" · ".join(msg_parts),
            extra={
                "started_ts": self._started_ts,
                "last_poll_ts": self._last_poll_ts,
                "rules": len(self.watchlist.all()),
                "rules_enabled": len(self.watchlist.enabled()),
                "mqtt_url": self.publisher.cfg.url,
                "mqtt_connected": self.publisher.is_connected,
                "counts": counts,
            },
        )

    def summary(self) -> dict:
        counts = self.db.counts()
        last = self._db_last_alert_ts()
        # Build a "preview" alert for dashboard
        preview = preview_ts = preview_label = None
        rows = self.db.list_alerts(since_ts=0, limit=1)
        if rows:
            r0 = rows[0]
            preview = r0.get("transcript")
            preview_ts = r0.get("ts")
            preview_label = r0.get("channel")
        return {
            "running": self._running,
            "total_alerts_24h": sum(1 for a in self.db.list_alerts(
                since_ts=time.time() - 86400, limit=10000)),
            "active_channels_24h": None,
            "last_activity_ts": last,
            "preview": preview,
            "preview_ts": preview_ts,
            "preview_tg": preview_label,
            "all_time_count": counts["total"],
            "alert_counts": {k: counts[k] for k in
                             ("low", "medium", "high", "critical") if counts.get(k)},
        }

    # --- public helpers ---------------------------------------------

    def health_payload(self) -> dict:
        return {
            "running": self._running,
            "last_poll_ts": self._last_poll_ts,
            "mqtt": {
                "enabled": self._mqtt_enabled,
                "available": self.publisher.available,
                "connected": self.publisher.is_connected,
                "url": self.publisher.cfg.url,
                "last_error": self.publisher.last_error,
            },
            "watchlist": {
                "rules": len(self.watchlist.all()),
                "enabled": len(self.watchlist.enabled()),
            },
            "counts": self.db.counts(),
            "sources": [
                {"source": s.source, "db_path": str(s.db_path),
                 "exists": s.db_path.exists(),
                 "cursor": self._cursor.get(s.source, 0)}
                for s in self._sources
            ],
        }

    # --- polling worker ----------------------------------------------

    def _backfill(self):
        """Scan last N hours of source DBs to populate alert history.

        Only matches that haven't already been recorded (idempotent on
        (source, source_call_id)). On a fresh install this means 24h of
        history shows up immediately.
        """
        cutoff = time.time() - self._backfill_hours * 3600
        for src in self._sources:
            if not src.db_path.exists():
                continue
            try:
                rows = self._read_new_rows(src, since_ts=cutoff,
                                           after_id=0)
                self._cursor[src.source] = self._handle_rows(src, rows,
                                                             from_backfill=True)
            except Exception:
                log.exception("backfill failed for source=%s", src.source)

    def _poll_loop(self):
        log.info("alerts polling loop started (interval=%.1fs)", self._poll_interval_s)
        while not self._stop.is_set():
            self._last_poll_ts = time.time()
            for src in self._sources:
                if not src.db_path.exists():
                    continue
                try:
                    rows = self._read_new_rows(
                        src, since_ts=0, after_id=self._cursor.get(src.source, 0)
                    )
                    self._cursor[src.source] = self._handle_rows(src, rows)
                except Exception:
                    log.exception("poll iteration failed for source=%s", src.source)
            # Sleep with stop check
            self._stop.wait(self._poll_interval_s)

    def _read_new_rows(self, src: _SourceConfig, *, since_ts: float,
                       after_id: int) -> list[dict]:
        """Read transcript rows from a source DB in read-only mode.

        Filters: transcript IS NOT NULL AND id > after_id AND ts >= since_ts.
        For OP25/GMRS, the DB allows status='ok' transcripts only.
        """
        uri = f"file:{src.db_path}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, isolation_level=None,
                                   check_same_thread=False, timeout=2.0)
        except sqlite3.OperationalError as e:
            log.debug("source %s not openable: %s", src.source, e)
            return []
        try:
            conn.row_factory = sqlite3.Row
            sql = (
                f"SELECT * FROM {src.table} "
                f"WHERE {src.id_col} > ? "
                f"AND {src.transcript_col} IS NOT NULL "
                f"AND TRIM({src.transcript_col}) <> '' "
                "AND transcript_status = 'ok' "
            )
            params: list = [int(after_id)]
            if since_ts > 0:
                sql += f"AND {src.ts_col} >= ? "
                params.append(float(since_ts))
            sql += f"ORDER BY {src.id_col} ASC LIMIT 500"
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _handle_rows(self, src: _SourceConfig, rows: list[dict],
                     *, from_backfill: bool = False) -> int:
        """Match each row, write alerts, publish. Returns the highest id seen."""
        max_id = self._cursor.get(src.source, 0)
        for row in rows:
            row_id = int(row[src.id_col])
            max_id = max(max_id, row_id)
            transcript = row.get(src.transcript_col) or ""
            hits = match_transcript(transcript, self.watchlist)
            if not hits:
                continue
            if self.db.already_recorded(src.source, row_id):
                continue
            self._record_alert(src, row, transcript, hits, from_backfill)
        return max_id

    def _record_alert(self, src: _SourceConfig, row: dict, transcript: str,
                      hits: list[Hit], from_backfill: bool):
        ts = row.get(src.ts_col) or row.get("start_ts") or time.time()
        severity = aggregate_severity(hits)
        rule_names = [h.rule.name for h in hits]
        channel_label = src.channel_label_fn(row)
        audio_url = src.audio_url_fn(row)
        try:
            alert_id = self.db.insert_alert(
                ts=float(ts), source=src.source,
                source_call_id=int(row[src.id_col]),
                channel=channel_label, severity=severity,
                rules_matched=rule_names,
                transcript=transcript, audio_url=audio_url,
            )
        except Exception:
            log.exception("alerts.db insert failed for %s id=%s",
                          src.source, row.get(src.id_col))
            return

        for h in hits:
            self.db.bump_history(h.rule.name, float(ts))

        log.warning(
            "ALERT [%s] %s/%s rules=%s text=%r%s",
            severity.upper(), src.source, channel_label,
            ",".join(rule_names),
            (transcript or "")[:120],
            " (backfill)" if from_backfill else "",
        )

        # Publish to MQTT (best effort).
        if not self._mqtt_enabled:
            return
        # Topic suffix from the highest-severity hit's mqtt_topic_suffix.
        suffix = ""
        for h in sorted(hits, key=lambda x: -_sev_rank(x.rule.severity)):
            if h.rule.mqtt_topic_suffix:
                suffix = h.rule.mqtt_topic_suffix
                break
        payload = {
            "id": alert_id,
            "ts": float(ts),
            "source": src.source,
            "channel_or_tg": channel_label,
            "transcript": transcript,
            "matched_rules": rule_names,
            "audio_url": audio_url,
            "severity": severity,
            "hits": [h.to_dict() for h in hits],
            "backfill": from_backfill,
        }
        self.publisher.publish_alert(payload, topic_suffix=suffix or None)

    def _db_last_alert_ts(self) -> float | None:
        try:
            row = self.db.conn.execute(
                "SELECT MAX(ts) AS t FROM alerts"
            ).fetchone()
            return row["t"] if row and row["t"] else None
        except Exception:
            return None

    # --- API + page --------------------------------------------------

    def api_router(self):
        return self._router

    def page_html(self) -> str:
        return (Path(__file__).parent / "page.html").read_text(encoding="utf-8")


def _sev_rank(severity: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(severity, 0)
