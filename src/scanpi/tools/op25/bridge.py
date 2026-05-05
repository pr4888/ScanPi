"""OP25 process manager + log tailer + UDP audio capture.

Spawns `multi_rx.py` as a subprocess (OP25's P25 decoder), tails its log
for voice updates (tgid/rid/freq), and listens on the UDP audio port to
capture decoded voice into per-call WAV files.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import socket
import subprocess
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


VOICE_RE = re.compile(
    r'voice update:\s+tg\((\d+)\),\s+rid\((\d+)\),\s+freq\(([0-9.]+)\),\s+slot\((\d+)\),\s+prio\((\d+)\)'
)
# OP25 fires "releasing: tg(...), freq(...), slot(N), reason(duidN)" at end of call.
RELEASE_RE = re.compile(
    r'releasing:\s+tg\((\d+)\),\s+freq\(([0-9.]+)\),\s+slot\((\d+)\)'
)
CC_RE = re.compile(r'control channel.*freq\(([0-9.]+)\)', re.IGNORECASE)


@dataclass
class ActiveCall:
    tgid: int
    rid: int
    freq_mhz: float
    start_ts: float
    last_update_ts: float
    call_id: int | None = None
    wav: wave.Wave_write | None = None
    wav_path: Path | None = None
    samples_written: int = 0


@dataclass
class BridgeConfig:
    op25_dir: Path            # e.g. ~/op25/op25/gr-op25_repeater/apps
    config_json: str          # e.g. clmrn_cfg.json (relative to op25_dir)
    log_path: Path            # e.g. /tmp/scanpi_op25.log
    audio_dir: Path           # e.g. ~/scanpi/op25_audio
    udp_port: int = 23456     # OP25 sockaudio default
    sample_rate: int = 8000   # P25 phase-1 IMBE decoded audio
    call_timeout_s: float = 3.0   # end call if no voice-update for N seconds


class OP25Bridge:
    def __init__(
        self,
        cfg: BridgeConfig,
        on_call_open: Callable[[ActiveCall], int],
        on_call_close: Callable[[int, float, str | None], None],
        on_cc_freq: Callable[[float], None] | None = None,
    ):
        self.cfg = cfg
        self._on_open = on_call_open
        self._on_close = on_call_close
        self._on_cc_freq = on_cc_freq

        self._proc: subprocess.Popen | None = None
        self._logf = None
        self._log_thread: threading.Thread | None = None
        self._udp_thread: threading.Thread | None = None
        self._timeout_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.restart_count = 0

        self._active: dict[int, ActiveCall] = {}  # tgid -> ActiveCall (one current per TG)
        self._current_tgid: int | None = None
        self._lock = threading.Lock()

        self.cc_freq_mhz: float | None = None
        self.started_at: float | None = None

    # --- lifecycle ------------------------------------------------------

    def start(self):
        self._stop.clear()
        self.cfg.audio_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._logf = None
        self.restart_count = 0
        self._spawn_multi_rx()
        self.started_at = time.time()
        self._log_thread = threading.Thread(target=self._tail_log, name="op25-log", daemon=True)
        self._log_thread.start()
        self._udp_thread = threading.Thread(target=self._udp_loop, name="op25-udp", daemon=True)
        self._udp_thread.start()
        self._timeout_thread = threading.Thread(target=self._timeout_loop, name="op25-timeout", daemon=True)
        self._timeout_thread.start()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="op25-watchdog", daemon=True)
        self._watchdog_thread.start()
        log.info("OP25 bridge started (pid=%s, udp=%d)", self._proc.pid if self._proc else "?", self.cfg.udp_port)

    def _spawn_multi_rx(self):
        log.info("spawning multi_rx.py with config=%s", self.cfg.config_json)
        # Truncate prior log only on first spawn so restart context is kept.
        if self.restart_count == 0 and self.cfg.log_path.exists():
            self.cfg.log_path.write_text("")
        if self._logf is None or self._logf.closed:
            self._logf = open(self.cfg.log_path, "a", buffering=1)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        self._proc = subprocess.Popen(
            ["python3", "-u", "multi_rx.py", "-v", "2", "-c", self.cfg.config_json],
            cwd=str(self.cfg.op25_dir),
            stdout=self._logf, stderr=subprocess.STDOUT,
            start_new_session=True, env=env,
        )

    def stop(self, timeout: float = 3.0):
        self._stop.set()
        # Close any open calls first
        with self._lock:
            for ac in list(self._active.values()):
                self._finalize_call_locked(ac, time.time())
            self._active.clear()
            self._current_tgid = None

        if self._proc is not None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
            try:
                self._proc.wait(timeout=timeout)
            except Exception:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except Exception:
                    pass
            self._proc = None

        for t in (self._log_thread, self._udp_thread, self._timeout_thread, self._watchdog_thread):
            if t is not None:
                t.join(timeout=1.0)
        self._log_thread = self._udp_thread = self._timeout_thread = self._watchdog_thread = None
        if self._logf and not self._logf.closed:
            try: self._logf.close()
            except Exception: pass
        self.started_at = None
        log.info("OP25 bridge stopped")

    # --- log tailer -----------------------------------------------------

    def _tail_log(self):
        """Follow /tmp/scanpi_op25.log, parsing voice updates + CC freq."""
        path = self.cfg.log_path
        # Wait for file to exist
        while not self._stop.is_set() and not path.exists():
            time.sleep(0.2)
        try:
            f = open(path, "r")
        except Exception:
            log.exception("could not open OP25 log")
            return
        f.seek(0, 2)  # tail mode
        while not self._stop.is_set():
            line = f.readline()
            if not line:
                time.sleep(0.05)
                continue
            # Voice update
            m = VOICE_RE.search(line)
            if m:
                tgid, rid, freq, _slot, _prio = m.groups()
                self._handle_voice(int(tgid), int(rid), float(freq))
                continue
            # Release (end of call) — OP25 fires this ~100ms after the voice
            # actually stops. Use the current time as the authoritative end_ts.
            rel = RELEASE_RE.search(line)
            if rel:
                tgid = int(rel.group(1))
                self._handle_release(tgid)
                continue
            # Control channel
            cc = CC_RE.search(line)
            if cc:
                try:
                    mhz = float(cc.group(1)) / 1e6 if float(cc.group(1)) > 1e6 else float(cc.group(1))
                except Exception:
                    mhz = None
                if mhz and mhz != self.cc_freq_mhz:
                    self.cc_freq_mhz = mhz
                    if self._on_cc_freq:
                        try:
                            self._on_cc_freq(mhz)
                        except Exception:
                            pass
        try:
            f.close()
        except Exception:
            pass

    def _handle_voice(self, tgid: int, rid: int, freq_hz_or_mhz: float):
        # multi_rx logs freq either as Hz (large) or MHz — normalize to MHz.
        freq_mhz = freq_hz_or_mhz / 1e6 if freq_hz_or_mhz > 1e6 else freq_hz_or_mhz
        now = time.time()
        with self._lock:
            self._current_tgid = tgid
            ac = self._active.get(tgid)
            if ac is None:
                ac = ActiveCall(tgid=tgid, rid=rid, freq_mhz=freq_mhz,
                                 start_ts=now, last_update_ts=now)
                self._active[tgid] = ac
                # Open WAV file
                from datetime import datetime
                date_str = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
                time_str = datetime.fromtimestamp(now).strftime("%H%M%S")
                p = self.cfg.audio_dir / date_str / f"tg_{tgid:05d}" / f"tg{tgid:05d}_{time_str}_{int(now)}.wav"
                p.parent.mkdir(parents=True, exist_ok=True)
                try:
                    ac.wav = wave.open(str(p), "wb")
                    ac.wav.setnchannels(1)
                    ac.wav.setsampwidth(2)
                    ac.wav.setframerate(self.cfg.sample_rate)
                    ac.wav_path = p
                except Exception:
                    log.exception("failed to open WAV for tgid=%d", tgid)
                    ac.wav = None
                # Open DB row
                try:
                    ac.call_id = self._on_open(ac)
                except Exception:
                    log.exception("on_call_open failed")
            else:
                ac.last_update_ts = now
                if rid:
                    ac.rid = rid

    def _handle_release(self, tgid: int):
        """OP25 'releasing:' line — finalize this call immediately."""
        now = time.time()
        with self._lock:
            ac = self._active.get(tgid)
            if ac is None:
                return
            # Update last_update_ts to 'now' so the WAV reflects the tail
            # audio that may still be flushing.
            ac.last_update_ts = now
            self._finalize_call_locked(ac, now)
            self._active.pop(tgid, None)
            if self._current_tgid == tgid:
                self._current_tgid = None

    # --- UDP audio ------------------------------------------------------

    def _udp_loop(self):
        """Receive 8 kHz 16-bit PCM frames from OP25 sockaudio and write to the
        currently-active call's WAV.
        """
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.cfg.udp_port))
            sock.settimeout(0.3)
        except Exception:
            log.exception("UDP bind failed on port %d", self.cfg.udp_port)
            return

        while not self._stop.is_set():
            try:
                data, _ = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except Exception:
                continue
            if not data:
                continue
            with self._lock:
                tg = self._current_tgid
                if tg is None:
                    continue
                ac = self._active.get(tg)
                if ac is None or ac.wav is None:
                    continue
                try:
                    ac.wav.writeframes(data)
                    ac.samples_written += len(data) // 2
                except Exception:
                    log.exception("wav write failed for tgid=%d", tg)
        try:
            sock.close()
        except Exception:
            pass

    # --- call timeout / finalization -----------------------------------

    def _watchdog_loop(self):
        """Restart multi_rx.py if it dies unexpectedly.

        Crash loops (restart <30 s apart 3x) are disabled to prevent runaway.
        """
        last_restart_ts = time.time()
        consecutive_fast_restarts = 0
        while not self._stop.wait(2.0):
            if self._proc is None:
                continue
            ret = self._proc.poll()
            if ret is None:
                continue
            # Process has exited.
            now = time.time()
            if now - last_restart_ts < 30:
                consecutive_fast_restarts += 1
            else:
                consecutive_fast_restarts = 0
            if consecutive_fast_restarts >= 3:
                log.error("multi_rx.py crashed 3x in rapid succession — giving up (restart the tool manually)")
                return
            self.restart_count += 1
            last_restart_ts = now
            log.warning("multi_rx.py exited with code %s — respawning (restart #%d)",
                        ret, self.restart_count)
            try:
                self._spawn_multi_rx()
            except Exception:
                log.exception("respawn failed")
                return

    def _timeout_loop(self):
        """Safety net — finalize any call that hasn't seen voice-update in N
        seconds, even if OP25 didn't emit a 'releasing:' line.
        """
        while not self._stop.wait(0.5):
            now = time.time()
            to_close = []
            with self._lock:
                for tg, ac in list(self._active.items()):
                    if now - ac.last_update_ts > self.cfg.call_timeout_s:
                        to_close.append((tg, ac))
                for tg, ac in to_close:
                    # Use last_update_ts + a small tail so reported duration
                    # reflects the actual voice window. If no multi-update
                    # calls, this gives at least ~0.5s duration.
                    self._finalize_call_locked(ac, max(ac.last_update_ts + 0.5, ac.start_ts + 0.5))
                    self._active.pop(tg, None)
                    if self._current_tgid == tg:
                        self._current_tgid = None

    def _finalize_call_locked(self, ac: ActiveCall, end_ts: float):
        try:
            if ac.wav is not None:
                ac.wav.close()
                ac.wav = None
        except Exception:
            log.exception("wav close failed for tgid=%d", ac.tgid)
        path = str(ac.wav_path) if ac.wav_path else None
        if ac.call_id is not None:
            try:
                self._on_close(ac.call_id, end_ts, path)
            except Exception:
                log.exception("on_call_close failed")

    # --- introspection --------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            active = [{
                "tgid": ac.tgid, "rid": ac.rid, "freq_mhz": ac.freq_mhz,
                "start_ts": ac.start_ts, "elapsed_s": round(time.time() - ac.start_ts, 1),
            } for ac in self._active.values()]
            return {
                "running": self._proc is not None and self._proc.poll() is None,
                "pid": self._proc.pid if self._proc else None,
                "started_at": self.started_at,
                "cc_freq_mhz": self.cc_freq_mhz,
                "active_calls": active,
                "restart_count": self.restart_count,
            }
