"""SQLite store for the GEO tool.

Three tables:
  - gazetteer:  pre-seeded place index (towns, routes, streets, landmarks)
  - cache:      Nominatim response cache (indefinite, by query string)
  - pins:       historical geo-pinned transcript references

Migrations are idempotent — `connect()` is safe to call repeatedly.
"""
from __future__ import annotations

import csv
import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS gazetteer (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    name_lower  TEXT NOT NULL,
    kind        TEXT NOT NULL,           -- town | street | route | landmark
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    town        TEXT,                    -- containing/parent town
    source      TEXT                     -- seed | nominatim | manual
);
CREATE INDEX IF NOT EXISTS idx_gz_name_lower ON gazetteer(name_lower);
CREATE INDEX IF NOT EXISTS idx_gz_kind       ON gazetteer(kind);
CREATE INDEX IF NOT EXISTS idx_gz_town       ON gazetteer(town);

CREATE TABLE IF NOT EXISTS cache (
    query         TEXT PRIMARY KEY,
    response_json TEXT,                  -- raw JSON response (whatever provider gave us)
    ts            REAL NOT NULL,
    hits          INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pins (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  REAL NOT NULL,
    source              TEXT NOT NULL,    -- 'gmrs' | 'op25'
    source_call_id      INTEGER,          -- foreign id into gmrs.tx_events / op25.p25_calls
    channel_or_tg       TEXT,             -- e.g. 'ch5' or '8851' (TGID)
    transcript_excerpt  TEXT,
    lat                 REAL,
    lon                 REAL,
    label               TEXT,             -- display name (street/town/route)
    kind                TEXT,             -- street | town | route | intersection | landmark
    confidence          REAL,             -- 0.0 - 1.0
    source_geocoder     TEXT,             -- cache | nominatim | local | gazetteer
    expires_ts          REAL,             -- pin "live" expiry; history retained forever
    raw_match           TEXT              -- original matched text from transcript
);
CREATE INDEX IF NOT EXISTS idx_pins_ts          ON pins(ts);
CREATE INDEX IF NOT EXISTS idx_pins_expires     ON pins(expires_ts);
CREATE INDEX IF NOT EXISTS idx_pins_source      ON pins(source);
CREATE INDEX IF NOT EXISTS idx_pins_source_call ON pins(source, source_call_id);
"""


class GeoDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self):
        self._conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        # Idempotent migrations — add columns if upgrading older DB.
        self._migrate("pins", "raw_match", "TEXT")
        self._migrate("pins", "source_geocoder", "TEXT")

    def _migrate(self, table: str, column: str, decl: str):
        cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if not self._conn:
            raise RuntimeError("GeoDB not connected")
        return self._conn

    # --- gazetteer ------------------------------------------------------

    def seed_from_csv(self, towns_csv: Path, streets_csv: Path) -> int:
        """Load seed CSVs into gazetteer if it's empty. Returns rows added."""
        existing = self.conn.execute(
            "SELECT COUNT(*) FROM gazetteer WHERE source = 'seed'"
        ).fetchone()[0]
        if existing > 0:
            return 0
        added = 0
        if towns_csv.exists():
            with open(towns_csv, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self.add_place(
                        name=row["name"],
                        kind=row.get("kind", "town"),
                        lat=float(row["lat"]),
                        lon=float(row["lon"]),
                        town=row.get("town") or row["name"],
                        source="seed",
                    )
                    added += 1
        if streets_csv.exists():
            with open(streets_csv, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self.add_place(
                        name=row["name"],
                        kind=row.get("kind", "street"),
                        lat=float(row["lat"]),
                        lon=float(row["lon"]),
                        town=row.get("town", ""),
                        source="seed",
                    )
                    added += 1
        log.info("geo gazetteer seeded with %d places", added)
        return added

    def add_place(self, name: str, kind: str, lat: float, lon: float,
                  town: str | None = None, source: str = "manual") -> int:
        cur = self.conn.execute(
            "INSERT INTO gazetteer (name, name_lower, kind, lat, lon, town, source) "
            "VALUES (?,?,?,?,?,?,?)",
            (name, name.lower().strip(), kind, lat, lon, town or "", source),
        )
        return cur.lastrowid

    def find_place(self, name: str, town: str | None = None,
                   kind: str | None = None) -> dict | None:
        """Find best gazetteer match. Town-disambiguated if provided."""
        n = name.lower().strip()
        if not n:
            return None
        sql = "SELECT * FROM gazetteer WHERE name_lower = ?"
        params: list = [n]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if town:
            sql += " AND LOWER(town) = ?"
            params.append(town.lower().strip())
        row = self.conn.execute(sql + " LIMIT 1", params).fetchone()
        if row:
            return dict(row)
        # Fallback: name only without town filter
        if town:
            row = self.conn.execute(
                "SELECT * FROM gazetteer WHERE name_lower = ? LIMIT 1", (n,),
            ).fetchone()
            return dict(row) if row else None
        return None

    def search_places(self, q: str, limit: int = 25) -> list[dict]:
        """Prefix/substring match for autocomplete-style searches."""
        like = f"%{q.lower().strip()}%"
        rows = self.conn.execute(
            "SELECT * FROM gazetteer WHERE name_lower LIKE ? "
            "ORDER BY (name_lower = ?) DESC, kind, name LIMIT ?",
            (like, q.lower().strip(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_towns(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM gazetteer WHERE kind = 'town' ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- cache ----------------------------------------------------------

    def cache_get(self, query: str) -> dict | None:
        row = self.conn.execute(
            "SELECT response_json, ts, hits FROM cache WHERE query = ?", (query,),
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["response_json"]) if row["response_json"] else None
        except json.JSONDecodeError:
            return None
        # Bump hit counter (best-effort).
        try:
            self.conn.execute(
                "UPDATE cache SET hits = hits + 1 WHERE query = ?", (query,),
            )
        except Exception:
            pass
        return {"payload": payload, "ts": row["ts"]}

    def cache_put(self, query: str, payload: dict | list | None):
        self.conn.execute(
            "INSERT OR REPLACE INTO cache (query, response_json, ts, hits) "
            "VALUES (?, ?, ?, COALESCE((SELECT hits FROM cache WHERE query = ?), 0))",
            (query, json.dumps(payload) if payload is not None else None, time.time(), query),
        )

    def cache_stats(self) -> dict:
        n = self.conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        hits = self.conn.execute("SELECT COALESCE(SUM(hits), 0) FROM cache").fetchone()[0]
        return {"entries": n, "total_hits": hits}

    # --- pins -----------------------------------------------------------

    def add_pin(self, *, ts: float, source: str, source_call_id: int | None,
                channel_or_tg: str, transcript_excerpt: str,
                lat: float, lon: float, label: str, kind: str,
                confidence: float, source_geocoder: str,
                expires_ts: float, raw_match: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO pins (ts, source, source_call_id, channel_or_tg, "
            "transcript_excerpt, lat, lon, label, kind, confidence, "
            "source_geocoder, expires_ts, raw_match) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, source, source_call_id, channel_or_tg, transcript_excerpt,
             lat, lon, label, kind, confidence, source_geocoder, expires_ts,
             raw_match),
        )
        return cur.lastrowid

    def pins_since(self, since_ts: float, until_ts: float | None = None,
                   kind: str | None = None, only_live: bool = False,
                   min_confidence: float = 0.0,
                   limit: int = 1000) -> list[dict]:
        sql = "SELECT * FROM pins WHERE ts >= ?"
        params: list = [since_ts]
        if until_ts is not None:
            sql += " AND ts <= ?"
            params.append(until_ts)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if only_live:
            sql += " AND expires_ts >= ?"
            params.append(time.time())
        if min_confidence > 0:
            sql += " AND confidence >= ?"
            params.append(min_confidence)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def pin_exists_for_call(self, source: str, source_call_id: int,
                             label: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM pins WHERE source = ? AND source_call_id = ? "
            "AND label = ? LIMIT 1",
            (source, source_call_id, label),
        ).fetchone()
        return row is not None

    def get_pin(self, pin_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM pins WHERE id = ?", (pin_id,),
        ).fetchone()
        return dict(row) if row else None

    def total_pins(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM pins").fetchone()[0]

    def last_pin_ts(self) -> float | None:
        row = self.conn.execute("SELECT MAX(ts) AS t FROM pins").fetchone()
        return row["t"] if row and row["t"] else None
