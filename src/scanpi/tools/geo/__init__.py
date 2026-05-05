"""GEO tool — extracts place references from radio transcripts and pins them on a map.

Lifecycle:
  start() -> spawn a worker thread that polls gmrs.db and op25.db in
              read-only mode, runs the extractor + geocoder over any
              new transcripts, writes pins to geo.db.
  stop()  -> signal the worker thread to exit, close DB.

This tool reads gmrs.db/op25.db read-only (sqlite3 ?mode=ro) and never
mutates them. All writes are to ~/scanpi/geo.db.

------------------------------------------------------------------------
Integration snippet — add this to scanpi/app_v3.py inside run_v3()
just after OP25Tool registration (and before starting the coordinator):

    from .tools.geo import GeoTool
    registry.register(GeoTool(config={"data_dir": str(data_dir)}))

GeoTool sets `needs_sdr = False` so the coordinator will start it as
part of `start_non_sdr_tools()` automatically.
------------------------------------------------------------------------
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import APIRouter

from ...tools import Tool, ToolStatus
from .api import build_router
from .db import GeoDB
from .extractor import attach_town_context, extract
from .geocoder import Geocoder

log = logging.getLogger(__name__)


# Default poll cadence — light enough not to thrash the source DBs.
POLL_INTERVAL_S = 15.0
# How long a pin stays "live" by default.
DEFAULT_PIN_TTL_S = 300.0   # 5 minutes
# Cap how much transcript context we copy into the pin row.
EXCERPT_MAX = 240


# Map our profile flag to env var fallback (per AGENT_CONTRACT.md).
def _feature_enabled(name: str, default: bool = True) -> bool:
    """Wrap scanpi.profile.feature_enabled() with env-var fallback.

    Lite + Full both default `external_geocoder` ON with caching mandatory,
    so the default is True for that key.
    """
    try:
        from ...profile import feature_enabled  # type: ignore
        return bool(feature_enabled(name))
    except Exception:
        import os
        env = "SCANPI_FEATURE_" + name.upper()
        v = os.environ.get(env)
        if v is None:
            return default
        return v == "1" or v.lower() == "true"


class GeoTool(Tool):
    id = "geo"
    name = "Geo Map"
    description = "Pulls street/town/route mentions out of transcripts and pins them on a live map"
    needs_sdr = False

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        cfg = self.config
        self.data_dir = Path(cfg.get("data_dir", Path.home() / "scanpi"))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._db_path = self.data_dir / "geo.db"
        self._gmrs_db_path = Path(cfg.get("gmrs_db", self.data_dir / "gmrs.db"))
        self._op25_db_path = Path(cfg.get("op25_db", self.data_dir / "op25.db"))

        self._poll_interval = float(cfg.get("poll_interval_s", POLL_INTERVAL_S))
        self._pin_ttl = float(cfg.get("pin_ttl_s", DEFAULT_PIN_TTL_S))
        self._excerpt_max = int(cfg.get("excerpt_max", EXCERPT_MAX))
        self._user_agent = str(cfg.get("user_agent",
                                        "ScanPi/0.4.0 (+https://github.com/pr4888/ScanPi)"))

        self.db = GeoDB(self._db_path)
        self.db.connect()
        # Seed gazetteer if empty.
        seed_dir = Path(__file__).parent / "data"
        try:
            self.db.seed_from_csv(seed_dir / "towns_seed.csv",
                                   seed_dir / "streets_seed.csv")
        except Exception:
            log.exception("gazetteer seed failed (continuing)")

        # Resolve profile knob — both lite and full default ON.
        self._external_enabled = _feature_enabled("external_geocoder", default=True)
        self._local_enabled = _feature_enabled("local_geocoder", default=False)

        self.geocoder = Geocoder(
            self.db,
            user_agent=self._user_agent,
            enable_external=self._external_enabled,
            enable_local=self._local_enabled,
        )

        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._started_ts: float | None = None
        # Cursors so we don't reprocess.
        self._gmrs_last_id = self._load_cursor("gmrs")
        self._op25_last_id = self._load_cursor("op25")
        self._processed = 0
        self._last_external_lookup_ts: float | None = None

        self._router = build_router(self)

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._started_ts = time.time()
        self._worker = threading.Thread(target=self._run, name="geo-worker", daemon=True)
        self._worker.start()
        log.info("geo tool started — poll=%.0fs ttl=%.0fs external=%s",
                 self._poll_interval, self._pin_ttl, self._external_enabled)

    def stop(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=5.0)
            self._worker = None
        # Persist cursors for next run.
        self._save_cursor("gmrs", self._gmrs_last_id)
        self._save_cursor("op25", self._op25_last_id)
        self._started_ts = None

    def status(self) -> ToolStatus:
        running = self._worker is not None and self._worker.is_alive()
        last = self.db.last_pin_ts()
        msg_parts = [
            f"pins_total={self.db.total_pins()}",
            f"processed={self._processed}",
            f"external={'on' if self._external_enabled else 'off'}",
        ]
        return ToolStatus(
            running=running,
            healthy=True,
            last_activity_ts=last,
            message=" · ".join(msg_parts),
            extra={
                "started_ts": self._started_ts,
                "poll_interval_s": self._poll_interval,
                "external_enabled": self._external_enabled,
                "local_enabled": self._local_enabled,
            },
        )

    def summary(self) -> dict:
        return {
            "running": self._worker is not None and self._worker.is_alive(),
            "all_time_count": self.db.total_pins(),
            "last_activity_ts": self.db.last_pin_ts(),
            "active_pins_5m": self._active_pin_count(300),
            "preview": None,  # we don't carry one
        }

    def _active_pin_count(self, seconds: float) -> int:
        try:
            since = time.time() - seconds
            return self.db.conn.execute(
                "SELECT COUNT(*) FROM pins WHERE ts >= ? AND expires_ts >= ?",
                (since, time.time()),
            ).fetchone()[0]
        except Exception:
            return 0

    # --- API + page ----------------------------------------------------

    def api_router(self) -> APIRouter:
        return self._router

    def page_html(self) -> str | None:
        try:
            return (Path(__file__).parent / "page.html").read_text(encoding="utf-8")
        except Exception:
            log.exception("page.html read failed")
            return None

    def health_payload(self) -> dict:
        cache = self.db.cache_stats()
        try:
            gz = self.db.conn.execute("SELECT COUNT(*) FROM gazetteer").fetchone()[0]
        except Exception:
            gz = 0
        return {
            "running": self._worker is not None and self._worker.is_alive(),
            "started_ts": self._started_ts,
            "gmrs_last_id": self._gmrs_last_id,
            "op25_last_id": self._op25_last_id,
            "processed": self._processed,
            "total_pins": self.db.total_pins(),
            "last_pin_ts": self.db.last_pin_ts(),
            "gazetteer_size": gz,
            "cache_entries": cache.get("entries", 0),
            "cache_total_hits": cache.get("total_hits", 0),
            "external_enabled": self._external_enabled,
            "local_enabled": self._local_enabled,
            "poll_interval_s": self._poll_interval,
            "pin_ttl_s": self._pin_ttl,
            "last_external_lookup_ts": self._last_external_lookup_ts,
        }

    # --- worker loop ---------------------------------------------------

    def _run(self):
        # Fast first pass, then poll.
        try:
            self._poll_once()
        except Exception:
            log.exception("initial geo pass failed")
        while not self._stop.wait(self._poll_interval):
            try:
                self._poll_once()
            except Exception:
                log.exception("geo poll failed (continuing)")

    def _poll_once(self):
        # Build town list once per pass — gazetteer is small.
        try:
            towns = [r["name"].lower() for r in self.db.all_towns()]
        except Exception:
            log.exception("geo: failed to load town list")
            towns = []

        # GMRS transcripts.
        if self._gmrs_db_path.exists():
            try:
                self._poll_gmrs(towns)
            except Exception:
                log.exception("geo: gmrs poll failed")
        # OP25 transcripts.
        if self._op25_db_path.exists():
            try:
                self._poll_op25(towns)
            except Exception:
                log.exception("geo: op25 poll failed")

    def _ro_connect(self, path: Path) -> sqlite3.Connection:
        """Open another tool's sqlite read-only via URI mode=ro."""
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _poll_gmrs(self, towns: list[str]):
        conn = self._ro_connect(self._gmrs_db_path)
        try:
            rows = conn.execute(
                "SELECT id, channel, freq_hz, start_ts, end_ts, transcript "
                "FROM tx_events "
                "WHERE id > ? AND transcript IS NOT NULL "
                "AND transcript_status = 'ok' "
                "AND LENGTH(TRIM(transcript)) >= 4 "
                "ORDER BY id ASC LIMIT 200",
                (self._gmrs_last_id,),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            self._process_transcript(
                source="gmrs",
                source_call_id=row["id"],
                channel_or_tg=f"ch{row['channel']}",
                ts=row["end_ts"] or row["start_ts"],
                transcript=row["transcript"],
                towns=towns,
            )
            self._gmrs_last_id = row["id"]
        if rows:
            self._save_cursor("gmrs", self._gmrs_last_id)

    def _poll_op25(self, towns: list[str]):
        conn = self._ro_connect(self._op25_db_path)
        try:
            rows = conn.execute(
                "SELECT id, tgid, tg_name, start_ts, end_ts, transcript "
                "FROM p25_calls "
                "WHERE id > ? AND transcript IS NOT NULL "
                "AND transcript_status = 'ok' "
                "AND LENGTH(TRIM(transcript)) >= 4 "
                "ORDER BY id ASC LIMIT 200",
                (self._op25_last_id,),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            label = row["tg_name"] or str(row["tgid"])
            self._process_transcript(
                source="op25",
                source_call_id=row["id"],
                channel_or_tg=label,
                ts=row["end_ts"] or row["start_ts"],
                transcript=row["transcript"],
                towns=towns,
            )
            self._op25_last_id = row["id"]
        if rows:
            self._save_cursor("op25", self._op25_last_id)

    def _process_transcript(self, *, source: str, source_call_id: int,
                            channel_or_tg: str, ts: float | None,
                            transcript: str, towns: list[str]):
        if not transcript:
            return
        candidates = extract(transcript, town_names=towns)
        if not candidates:
            self._processed += 1
            return
        candidates = attach_town_context(candidates)
        excerpt = transcript[: self._excerpt_max].strip()
        ts = ts or time.time()
        expires_ts = ts + self._pin_ttl
        for c in candidates:
            try:
                result = self.geocoder.resolve(c)
            except Exception:
                log.exception("geo: geocoder.resolve failed for %r", c.raw_text)
                continue
            if result is None:
                continue
            if result.source == "nominatim":
                self._last_external_lookup_ts = time.time()
            # Skip duplicates from the same source call + label.
            if self.db.pin_exists_for_call(source, source_call_id, result.display_name):
                continue
            try:
                self.db.add_pin(
                    ts=ts,
                    source=source,
                    source_call_id=source_call_id,
                    channel_or_tg=channel_or_tg,
                    transcript_excerpt=excerpt,
                    lat=result.lat, lon=result.lon,
                    label=result.display_name,
                    kind=result.kind,
                    confidence=float(result.confidence),
                    source_geocoder=result.source,
                    expires_ts=expires_ts,
                    raw_match=c.raw_text,
                )
            except Exception:
                log.exception("geo: add_pin failed for %r", result.display_name)
        self._processed += 1

    # --- cursor persistence --------------------------------------------

    def _cursor_path(self, name: str) -> Path:
        return self.data_dir / f"geo_cursor_{name}.txt"

    def _load_cursor(self, name: str) -> int:
        p = self._cursor_path(name)
        try:
            if p.exists():
                return int(p.read_text(encoding="utf-8").strip())
        except Exception:
            log.warning("geo: failed to read cursor %s, starting at 0", name)
        return 0

    def _save_cursor(self, name: str, value: int) -> None:
        try:
            self._cursor_path(name).write_text(str(int(value)), encoding="utf-8")
        except Exception:
            log.exception("geo: failed to save cursor %s", name)
