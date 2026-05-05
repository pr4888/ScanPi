"""HackRF wideband channelizer tool for ScanPi.

Architecture: HackRF One samples ~8 MHz wide; a GNU Radio polyphase channelizer
splits that into M sub-channels (default 32 -> 250 kHz each), each fed to its
own NFM/AM/WFM demod chain with squelch + per-TX WAV recording. Profile
TOMLs at ``~/scanpi/profiles/sdrs/<id>.toml`` (see ``profiles.py``).

Integration in app_v3.py — drop these 3 lines into run_v3() *guarded by
try/except* because gnuradio is not importable on the dev box::

    try:
        from .tools.hackrf import HackrfTool
        registry.register(HackrfTool(config={"data_dir": str(data_dir)}))
    except Exception:
        log.warning("HackrfTool failed to register (gnuradio missing or no HackRF?); skipping")

Profile gating: the tool checks ``feature_enabled("multi_band_hackrf")``; if
disabled the tool registers itself but stays in 'idle, disabled by profile'
status and refuses to start. If the GR import fails the registration line
above raises and we fall through.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ...alerts import scan as scan_alerts
from ...notify import fire_webhook
from ...tools import Tool, ToolStatus

from .db import HackrfDB
from .flowgraph import HackrfMonitor, HackrfMonitorConfig
from .profiles import (
    Profile,
    find_default_profile,
    list_presets,
    list_user_profiles,
    load_profile,
    parse_text,
    save_profile,
    user_profiles_dir,
)

log = logging.getLogger(__name__)


# Optional profile module — may not be loaded yet during early test runs.
try:
    from ...profile import feature_enabled
except Exception:  # pragma: no cover
    def feature_enabled(key: str, default: bool = False) -> bool:  # type: ignore[override]
        env = os.environ.get(f"SCANPI_FEATURE_{key.upper()}")
        if env is not None:
            return env.strip().lower() in ("1", "true", "yes", "on")
        return default


@dataclass
class _LiveChannelState:
    name: str
    freq_hz: int
    open: bool = False
    last_rssi: float = -120.0
    peak_rssi: float = -120.0
    open_since: float | None = None
    last_event_ts: float = 0.0


class HackrfTool(Tool):
    id = "hackrf"
    name = "HackRF Wideband"
    description = "HackRF One polyphase channelizer — N-channel parallel scan with per-TX recording"
    needs_sdr = True

    @property
    def sdr_device_index(self) -> int:
        """HackRF lives in the 200-range so the SDR coordinator treats it as
        a fully separate device from RTL-SDRs (0-99) and the YS1 (100).
        Allow override via config['sdr_device'] for multi-HackRF setups
        (200, 201, ...).
        """
        return int(self.config.get("sdr_device", 200))

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        cfg = self.config
        data_dir = Path(cfg.get("data_dir", Path.home() / "scanpi"))
        self._data_dir = data_dir
        self._db_path = data_dir / "hackrf.db"
        self._audio_dir = data_dir / "hackrf_audio"
        self._audio_dir.mkdir(parents=True, exist_ok=True)

        self._db = HackrfDB(self._db_path)
        self._db.connect()

        # Profile resolution: explicit path -> first user profile -> first preset.
        prof_path = cfg.get("profile_path")
        prof: Profile | None = None
        try:
            if prof_path:
                prof = load_profile(prof_path)
            else:
                default = find_default_profile()
                if default is not None:
                    prof = load_profile(default)
        except Exception as e:
            log.warning("could not load profile (%s); HackRF tool starts idle", e)

        self._profile: Profile | None = prof
        self._monitor_cfg = HackrfMonitorConfig(
            audio_rate=int(cfg.get("audio_rate", 16_000)),
            preroll_s=float(cfg.get("preroll_s", 0.5)),
            audio_gain=float(cfg.get("audio_gain", 3.0)),
            max_record_s=float(cfg.get("max_record_s", 120.0)),
        )
        self._monitor: HackrfMonitor | None = None
        self._lock = threading.Lock()
        self._live: dict[str, _LiveChannelState] = {}
        self._open_events: dict[str, dict] = {}
        self._started_ts: float | None = None

        self._webhook_url = cfg.get("webhook_url") or os.environ.get("SCANPI_WEBHOOK_URL", "")
        self._public_base = cfg.get("public_base_url") or os.environ.get("SCANPI_PUBLIC_URL", "")

    # ----------------------------------------------------------- lifecycle

    def start(self) -> None:
        if not feature_enabled("multi_band_hackrf", default=True):
            log.info("HackRF disabled by profile (multi_band_hackrf=false); skipping start")
            return
        if self._profile is None:
            log.warning("HackRF tool has no profile loaded; cannot start")
            return
        # Cleanup orphan events from prior unclean shutdown
        cur = self._db.conn.execute(
            "UPDATE tx_events SET end_ts = start_ts, duration_s = 0 WHERE end_ts IS NULL"
        )
        if cur.rowcount:
            log.info("cleaned %d orphan tx_events from prior runs", cur.rowcount)

        self._live = {
            ch.name: _LiveChannelState(name=ch.name, freq_hz=ch.freq_hz)
            for ch in self._profile.channels
        }
        try:
            self._monitor = HackrfMonitor(
                self._profile, self._monitor_cfg,
                on_open=self._on_open,
                on_rssi=self._on_rssi,
                on_close=self._on_close,
                on_tick=self._on_tick,
            )
            self._monitor.start()
        except ImportError as e:
            log.warning("HackRF flowgraph requires gnuradio + gr-osmosdr; not available (%s)", e)
            self._monitor = None
            return
        except Exception:
            log.exception("HackRF monitor failed to start")
            self._monitor = None
            return
        self._started_ts = time.time()
        try:
            self._db.log_profile_load(
                self._profile.sdr.id,
                self._profile.sdr.center_hz,
                self._profile.sdr.sample_rate,
                self._profile.channelizer.num_chans,
                len(self._profile.channels),
                str(self._profile.source_path) if self._profile.source_path else None,
            )
        except Exception:
            log.exception("profile_log insert failed")

    def stop(self) -> None:
        if self._monitor is not None:
            try:
                self._monitor.stop()
            except Exception:
                log.exception("monitor stop failed")
            self._monitor = None
        self._open_events.clear()
        self._started_ts = None

    def status(self) -> ToolStatus:
        running = self._monitor is not None
        prof = self._profile
        if prof is None:
            return ToolStatus(running=False, healthy=False, message="no profile loaded")
        last = self._db.last_event_end_ts() if self._db else None
        live_last = max((s.last_event_ts for s in self._live.values()), default=0)
        last_combined = max(filter(None, [live_last if live_last > 0 else None, last]),
                             default=None)
        if running:
            sr_mhz = prof.sdr.sample_rate / 1e6
            msg = (f"profile {prof.sdr.id}: {len(prof.channels)} ch "
                   f"@ {prof.sdr.center_hz/1e6:.3f} MHz, sr={sr_mhz:.1f} Msps, "
                   f"M={prof.channelizer.num_chans}")
        else:
            msg = f"stopped — profile {prof.sdr.id} loaded"
        return ToolStatus(
            running=running,
            healthy=running,  # no special unhealthy state until we have live data
            last_activity_ts=last_combined,
            message=msg,
            extra={
                "profile_id": prof.sdr.id,
                "center_hz": prof.sdr.center_hz,
                "sample_rate": prof.sdr.sample_rate,
                "num_chans": prof.channelizer.num_chans,
                "channels_in_profile": len(prof.channels),
                "fake_iq": bool(prof.sdr.fake_iq_path),
                "started_ts": self._started_ts,
            },
        )

    def summary(self) -> dict:
        if not self._db:
            return {"running": False}
        since = time.time() - 24 * 3600
        stats = self._db.channel_stats(since_ts=since)
        top = stats[0] if stats else None
        return {
            "running": self._monitor is not None,
            "profile_id": self._profile.sdr.id if self._profile else None,
            "channel_count": len(self._profile.channels) if self._profile else 0,
            "center_mhz": (self._profile.sdr.center_hz / 1e6) if self._profile else None,
            "sample_rate_msps": (self._profile.sdr.sample_rate / 1e6) if self._profile else None,
            "top_channel": top["channel"] if top else None,
            "top_count": top["tx_count"] if top else 0,
            "total_tx_24h": sum(s["tx_count"] for s in stats) if stats else 0,
            "last_activity_ts": self._db.last_event_end_ts(),
            "all_time_count": self._db.all_time_count(),
        }

    # ---------------------------------------------------- monitor callbacks

    def _on_tick(self, spec, rssi: float):
        s = self._live.get(spec.name)
        if s is not None:
            s.last_rssi = rssi

    def _on_open(self, spec, ts: float, rssi: float):
        date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        time_str = datetime.fromtimestamp(ts).strftime("%H%M%S")
        safe_name = spec.name.replace("/", "_").replace(" ", "_")
        wav_path = (self._audio_dir / safe_name / date_str /
                    f"{safe_name}_{time_str}_{int(ts)}.wav")
        recorder = self._monitor.recorders.get(spec.name) if self._monitor else None
        if recorder is not None:
            try:
                recorder.start_record(str(wav_path))
            except Exception:
                log.exception("start_record failed name=%s", spec.name)
                wav_path = None
        with self._lock:
            evt_id = self._db.open_event(spec.name, spec.freq_hz, ts, rssi)
            self._open_events[spec.name] = {
                "id": evt_id, "path": str(wav_path) if wav_path else None,
            }
            s = self._live[spec.name]
            s.open = True
            s.open_since = ts
            s.last_rssi = rssi
            s.peak_rssi = rssi
        log.info("OPEN  hackrf:%-12s rssi=%6.1f dBFS freq=%.4f MHz",
                 spec.name, rssi, spec.freq_hz / 1e6)

    def _on_rssi(self, spec, rssi: float):
        with self._lock:
            s = self._live.get(spec.name)
            if s is not None:
                s.last_rssi = rssi
                if rssi > s.peak_rssi:
                    s.peak_rssi = rssi
            evt = self._open_events.get(spec.name)
            if evt:
                self._db.update_event_rssi(evt["id"], rssi)

    def _on_close(self, spec, ts: float):
        clip_path = None
        recorder = self._monitor.recorders.get(spec.name) if self._monitor else None
        if recorder is not None:
            clip_path, _ = recorder.stop_record()
        with self._lock:
            s = self._live.get(spec.name)
            if s is not None:
                s.open = False
                s.open_since = None
                s.last_event_ts = ts
                s.peak_rssi = -120.0
            evt = self._open_events.pop(spec.name, None)
            if evt:
                self._db.close_event(evt["id"], ts, clip_path=clip_path)
                # Alert/transcript hook can attach here when SEARCH agent ships
                # a transcription consumer for hackrf.db.
                if clip_path:
                    self._maybe_run_alerts(evt["id"], spec)
        log.info("CLOSE hackrf:%-12s", spec.name)

    def _maybe_run_alerts(self, event_id: int, spec):
        """Lightweight alert path: scan_alerts() on transcript when one lands.

        For now we don't run whisper inside the HackRF tool — the SEARCH /
        transcription worker is shared infra. Webhook hook is left so the
        eventual cross-tool transcript pipeline can call set_transcript() and
        we'll re-fire alerts from there.
        """
        return

    # --------------------------------------------------------------- helpers

    def _swap_profile(self, prof: Profile) -> None:
        """Stop monitor, swap profile, restart. Safe to call from API thread."""
        was_running = self._monitor is not None
        if was_running:
            self.stop()
        self._profile = prof
        if was_running:
            self.start()

    # ---------------------------------------------------------- API + page

    def api_router(self):
        from .api import build_router
        return build_router(self)

    def page_html(self) -> str | None:
        page = Path(__file__).parent / "page.html"
        return page.read_text(encoding="utf-8") if page.exists() else None

    # ------------------------------------------------ accessors used by api

    @property
    def db(self) -> HackrfDB:
        return self._db

    @property
    def profile(self) -> Profile | None:
        return self._profile

    @property
    def live(self) -> dict[str, _LiveChannelState]:
        return self._live

    @property
    def open_events(self) -> dict[str, dict]:
        return self._open_events

    @property
    def is_running(self) -> bool:
        return self._monitor is not None

    @property
    def monitor_meta(self) -> dict:
        if self._monitor is None:
            return {}
        return dict(self._monitor.meta or {})


# Re-export commonly used helpers (handy for tests + REPL)
__all__ = [
    "HackrfTool",
    "Profile",
    "load_profile",
    "parse_text",
    "save_profile",
    "list_presets",
    "list_user_profiles",
    "user_profiles_dir",
]
