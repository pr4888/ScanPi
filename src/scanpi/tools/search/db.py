"""SearchDB — owns search.db with an FTS5 virtual table over gmrs/op25 transcripts.

Design notes:
  * gmrs.db and op25.db are ATTACHed read-only via sqlite3 URI mode=ro. We never
    write to them.
  * We do NOT use cross-DB triggers (fragile, and triggers don't fire on attached
    read-only DBs anyway). Instead a sync thread polls max(rowid) per source
    every ~10s and inserts new rows into our own FTS5 table.
  * Embeddings (if enabled) live in this same DB as 384-dim float32 BLOBs in a
    plain table — sqlite-vss is finicky on ARM/Pi 5, and 50k vectors * 384 * 4B
    is only ~75 MB. Numpy cosine on a memory-mapped array is fast enough for
    interactive search at that scale.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Sequence

log = logging.getLogger(__name__)


# --- Schema ---------------------------------------------------------------
# `fts_calls` is the FTS5 virtual table. Two helper "shadow" tables track:
#   - watermarks: the last source rowid we copied per source
#   - meta:       per-FTS-row metadata (source, source_id, ts, channel/tg, audio_id)
#   - embeddings: 384-dim float32 vector BLOBs keyed to fts_meta.id

SCHEMA_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- FTS5 virtual table over the transcript text column only.
-- We keep the transcript content here and other metadata in fts_meta.
CREATE VIRTUAL TABLE IF NOT EXISTS fts_calls USING fts5(
    transcript,
    tokenize='porter unicode61 remove_diacritics 2'
);

-- Per-row metadata — joined to fts_calls by rowid.
CREATE TABLE IF NOT EXISTS fts_meta (
    id            INTEGER PRIMARY KEY,        -- mirrors fts_calls.rowid
    source        TEXT    NOT NULL,           -- 'gmrs' | 'op25'
    source_id     INTEGER NOT NULL,           -- tx_events.id or p25_calls.id
    ts            REAL    NOT NULL,           -- start_ts
    channel_or_tg TEXT,                        -- "Ch 16" or "TG 8851"
    label         TEXT,                        -- tg_name for op25, channel# for gmrs
    audio_url     TEXT,                        -- API URL for clip
    duration_s    REAL,
    indexed_ts    REAL    NOT NULL,
    embedded_ts   REAL,                        -- when embedding was generated, NULL if not
    UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_fts_meta_source_id  ON fts_meta(source, source_id);
CREATE INDEX IF NOT EXISTS idx_fts_meta_ts         ON fts_meta(ts);
CREATE INDEX IF NOT EXISTS idx_fts_meta_embed      ON fts_meta(embedded_ts);

-- Watermarks: the largest source rowid we've already imported per source.
CREATE TABLE IF NOT EXISTS sync_watermarks (
    source     TEXT PRIMARY KEY,
    last_id    INTEGER NOT NULL DEFAULT 0,
    last_sync  REAL    NOT NULL DEFAULT 0
);

-- Embeddings: 384-dim float32 (1536 bytes per row) keyed to fts_meta.id.
CREATE TABLE IF NOT EXISTS embeddings (
    fts_id     INTEGER PRIMARY KEY,
    vec        BLOB    NOT NULL,        -- np.float32 raw bytes, 384 dims
    dim        INTEGER NOT NULL DEFAULT 384,
    model      TEXT    NOT NULL,
    created_ts REAL    NOT NULL
);
"""


# Source-DB attach paths use sqlite3's URI mode for read-only.
def _ro_uri(path: Path) -> str:
    p = str(path).replace("\\", "/")
    return f"file:{p}?mode=ro"


class SearchDB:
    """Connection wrapper. Keeps source DBs ATTACHed read-only."""

    def __init__(
        self,
        path: Path,
        gmrs_db_path: Path,
        op25_db_path: Path,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.gmrs_db_path = Path(gmrs_db_path)
        self.op25_db_path = Path(op25_db_path)
        self._conn: sqlite3.Connection | None = None
        self._gmrs_attached = False
        self._op25_attached = False

    def connect(self):
        # Main connection — owns search.db, ATTACHes the source DBs read-only.
        # uri=True is required so ATTACH DATABASE 'file:...?mode=ro' is honored.
        main_uri = "file:" + str(self.path).replace("\\", "/")
        self._conn = sqlite3.connect(
            main_uri,
            isolation_level=None,
            check_same_thread=False,
            uri=True,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_DDL)

        # Seed watermarks
        self._conn.execute(
            "INSERT OR IGNORE INTO sync_watermarks(source,last_id,last_sync) VALUES (?,0,0)",
            ("gmrs",),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO sync_watermarks(source,last_id,last_sync) VALUES (?,0,0)",
            ("op25",),
        )

        self._reattach()
        log.info(
            "SearchDB connected — fts=%d gmrs_attached=%s op25_attached=%s",
            self.row_counts()["fts"], self._gmrs_attached, self._op25_attached,
        )

    def close(self):
        if self._conn is not None:
            try:
                if self._gmrs_attached:
                    self._conn.execute("DETACH DATABASE gmrs_src")
                if self._op25_attached:
                    self._conn.execute("DETACH DATABASE op25_src")
            except Exception:
                pass
            self._conn.close()
            self._conn = None

    # Re-attach — silently no-ops if a source DB doesn't exist yet.
    def _reattach(self):
        assert self._conn is not None
        # Detach first (in case start() is called twice)
        try:
            self._conn.execute("DETACH DATABASE gmrs_src")
        except sqlite3.OperationalError:
            pass
        self._gmrs_attached = False
        try:
            self._conn.execute("DETACH DATABASE op25_src")
        except sqlite3.OperationalError:
            pass
        self._op25_attached = False

        if self.gmrs_db_path.exists():
            try:
                self._conn.execute(
                    "ATTACH DATABASE ? AS gmrs_src", (_ro_uri(self.gmrs_db_path),),
                )
                self._gmrs_attached = True
            except sqlite3.OperationalError as e:
                log.warning("could not attach gmrs.db read-only: %s", e)
        if self.op25_db_path.exists():
            try:
                self._conn.execute(
                    "ATTACH DATABASE ? AS op25_src", (_ro_uri(self.op25_db_path),),
                )
                self._op25_attached = True
            except sqlite3.OperationalError as e:
                log.warning("could not attach op25.db read-only: %s", e)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SearchDB not connected")
        return self._conn

    # --- Sync -------------------------------------------------------------

    def sync_fts(self) -> int:
        """Import new transcribed rows from each source DB into FTS5.

        Returns the number of rows added across both sources.
        Re-attaches source DBs if they appeared since last call (e.g., the
        GMRS tool was started after the search tool).
        """
        added = 0
        # If a source DB was missing at start, retry attach each cycle.
        if (not self._gmrs_attached and self.gmrs_db_path.exists()) or \
           (not self._op25_attached and self.op25_db_path.exists()):
            self._reattach()

        if self._gmrs_attached:
            added += self._sync_gmrs()
        if self._op25_attached:
            added += self._sync_op25()
        return added

    def _last_id(self, source: str) -> int:
        row = self.conn.execute(
            "SELECT last_id FROM sync_watermarks WHERE source = ?", (source,),
        ).fetchone()
        return int(row["last_id"]) if row else 0

    def _bump_watermark(self, source: str, new_last_id: int):
        self.conn.execute(
            "UPDATE sync_watermarks SET last_id = ?, last_sync = ? WHERE source = ?",
            (new_last_id, time.time(), source),
        )

    def _sync_gmrs(self) -> int:
        last = self._last_id("gmrs")
        rows = self.conn.execute(
            """SELECT id, channel, freq_hz, start_ts, end_ts, duration_s,
                      transcript, transcript_status
               FROM gmrs_src.tx_events
               WHERE id > ? AND transcript IS NOT NULL
                 AND TRIM(transcript) != ''
                 AND COALESCE(transcript_status, 'ok') = 'ok'
               ORDER BY id ASC LIMIT 500""",
            (last,),
        ).fetchall()
        if not rows:
            # Even when no transcribed rows, advance watermark past unfinished rows
            # so we don't re-scan thousands of pending rows on every cycle.
            row = self.conn.execute(
                "SELECT MAX(id) AS m FROM gmrs_src.tx_events WHERE id > ?",
                (last,),
            ).fetchone()
            if row and row["m"]:
                # Don't actually advance — we want to pick these rows up later
                # once they're transcribed. Just return 0.
                pass
            return 0

        added = 0
        max_seen = last
        for r in rows:
            ts = r["start_ts"]
            ch = r["channel"]
            label = f"Ch {ch:02d}" if ch is not None else ""
            audio_url = f"/tools/gmrs/api/clip/{r['id']}"
            try:
                self._insert_fts(
                    source="gmrs",
                    source_id=int(r["id"]),
                    ts=float(ts),
                    transcript=r["transcript"] or "",
                    channel_or_tg=label,
                    label=label,
                    audio_url=audio_url,
                    duration_s=r["duration_s"],
                )
                added += 1
            except Exception:
                log.exception("FTS insert failed gmrs id=%s", r["id"])
            if r["id"] > max_seen:
                max_seen = r["id"]
        if max_seen > last:
            self._bump_watermark("gmrs", max_seen)
        return added

    def _sync_op25(self) -> int:
        last = self._last_id("op25")
        rows = self.conn.execute(
            """SELECT id, tgid, tg_name, category, freq_mhz, start_ts, end_ts,
                      duration_s, transcript, transcript_status
               FROM op25_src.p25_calls
               WHERE id > ? AND transcript IS NOT NULL
                 AND TRIM(transcript) != ''
                 AND COALESCE(transcript_status, 'ok') = 'ok'
               ORDER BY id ASC LIMIT 500""",
            (last,),
        ).fetchall()
        if not rows:
            return 0
        added = 0
        max_seen = last
        for r in rows:
            ts = r["start_ts"]
            tg_name = r["tg_name"] or f"TG-{r['tgid']}"
            channel_or_tg = f"TG {r['tgid']}"
            audio_url = f"/tools/op25/api/clip/{r['id']}"
            try:
                self._insert_fts(
                    source="op25",
                    source_id=int(r["id"]),
                    ts=float(ts),
                    transcript=r["transcript"] or "",
                    channel_or_tg=channel_or_tg,
                    label=tg_name,
                    audio_url=audio_url,
                    duration_s=r["duration_s"],
                )
                added += 1
            except Exception:
                log.exception("FTS insert failed op25 id=%s", r["id"])
            if r["id"] > max_seen:
                max_seen = r["id"]
        if max_seen > last:
            self._bump_watermark("op25", max_seen)
        return added

    def _insert_fts(
        self,
        source: str,
        source_id: int,
        ts: float,
        transcript: str,
        channel_or_tg: str,
        label: str,
        audio_url: str,
        duration_s: float | None,
    ):
        # Idempotent — fts_meta has UNIQUE(source, source_id).
        existing = self.conn.execute(
            "SELECT id FROM fts_meta WHERE source = ? AND source_id = ?",
            (source, source_id),
        ).fetchone()
        if existing:
            return
        # Insert FTS5 row first to get a rowid
        cur = self.conn.execute(
            "INSERT INTO fts_calls(transcript) VALUES (?)", (transcript,),
        )
        rowid = cur.lastrowid
        # Mirror metadata into fts_meta with the same id
        self.conn.execute(
            "INSERT INTO fts_meta(id, source, source_id, ts, channel_or_tg, label, "
            "audio_url, duration_s, indexed_ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (rowid, source, source_id, ts, channel_or_tg, label, audio_url,
             duration_s, time.time()),
        )

    # --- Search -----------------------------------------------------------

    def search_fts(
        self,
        query: str,
        since_ts: float = 0.0,
        source: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """FTS5 MATCH search. Returns scored results, newest within score group."""
        q = (query or "").strip()
        if not q:
            return []
        # Sanitize the query for FTS5 — escape double quotes by quoting each token.
        # FTS5 syntax: prefix matches use `term*`, phrases use "term term".
        safe = self._sanitize_fts(q)
        params: list = [safe]
        where = ["fts_calls MATCH ?"]
        if since_ts > 0:
            where.append("m.ts >= ?")
            params.append(since_ts)
        if source and source != "all":
            where.append("m.source = ?")
            params.append(source)
        where_sql = " AND ".join(where)
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT m.id, m.source, m.source_id, m.ts, m.channel_or_tg,
                       m.label, m.audio_url, m.duration_s,
                       snippet(fts_calls, 0, '<mark>', '</mark>', '...', 24) AS snippet,
                       fts_calls.transcript AS transcript,
                       bm25(fts_calls) AS score
                FROM fts_calls
                JOIN fts_meta m ON m.id = fts_calls.rowid
                WHERE {where_sql}
                ORDER BY score ASC, m.ts DESC
                LIMIT ?""",
            params,
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"],
                "source": r["source"],
                "source_id": r["source_id"],
                "ts": r["ts"],
                "channel_or_tg": r["channel_or_tg"],
                "label": r["label"],
                "transcript": r["transcript"],
                "snippet": r["snippet"],
                "audio_url": r["audio_url"],
                "duration_s": r["duration_s"],
                # bm25 returns a "lower is better" score; flip sign so callers
                # can mix consistently with cosine (higher = better).
                "score": -float(r["score"]) if r["score"] is not None else 0.0,
                "match_type": "lexical",
            })
        return out

    @staticmethod
    def _sanitize_fts(q: str) -> str:
        """Take a user query and turn it into an FTS5 expression.

        - Quoted phrases ("foo bar") preserved.
        - Bare tokens get prefix-match (* suffix) so partial words work.
        - Anything FTS5 might choke on (single quote, weird operators) is stripped.
        """
        q = q.strip()
        if not q:
            return q
        # If the user wrote a quoted phrase, trust them.
        if '"' in q and q.count('"') % 2 == 0:
            return q
        # Otherwise tokenize on whitespace, drop non-word chars, prefix each.
        import re
        tokens = re.findall(r"[A-Za-z0-9_]{2,}", q)
        if not tokens:
            return '""'  # nothing left → empty match
        return " ".join(f"{t}*" for t in tokens)

    def fetch_meta(self, fts_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT m.*, fts_calls.transcript AS transcript "
            "FROM fts_meta m JOIN fts_calls ON fts_calls.rowid = m.id "
            "WHERE m.id = ?", (fts_id,),
        ).fetchone()
        return dict(row) if row else None

    def fetch_meta_by_source(self, source: str, source_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT m.*, fts_calls.transcript AS transcript "
            "FROM fts_meta m JOIN fts_calls ON fts_calls.rowid = m.id "
            "WHERE m.source = ? AND m.source_id = ?", (source, source_id),
        ).fetchone()
        return dict(row) if row else None

    # --- Embeddings -------------------------------------------------------

    def pending_embedding_ids(self, limit: int = 200) -> list[tuple[int, str]]:
        """fts_meta rows that don't have an embedding yet, newest first."""
        rows = self.conn.execute(
            "SELECT m.id, fts_calls.transcript "
            "FROM fts_meta m JOIN fts_calls ON fts_calls.rowid = m.id "
            "WHERE m.embedded_ts IS NULL "
            "ORDER BY m.ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(int(r["id"]), r["transcript"] or "") for r in rows]

    def store_embedding(self, fts_id: int, vec_bytes: bytes, dim: int, model: str):
        now = time.time()
        self.conn.execute(
            "INSERT OR REPLACE INTO embeddings(fts_id, vec, dim, model, created_ts) "
            "VALUES (?,?,?,?,?)",
            (fts_id, vec_bytes, dim, model, now),
        )
        self.conn.execute(
            "UPDATE fts_meta SET embedded_ts = ? WHERE id = ?",
            (now, fts_id),
        )

    def fetch_all_embeddings(self) -> Iterable[tuple[int, bytes]]:
        """Iterate every (fts_id, vec_bytes) pair. Caller decodes with numpy."""
        cur = self.conn.execute("SELECT fts_id, vec FROM embeddings")
        for row in cur:
            yield int(row["fts_id"]), bytes(row["vec"])

    def fetch_embedding(self, fts_id: int) -> bytes | None:
        row = self.conn.execute(
            "SELECT vec FROM embeddings WHERE fts_id = ?", (fts_id,),
        ).fetchone()
        return bytes(row["vec"]) if row else None

    def fetch_metas_bulk(self, fts_ids: Sequence[int]) -> dict[int, dict]:
        if not fts_ids:
            return {}
        placeholders = ",".join("?" * len(fts_ids))
        rows = self.conn.execute(
            f"SELECT m.*, fts_calls.transcript AS transcript "
            f"FROM fts_meta m JOIN fts_calls ON fts_calls.rowid = m.id "
            f"WHERE m.id IN ({placeholders})",
            list(fts_ids),
        ).fetchall()
        return {int(r["id"]): dict(r) for r in rows}

    # --- Stats ------------------------------------------------------------

    def row_counts(self) -> dict:
        out = {"fts": 0, "gmrs": 0, "op25": 0, "embeddings": 0}
        try:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM fts_meta").fetchone()
            out["fts"] = int(row["n"])
        except Exception:
            pass
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM fts_meta WHERE source='gmrs'"
            ).fetchone()
            out["gmrs"] = int(row["n"])
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM fts_meta WHERE source='op25'"
            ).fetchone()
            out["op25"] = int(row["n"])
        except Exception:
            pass
        try:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()
            out["embeddings"] = int(row["n"])
        except Exception:
            pass
        return out
