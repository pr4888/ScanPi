"""OP25 Call Bridge — captures P25 talkgroup calls from OP25 log + audio UDP.

Parses OP25 multi_rx.py log for voice updates, captures audio from UDP port,
saves per-call WAV files, and indexes them in the ScanPi database.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import struct
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

from .config import ScanConfig
from .db import ScanPiDB

log = logging.getLogger("scanpi.op25bridge")

# OP25 log line pattern: voice update:  tg(XXXXX), rid(YYYYY), freq(ZZZ.ZZZZZZ), slot(N), prio(N)
VOICE_RE = re.compile(
    r'voice update:\s+tg\((\d+)\),\s+rid\((\d+)\),\s+freq\(([0-9.]+)\),\s+slot\((\d+)\),\s+prio\((\d+)\)'
)

# Default talkgroup info
CATEGORIES = {
    "pd": "police", "police": "police", "troop": "police", "csp": "police",
    "fire": "fire", "ems": "ems", "medic": "ems", "ambulance": "ems",
    "hospital": "ems", "transit": "utility", "dpw": "utility", "utilities": "utility",
    "dispatch": "fire",  # most dispatch is fire
}

CATEGORY_COLORS = {
    "police": "#3b82f6", "fire": "#ef4444", "ems": "#f97316",
    "marine": "#06b6d4", "weather": "#22c55e", "utility": "#a855f7", "other": "#64748b",
}

CATEGORY_ICONS = {
    "police": "🔵", "fire": "🔴", "ems": "🟠",
    "utility": "🟣", "other": "⚪",
}


@dataclass
class Talkgroup:
    tgid: int
    name: str
    category: str = "other"
    color: str = "#64748b"
    priority: int = 0
    last_active: float = 0
    call_count: int = 0
    total_duration: float = 0


@dataclass
class ActiveCall:
    tgid: int
    start_time: float
    last_update: float
    radio_id: int = 0
    freq_mhz: float = 0
    audio_chunks: list = field(default_factory=list)


class OP25Bridge:
    """Bridges OP25 output into ScanPi's call database."""

    def __init__(self, cfg: ScanConfig, db: ScanPiDB,
                 op25_log: str = "/tmp/op25.log",
                 talkgroups_file: str | None = None):
        self.cfg = cfg
        self.db = db
        self.op25_log = Path(op25_log)
        self.talkgroups: dict[int, Talkgroup] = {}
        self.active_calls: dict[int, ActiveCall] = {}
        self._running = False
        self._call_timeout = 3.0  # seconds of silence = call ended

        # Load talkgroup definitions
        if talkgroups_file:
            self._load_talkgroups(Path(talkgroups_file))

    def _load_talkgroups(self, path: Path):
        """Load talkgroup TSV file (OP25 format: TGID<tab>Name<tab>Priority)."""
        if not path.exists():
            log.warning(f"Talkgroups file not found: {path}")
            return
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            tgid = int(parts[0])
            name = parts[1]
            prio = int(parts[2]) if len(parts) > 2 else 0
            cat = self._guess_category(name)
            self.talkgroups[tgid] = Talkgroup(
                tgid=tgid, name=name, category=cat,
                color=CATEGORY_COLORS.get(cat, "#64748b"),
                priority=prio,
            )
        log.info(f"Loaded {len(self.talkgroups)} talkgroups from {path}")

    def _guess_category(self, name: str) -> str:
        """Guess talkgroup category from name."""
        lower = name.lower()
        for keyword, cat in CATEGORIES.items():
            if keyword in lower:
                return cat
        return "other"

    def get_talkgroup(self, tgid: int) -> Talkgroup:
        """Get or create talkgroup info."""
        if tgid not in self.talkgroups:
            self.talkgroups[tgid] = Talkgroup(
                tgid=tgid, name=f"TG {tgid}", category="other"
            )
        return self.talkgroups[tgid]

    async def start(self):
        """Start monitoring OP25 log for voice updates."""
        self._running = True
        log.info("OP25 Bridge started — monitoring log for calls")

        # Ensure calls table exists
        self._ensure_calls_table()

        # Run log tailer and call finalizer concurrently
        await asyncio.gather(
            self._tail_log(),
            self._finalize_loop(),
        )

    def _ensure_calls_table(self):
        """Create calls table if it doesn't exist."""
        with self.db.cursor() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tgid INTEGER NOT NULL,
                    tg_name TEXT,
                    tg_category TEXT,
                    radio_id INTEGER,
                    freq_mhz REAL,
                    start_time REAL NOT NULL,
                    end_time REAL,
                    duration_s REAL,
                    filepath TEXT,
                    size_bytes INTEGER DEFAULT 0,
                    transcribed BOOLEAN DEFAULT 0,
                    transcript TEXT,
                    transcript_confidence REAL,
                    keywords TEXT
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_calls_tgid ON calls(tgid)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_calls_time ON calls(start_time DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_calls_cat ON calls(tg_category)")

    async def _tail_log(self):
        """Tail OP25 log file for voice updates."""
        # Start from end of file
        if not self.op25_log.exists():
            log.warning(f"OP25 log not found: {self.op25_log}")
            while self._running and not self.op25_log.exists():
                await asyncio.sleep(2)

        with open(self.op25_log, "r") as f:
            # Seek to end
            f.seek(0, 2)
            while self._running:
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.2)
                    continue
                self._process_line(line.strip())

    def _process_line(self, line: str):
        """Process an OP25 log line."""
        match = VOICE_RE.search(line)
        if not match:
            return

        tgid = int(match.group(1))
        rid = int(match.group(2))
        freq = float(match.group(3))
        now = time.time()

        tg = self.get_talkgroup(tgid)
        tg.last_active = now

        if tgid in self.active_calls:
            # Extend existing call
            call = self.active_calls[tgid]
            call.last_update = now
            if rid > 0:
                call.radio_id = rid
            call.freq_mhz = freq
        else:
            # New call
            self.active_calls[tgid] = ActiveCall(
                tgid=tgid, start_time=now, last_update=now,
                radio_id=rid, freq_mhz=freq,
            )
            log.info(f"Call started: {tg.name} (TG {tgid}) on {freq:.6f} MHz")

    async def _finalize_loop(self):
        """Periodically check for ended calls and save them."""
        while self._running:
            now = time.time()
            ended = []
            for tgid, call in self.active_calls.items():
                if now - call.last_update > self._call_timeout:
                    ended.append(tgid)

            for tgid in ended:
                call = self.active_calls.pop(tgid)
                await self._save_call(call)

            await asyncio.sleep(1.0)

    async def _save_call(self, call: ActiveCall):
        """Save a completed call to the database."""
        tg = self.get_talkgroup(call.tgid)
        duration = call.last_update - call.start_time
        tg.call_count += 1
        tg.total_duration += duration

        with self.db.cursor() as c:
            c.execute("""
                INSERT INTO calls (tgid, tg_name, tg_category, radio_id, freq_mhz,
                                   start_time, end_time, duration_s)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (call.tgid, tg.name, tg.category, call.radio_id, call.freq_mhz,
                  call.start_time, call.last_update, duration))

        log.info(f"Call saved: {tg.name} ({duration:.1f}s)")

    def get_recent_calls(self, limit: int = 50, tgid: int | None = None,
                         category: str | None = None) -> list[dict]:
        with self.db.cursor() as c:
            q = "SELECT * FROM calls WHERE 1=1"
            params = []
            if tgid is not None:
                q += " AND tgid = ?"
                params.append(tgid)
            if category:
                q += " AND tg_category = ?"
                params.append(category)
            q += " ORDER BY start_time DESC LIMIT ?"
            params.append(limit)
            c.execute(q, params)
            return [dict(r) for r in c.fetchall()]

    def get_talkgroup_summary(self) -> list[dict]:
        """Get all talkgroups with call counts and activity."""
        with self.db.cursor() as c:
            c.execute("""
                SELECT tgid, tg_name, tg_category,
                       COUNT(*) as call_count,
                       SUM(duration_s) as total_duration,
                       MAX(start_time) as last_call,
                       AVG(duration_s) as avg_duration
                FROM calls
                GROUP BY tgid
                ORDER BY last_call DESC
            """)
            rows = [dict(r) for r in c.fetchall()]

        # Merge with talkgroup definitions (includes ones with no calls yet)
        seen = {r["tgid"] for r in rows}
        for tgid, tg in self.talkgroups.items():
            if tgid not in seen:
                rows.append({
                    "tgid": tgid, "tg_name": tg.name, "tg_category": tg.category,
                    "call_count": 0, "total_duration": 0, "last_call": None,
                    "avg_duration": 0,
                })

        # Add colors and icons
        for r in rows:
            cat = r.get("tg_category", "other") or "other"
            r["color"] = CATEGORY_COLORS.get(cat, "#64748b")
            r["icon"] = CATEGORY_ICONS.get(cat, "⚪")
            # Merge priority from definition
            tg = self.talkgroups.get(r["tgid"])
            r["priority"] = tg.priority if tg else 0

        rows.sort(key=lambda r: (-(r.get("last_call") or 0), -r["call_count"]))
        return rows

    def get_active_calls(self) -> list[dict]:
        """Get currently active calls."""
        result = []
        for tgid, call in self.active_calls.items():
            tg = self.get_talkgroup(tgid)
            result.append({
                "tgid": tgid, "name": tg.name, "category": tg.category,
                "color": CATEGORY_COLORS.get(tg.category, "#64748b"),
                "icon": CATEGORY_ICONS.get(tg.category, "⚪"),
                "radio_id": call.radio_id, "freq_mhz": call.freq_mhz,
                "start_time": call.start_time,
                "duration": time.time() - call.start_time,
            })
        result.sort(key=lambda r: -r["start_time"])
        return result

    def stop(self):
        self._running = False
