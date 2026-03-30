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
        self._event_listeners: list = []

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

        # Start UDP audio receiver in a thread
        self._start_audio_receiver()

        # Load transcription model
        self._init_transcriber()

        # Run log tailer, call finalizer, and transcription loop concurrently
        await asyncio.gather(
            self._tail_log(),
            self._finalize_loop(),
            self._transcribe_loop(),
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

        # Grab any buffered audio and assign to the active talkgroup
        audio = self._grab_audio() if hasattr(self, '_audio_lock') else b""

        if tgid in self.active_calls:
            # Extend existing call
            call = self.active_calls[tgid]
            call.last_update = now
            if rid > 0:
                call.radio_id = rid
            call.freq_mhz = freq
            if audio:
                call.audio_chunks.append(audio)
        else:
            # New call — flush audio from any other active call first
            # (P25 single-channel: new TG = old TG call ended)
            for other_tgid in list(self.active_calls.keys()):
                if other_tgid != tgid:
                    old_call = self.active_calls.pop(other_tgid)
                    # Save synchronously (we're in the event loop via _process_line)
                    self._save_call_sync(old_call)

            call = ActiveCall(
                tgid=tgid, start_time=now, last_update=now,
                radio_id=rid, freq_mhz=freq,
            )
            if audio:
                call.audio_chunks.append(audio)
            self.active_calls[tgid] = call
            log.info(f"Call started: {tg.name} (TG {tgid}) on {freq:.6f} MHz")

    def emit_event(self, event_type: str, data: dict):
        """Emit an event to all registered SSE listeners."""
        for listener in self._event_listeners:
            try:
                listener(event_type, data)
            except Exception:
                pass

    def _save_call_sync(self, call: ActiveCall):
        """Synchronous version of _save_call (for use from _process_line)."""
        tg = self.get_talkgroup(call.tgid)
        audio_data = b"".join(call.audio_chunks)

        # Calculate duration from audio data length (8kHz, 16-bit mono = 16000 bytes/sec)
        if len(audio_data) > 320:
            duration = len(audio_data) / (8000 * 2)
        else:
            duration = call.last_update - call.start_time

        tg.call_count += 1
        tg.total_duration += duration

        filepath = None
        size_bytes = 0

        if len(audio_data) > 320:
            ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(call.start_time))
            safe_name = tg.name.replace("/", "-").replace(" ", "_")
            filename = f"{ts}_TG{call.tgid}_{safe_name}.wav"
            filepath = str(self.cfg.recordings_dir / filename)
            try:
                with wave.open(filepath, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(8000)
                    wf.writeframes(audio_data)
                size_bytes = Path(filepath).stat().st_size
            except Exception as e:
                log.error(f"Failed to save audio: {e}")
                filepath = None

        with self.db.cursor() as c:
            c.execute("""
                INSERT INTO calls (tgid, tg_name, tg_category, radio_id, freq_mhz,
                                   start_time, end_time, duration_s, filepath, size_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (call.tgid, tg.name, tg.category, call.radio_id, call.freq_mhz,
                  call.start_time, call.last_update, duration, filepath, size_bytes))

        if filepath:
            log.info(f"Call saved: {tg.name} ({duration:.1f}s, {size_bytes/1024:.0f}KB)")
        else:
            log.info(f"Call logged: {tg.name} ({duration:.1f}s, no audio)")

        # Emit SSE event for real-time UI updates
        self.emit_event("new_call", {
            "tgid": call.tgid,
            "tg_name": tg.name,
            "tg_category": tg.category,
            "radio_id": call.radio_id,
            "freq_mhz": call.freq_mhz,
            "duration_s": round(duration, 1),
            "has_audio": filepath is not None,
            "start_time": call.start_time,
        })

    async def _finalize_loop(self):
        """Periodically check for ended calls, grab remaining audio, and save."""
        while self._running:
            now = time.time()
            ended = []
            for tgid, call in list(self.active_calls.items()):
                if now - call.last_update > self._call_timeout:
                    ended.append(tgid)

            # Grab any remaining audio for ending calls
            if ended and hasattr(self, '_audio_lock'):
                remaining = self._grab_audio()
                if remaining and ended:
                    # Give remaining audio to the most recently active call
                    latest = max(ended, key=lambda t: self.active_calls[t].last_update)
                    self.active_calls[latest].audio_chunks.append(remaining)

            for tgid in ended:
                call = self.active_calls.pop(tgid)
                self._save_call_sync(call)

            await asyncio.sleep(1.0)

    def _start_audio_receiver(self):
        """Start UDP audio receiver in a background thread."""
        import threading
        self._audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._audio_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._audio_sock.bind(("0.0.0.0", 2345))
        self._audio_sock.settimeout(1.0)
        self._audio_buffer: list[bytes] = []
        self._audio_lock = threading.Lock()

        def recv_loop():
            while self._running:
                try:
                    data, _ = self._audio_sock.recvfrom(65535)
                    if len(data) > 2:  # skip 2-byte control packets
                        with self._audio_lock:
                            self._audio_buffer.append(data)
                except socket.timeout:
                    continue
                except Exception:
                    break

        t = threading.Thread(target=recv_loop, daemon=True)
        t.start()
        log.info("Audio receiver started on UDP 2345")

    def _grab_audio(self) -> bytes:
        """Grab all buffered audio and clear buffer."""
        import threading
        with self._audio_lock:
            if not self._audio_buffer:
                return b""
            data = b"".join(self._audio_buffer)
            self._audio_buffer.clear()
            return data

    async def _save_call(self, call: ActiveCall):
        """Save a completed call with audio to the database."""
        tg = self.get_talkgroup(call.tgid)
        duration = call.last_update - call.start_time
        tg.call_count += 1
        tg.total_duration += duration

        # Grab any buffered audio
        audio_data = b"".join(call.audio_chunks)
        filepath = None
        size_bytes = 0

        if len(audio_data) > 320:  # more than 1 packet
            # Save as WAV (8kHz 16-bit mono — P25 IMBE decoded output)
            ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(call.start_time))
            safe_name = tg.name.replace("/", "-").replace(" ", "_")
            filename = f"{ts}_TG{call.tgid}_{safe_name}.wav"
            filepath = str(self.cfg.recordings_dir / filename)

            try:
                with wave.open(filepath, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)  # 16-bit
                    wf.setframerate(8000)  # P25 audio is 8kHz
                    wf.writeframes(audio_data)
                size_bytes = Path(filepath).stat().st_size
            except Exception as e:
                log.error(f"Failed to save audio: {e}")
                filepath = None

        with self.db.cursor() as c:
            c.execute("""
                INSERT INTO calls (tgid, tg_name, tg_category, radio_id, freq_mhz,
                                   start_time, end_time, duration_s, filepath, size_bytes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (call.tgid, tg.name, tg.category, call.radio_id, call.freq_mhz,
                  call.start_time, call.last_update, duration, filepath, size_bytes))

        if filepath:
            log.info(f"Call saved: {tg.name} ({duration:.1f}s, {size_bytes/1024:.0f}KB audio)")
        else:
            log.info(f"Call saved: {tg.name} ({duration:.1f}s, no audio)")

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

    # --- Transcription ---

    def _init_transcriber(self):
        """Load faster-whisper model."""
        self._whisper = None
        try:
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel(
                "tiny.en", device="cpu", compute_type="int8", cpu_threads=2,
            )
            log.info("Whisper tiny.en model loaded for transcription")
        except ImportError:
            log.warning("faster-whisper not installed — no transcription")
        except Exception as e:
            log.error(f"Whisper init failed: {e}")

    async def _transcribe_loop(self):
        """Background loop — transcribe calls that have audio but no transcript."""
        if not self._whisper:
            return
        while self._running:
            await asyncio.sleep(10)  # check every 10s
            try:
                await self._transcribe_pending()
            except Exception as e:
                log.error(f"Transcription error: {e}")

    async def _transcribe_pending(self):
        """Find and transcribe untranscribed calls."""
        with self.db.cursor() as c:
            c.execute("""
                SELECT id, filepath, tg_name, tgid FROM calls
                WHERE transcribed = 0 AND filepath IS NOT NULL
                ORDER BY start_time DESC LIMIT 5
            """)
            pending = [dict(r) for r in c.fetchall()]

        if not pending:
            return

        loop = asyncio.get_event_loop()
        for call in pending:
            fp = call["filepath"]
            # Prefer the upsampled version
            up = fp.replace(".wav", ".48k.wav") if fp.endswith(".wav") else fp
            audio_file = up if Path(up).exists() else fp
            if not Path(audio_file).exists():
                continue

            # Run transcription in thread pool (CPU-intensive)
            text, confidence, keywords = await loop.run_in_executor(
                None, self._transcribe_file, audio_file
            )
            if text:
                with self.db.cursor() as c:
                    c.execute("""
                        UPDATE calls SET transcribed = 1, transcript = ?,
                               transcript_confidence = ?, keywords = ?
                        WHERE id = ?
                    """, (text, confidence, keywords, call["id"]))
                log.info(f"Transcribed call #{call['id']} ({call['tg_name']}): {text[:60]}...")

    def _transcribe_file(self, filepath: str) -> tuple[str, float, str]:
        """Transcribe a single file. Returns (text, confidence, keywords)."""
        import re
        try:
            segments, info = self._whisper.transcribe(
                filepath, language="en", beam_size=1, vad_filter=True,
            )
            texts = []
            total_prob = 0
            count = 0
            for seg in segments:
                t = seg.text.strip()
                if t:
                    texts.append(t)
                    total_prob += getattr(seg, 'avg_log_prob', getattr(seg, 'avg_logprob', -0.5))
                    count += 1

            if not texts:
                return "", 0, ""

            text = " ".join(texts)
            # Clean hallucinations
            for pattern in [r"thank you for watching", r"thanks for watching",
                            r"please subscribe", r"\[music\]"]:
                text = re.sub(pattern, "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s+", " ", text).strip()

            if len(text) < 3:
                return "", 0, ""

            confidence = min(1.0, max(0.0, 1.0 + (total_prob / count if count else -1)))

            # Extract alert keywords
            alert_words = ["mayday", "emergency", "fire", "accident", "rescue",
                           "shots", "shooting", "officer", "pursuit", "ambulance"]
            found = [w for w in alert_words if w.lower() in text.lower()]
            keywords = ",".join(found)

            return text, confidence, keywords

        except Exception as e:
            log.error(f"Transcribe error on {filepath}: {e}")
            return "", 0, ""

    def stop(self):
        self._running = False
