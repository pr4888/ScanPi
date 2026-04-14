"""YARD Stick One worker thread — owns the USB device, runs a sweep loop,
logs RSSI per slice, and calls out when a slice crosses the burst threshold.

YS1 / CC1111 is a narrowband transceiver — not a wideband scanner. We cover
a band by stepping through channels at ~125 kHz spacing, reading RSSI per
slice. A slice above threshold triggers a short dwell (listen for packets)
before resuming the sweep.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)


def rssi_byte_to_dbm(b: bytes | int) -> float:
    """CC1111 RSSI register → dBm.

    Formula per TI CC1111 datasheet:
      rssi_dec = signed byte
      rssi_dBm = rssi_dec/2 - RSSI_OFFSET   (offset ~74 for 915 MHz band)
    """
    if isinstance(b, (bytes, bytearray)):
        if not b:
            return -120.0
        v = b[0]
    else:
        v = int(b)
    if v >= 128:
        v -= 256
    return v / 2.0 - 74.0


@dataclass
class SweepConfig:
    start_hz: int = 902_000_000
    stop_hz: int  = 928_000_000
    step_hz: int  = 250_000         # 125 kHz BW × 2 = step so slices don't overlap much
    bw_hz: int    = 125_000
    baud: int     = 4800
    burst_threshold_dbm: float = -70.0
    dwell_us_per_slice: int = 3000  # microseconds per RSSI sample slice
    modulation: str = "ask_ook"     # ask_ook | fsk2 | fsk4 | msk | gfsk


class YSOneWorker:
    """Single-threaded YS1 driver. Emits spectrum + burst events via callbacks."""

    def __init__(
        self,
        cfg: SweepConfig,
        on_spectrum: Callable[[float, list[tuple[int, float]]], None],
        on_burst: Callable[[float, int, float], None],
    ):
        self.cfg = cfg
        self._on_spectrum = on_spectrum
        self._on_burst = on_burst
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._device = None
        self.running = False
        self.last_error: str | None = None
        self.sweep_count = 0

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ysone-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None
        # Device cleanup
        try:
            if self._device is not None:
                self._device.setModeIDLE()
        except Exception:
            pass

    def _setup_device(self):
        import rflib
        d = rflib.RfCat()
        d.setModeIDLE()
        d.setFreq(self.cfg.start_hz)
        mod_map = {
            "ask_ook": rflib.MOD_ASK_OOK,
            "fsk2":    rflib.MOD_2FSK,
            "fsk4":    rflib.MOD_4FSK,
            "msk":     rflib.MOD_MSK,
            "gfsk":    rflib.MOD_GFSK,
        }
        d.setMdmModulation(mod_map.get(self.cfg.modulation, rflib.MOD_ASK_OOK))
        d.setMdmDRate(self.cfg.baud)
        d.setMdmChanBW(self.cfg.bw_hz)
        d.setMaxPower()
        self._device = d
        log.info("YS1 opened: partnum=0x%x, band=%d-%d MHz, mod=%s",
                 d.getPartNum(),
                 self.cfg.start_hz // 1_000_000,
                 self.cfg.stop_hz // 1_000_000,
                 self.cfg.modulation)

    def _run(self):
        try:
            self._setup_device()
            self.running = True
        except Exception as e:
            log.exception("YS1 setup failed")
            self.last_error = str(e)
            self.running = False
            return

        try:
            while not self._stop.is_set():
                self._one_sweep()
                self.sweep_count += 1
        finally:
            self.running = False
            try:
                if self._device is not None:
                    self._device.setModeIDLE()
            except Exception:
                pass

    def _one_sweep(self):
        freqs = list(range(self.cfg.start_hz, self.cfg.stop_hz, self.cfg.step_hz))
        slice_out: list[tuple[int, float]] = []
        now = time.time()
        for fz in freqs:
            if self._stop.is_set():
                return
            try:
                self._device.setFreq(fz)
                # Two quick reads so AGC has a moment to settle on this freq.
                self._device.getRSSI()
                raw = self._device.getRSSI()
                dbm = rssi_byte_to_dbm(raw)
            except Exception:
                log.exception("RSSI read failed at %d Hz", fz)
                continue
            slice_out.append((fz, dbm))
            if dbm > self.cfg.burst_threshold_dbm:
                try:
                    self._on_burst(now, fz, dbm)
                except Exception:
                    log.exception("on_burst handler failed")
        # Emit the full sweep in one go — UI / DB consumer can decide how to store.
        try:
            self._on_spectrum(now, slice_out)
        except Exception:
            log.exception("on_spectrum handler failed")
