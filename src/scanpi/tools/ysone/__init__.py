"""YARD Stick One tool — ISM-band sub-1 GHz sweep + burst capture.

Complementary to the RTL-SDR tools (GMRS, OP25). The YS1 is a separate USB
device with its own sdr_device_index, so it runs concurrently with whichever
RTL-SDR tool has the SDR.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ...tools import Tool, ToolStatus
from .db import YSoneDB
from .worker import SweepConfig, YSOneWorker


# Common sub-1 GHz ISM band presets. Each: (start_hz, stop_hz, step_hz, default_mod)
BAND_PRESETS = {
    "ism-315":  (300_000_000, 348_000_000, 250_000, "ask_ook"),   # car remotes, tire sensors
    "ism-433":  (431_000_000, 435_000_000, 100_000, "ask_ook"),   # weather stations, door sensors
    "lpd433":   (433_050_000, 434_790_000,  25_000, "fsk2"),      # license-free EU/US remotes
    "ism-868":  (863_000_000, 870_000_000, 125_000, "gfsk"),      # EU LoRa, Z-Wave EU
    "ism-915":  (902_000_000, 928_000_000, 250_000, "gfsk"),      # US LoRa, Z-Wave US, weather
    "zwave-us": (908_000_000, 909_000_000,  25_000, "gfsk"),      # focused Z-Wave US
}

log = logging.getLogger(__name__)


class YardstickTool(Tool):
    id = "ysone"
    name = "ISM Sweep (YS1)"
    description = "YARD Stick One sub-1 GHz ISM-band spectrum sweep + burst capture"
    needs_sdr = True  # holds a USB device; coordinator-managed

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        cfg = self.config
        data_dir = Path(cfg.get("data_dir", Path.home() / "scanpi"))
        self._db_path = data_dir / "ysone.db"
        self._db = YSoneDB(self._db_path)
        self._db.connect()

        # Allow config "band" preset to seed start/stop/step/mod; any explicit
        # start_hz/stop_hz/etc. override the preset.
        band = cfg.get("band", "ism-915")
        preset = BAND_PRESETS.get(band, BAND_PRESETS["ism-915"])
        start, stop, step, mod = preset
        self._sweep_cfg = SweepConfig(
            start_hz=int(cfg.get("start_hz", start)),
            stop_hz=int(cfg.get("stop_hz", stop)),
            step_hz=int(cfg.get("step_hz", step)),
            bw_hz=int(cfg.get("bw_hz", 125_000)),
            baud=int(cfg.get("baud", 4800)),
            burst_threshold_dbm=float(cfg.get("burst_threshold_dbm", -70.0)),
            modulation=str(cfg.get("modulation", mod)),
        )
        self._band = band
        self._stare_freq_hz: int | None = None
        self._worker: YSOneWorker | None = None
        self._started_ts: float | None = None

    # The YS1 is a distinct USB device; advertise a non-RTL device index
    # so the coordinator treats it separately. Default "100" sits out of
    # the RTL-SDR range (0, 1, ...) so GMRS + OP25 + YSone all coexist.
    @property
    def sdr_device_index(self) -> int:
        return int(self.config.get("sdr_device", 100))

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        self._worker = YSOneWorker(
            self._sweep_cfg,
            on_spectrum=self._on_spectrum,
            on_burst=self._on_burst,
        )
        self._worker.start()
        self._started_ts = time.time()

    def stop(self) -> None:
        if self._worker is not None:
            try: self._worker.stop()
            except Exception: log.exception("YS1 worker stop failed")
            self._worker = None
        self._started_ts = None

    # --- callbacks ------------------------------------------------------

    def _on_spectrum(self, ts: float, slices: list):
        # Replace (not append) — keep latest sweep per freq; prune old rows periodically.
        try:
            self._db.conn.execute("BEGIN")
            for freq_hz, rssi_dbm in slices:
                self._db.log_spectrum(ts, freq_hz, rssi_dbm)
            self._db.conn.execute("COMMIT")
        except Exception:
            log.exception("spectrum log failed")
        # Prune spectrum rows older than 5 min — the UI only needs the latest sweep.
        try:
            self._db.prune_spectrum(keep_seconds=300)
        except Exception:
            pass

    def _on_burst(self, ts: float, freq_hz: int, rssi_dbm: float,
                   bytes_hex: str = "", guess: str = ""):
        self._db.log_burst(ts, freq_hz, rssi_dbm,
                           modulation=self._sweep_cfg.modulation,
                           bytes_hex=bytes_hex, note=guess)
        snippet = bytes_hex[:20] + ("…" if len(bytes_hex) > 20 else "")
        log.info("YS1 burst @ %.3f MHz  %.1f dBm  bytes=%s  [%s]",
                 freq_hz / 1e6, rssi_dbm, snippet or "(none)", guess)

    # --- status / summary ----------------------------------------------

    def status(self) -> ToolStatus:
        running = self._worker is not None and self._worker.running
        last = self._db.last_burst_ts()
        msg = f"sweeping {self._sweep_cfg.start_hz//1_000_000}-{self._sweep_cfg.stop_hz//1_000_000} MHz" if running else "stopped"
        if self._worker and self._worker.sweep_count:
            msg += f" · {self._worker.sweep_count} sweeps"
        return ToolStatus(
            running=running, healthy=True,
            last_activity_ts=last,
            message=msg,
            extra={
                "start_mhz": self._sweep_cfg.start_hz / 1e6,
                "stop_mhz":  self._sweep_cfg.stop_hz / 1e6,
                "step_khz":  self._sweep_cfg.step_hz / 1e3,
                "threshold_dbm": self._sweep_cfg.burst_threshold_dbm,
                "modulation": self._sweep_cfg.modulation,
                "started_ts": self._started_ts,
                "sweep_count": self._worker.sweep_count if self._worker else 0,
            },
        )

    def summary(self) -> dict:
        since = time.time() - 24 * 3600
        hist = self._db.burst_freq_histogram(hours=24)
        total = sum(h["n"] for h in hist)
        top = hist[0] if hist else None
        return {
            "running": self._worker is not None and self._worker.running,
            "total_calls_24h": total,
            "active_tgs_24h": len(hist),
            "top_tg": top["freq_hz"] if top else None,
            "top_tg_name": f"{top['freq_hz']/1e6:.3f} MHz" if top else None,
            "top_count": top["n"] if top else 0,
            "last_activity_ts": self._db.last_burst_ts(),
            "all_time_count": self._db.all_time_count(),
        }

    # --- API ------------------------------------------------------------

    def reconfigure(self, band: str | None = None, threshold_dbm: float | None = None,
                    modulation: str | None = None):
        """Change sweep config on the fly. Restarts the worker."""
        if band and band in BAND_PRESETS:
            start, stop, step, mod = BAND_PRESETS[band]
            self._sweep_cfg.start_hz = start
            self._sweep_cfg.stop_hz = stop
            self._sweep_cfg.step_hz = step
            if not modulation:
                self._sweep_cfg.modulation = mod
            self._band = band
        if threshold_dbm is not None:
            self._sweep_cfg.burst_threshold_dbm = float(threshold_dbm)
        if modulation:
            self._sweep_cfg.modulation = modulation
        # Restart worker so new config takes effect
        if self._worker is not None:
            try: self._worker.stop()
            except Exception: pass
            self._worker = YSOneWorker(
                self._sweep_cfg, on_spectrum=self._on_spectrum, on_burst=self._on_burst,
            )
            self._worker.start()

    def api_router(self):
        r = APIRouter()

        @r.get("/bands")
        def bands():
            return {"current": self._band, "presets": {
                k: {"start_mhz": v[0]/1e6, "stop_mhz": v[1]/1e6,
                    "step_khz": v[2]/1e3, "default_modulation": v[3]}
                for k, v in BAND_PRESETS.items()
            }}

        @r.post("/configure")
        def configure(body: dict):
            self.reconfigure(
                band=body.get("band"),
                threshold_dbm=body.get("threshold_dbm"),
                modulation=body.get("modulation"),
            )
            return {
                "band": self._band,
                "start_mhz": self._sweep_cfg.start_hz / 1e6,
                "stop_mhz":  self._sweep_cfg.stop_hz / 1e6,
                "threshold_dbm": self._sweep_cfg.burst_threshold_dbm,
                "modulation": self._sweep_cfg.modulation,
            }

        @r.get("/live")
        def live():
            snap = {
                "running": self._worker is not None and self._worker.running,
                "start_mhz": self._sweep_cfg.start_hz / 1e6,
                "stop_mhz":  self._sweep_cfg.stop_hz / 1e6,
                "threshold_dbm": self._sweep_cfg.burst_threshold_dbm,
                "sweep_count": self._worker.sweep_count if self._worker else 0,
                "spectrum": self._db.latest_spectrum(),
            }
            return snap

        @r.get("/bursts")
        def bursts(limit: int = 50):
            return {"bursts": self._db.recent_bursts(limit)}

        @r.get("/histogram")
        def histogram(hours: float = 24.0):
            return {"hours": hours, "bins": self._db.burst_freq_histogram(hours)}

        return r

    def page_html(self) -> str:
        return (Path(__file__).parent / "page.html").read_text(encoding="utf-8")
