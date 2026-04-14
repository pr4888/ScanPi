"""GNU Radio flowgraph: 15-way parallel GMRS/FRS activity monitor with per-TX recording.

Single RTL-SDR @ 462.6375 MHz, 2 Msps, tuned to the 462 block.
Covers channels 1-7 + 15-22 (15 of 22).

Per channel:
  freq_xlating_fir_filter_ccc  — decim 40 → 50 kHz sample rate, 12.5 kHz BW
  pwr_squelch_cc              — opens above threshold_db (used just to inspect)
  nbfm_rx                     — FM demod → 10 kHz audio
  AudioRecorder (custom)      — 2s preroll ring, writes WAV when armed
  probe_avg_mag_sqrd_c        — complex-baseband RMS for edge detection

A background thread polls probes at 20 Hz, detects rising/falling edges,
and fires on_open/on_rssi/on_close callbacks. The service layer arms/disarms
the AudioRecorder on each edge.
"""
from __future__ import annotations

import collections
import logging
import math
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from gnuradio import gr, blocks, analog, filter as gr_filter
from gnuradio.filter import firdes
import osmosdr

from .channels import Channel, CHANNELS_462

log = logging.getLogger(__name__)


@dataclass
class MonitorConfig:
    center_hz: int = 462_637_500
    sample_rate: int = 2_000_000
    rtl_gain: float = 40.0
    squelch_db: float = -30.0  # ~15 dB above typical RTL-SDR UHF noise floor
    squelch_alpha: float = 0.05
    # Edge detection
    open_hold_s: float = 0.3
    close_hold_s: float = 0.8
    poll_hz: float = 20.0
    # Audio recording
    audio_rate: int = 10_000  # must evenly divide channel_rate (50000/10000=5)
    preroll_s: float = 1.5    # seconds of audio buffered before squelch opens
    max_record_s: float = 120  # safety cap on any single TX


class AudioRecorder(gr.sync_block):
    """Sink block: buffers a rolling preroll and writes WAV on demand.

    Thread-safe: start_record/stop_record are called from the poll thread
    while work() runs in the GR scheduler thread. Guarded by a mutex.
    """

    def __init__(self, channel_num: int, sample_rate: int, preroll_samples: int):
        gr.sync_block.__init__(
            self, name=f"ch{channel_num}_rec",
            in_sig=[np.float32], out_sig=None,
        )
        self._ch = channel_num
        self._rate = sample_rate
        self._buf = collections.deque(maxlen=preroll_samples)
        self._lock = threading.Lock()
        self._wav: wave.Wave_write | None = None
        self._path: str | None = None
        self._samples_written = 0

    def work(self, input_items, output_items):
        samples = input_items[0]
        with self._lock:
            if self._wav is not None:
                self._wav.writeframes((samples * 32000).astype(np.int16).tobytes())
                self._samples_written += len(samples)
            else:
                self._buf.extend(samples)
        return len(samples)

    def start_record(self, path: str) -> str:
        with self._lock:
            if self._wav is not None:
                self._wav.close()
                self._wav = None
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._wav = wave.open(path, "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(self._rate)
            if self._buf:
                pre = np.fromiter(self._buf, dtype=np.float32, count=len(self._buf))
                self._wav.writeframes((pre * 32000).astype(np.int16).tobytes())
                self._buf.clear()
            self._path = path
            self._samples_written = 0
            return path

    def stop_record(self) -> tuple[str | None, float]:
        with self._lock:
            if self._wav is None:
                return None, 0.0
            self._wav.close()
            path = self._path
            dur = self._samples_written / self._rate
            self._wav = None
            self._path = None
            return path, dur


class GmrsFlowgraph(gr.top_block):
    def __init__(self, channels: list[Channel], cfg: MonitorConfig):
        super().__init__("GMRS Monitor")
        self.channels = channels
        self.cfg = cfg

        src = osmosdr.source(args="numchan=1 rtl=0")
        src.set_sample_rate(cfg.sample_rate)
        src.set_center_freq(cfg.center_hz)
        src.set_gain_mode(False)
        src.set_gain(cfg.rtl_gain)
        src.set_bandwidth(cfg.sample_rate)
        self.src = src

        decim = 40
        channel_rate = cfg.sample_rate // decim  # 50 kHz
        audio_rate = cfg.audio_rate

        ch_taps = firdes.low_pass(1.0, cfg.sample_rate, 6_250, 2_000)
        preroll_samples = int(cfg.preroll_s * audio_rate)

        self.probes: dict[int, analog.probe_avg_mag_sqrd_c] = {}
        self.recorders: dict[int, AudioRecorder] = {}

        for ch in channels:
            offset = ch.freq_hz - cfg.center_hz
            xlate = gr_filter.freq_xlating_fir_filter_ccc(
                decim, ch_taps, offset, cfg.sample_rate,
            )
            # Squelch kept optional (set very low — we use the probe for edge detect)
            squelch = analog.pwr_squelch_cc(-120.0, cfg.squelch_alpha, 0, False)
            fm = analog.nbfm_rx(
                audio_rate=audio_rate,
                quad_rate=channel_rate,
                tau=75e-6,
                max_dev=5_000,
            )
            probe = analog.probe_avg_mag_sqrd_c(cfg.squelch_db - 5.0, 0.05)
            recorder = AudioRecorder(ch.num, audio_rate, preroll_samples)

            self.connect(src, xlate, probe)
            self.connect(xlate, squelch, fm, recorder)

            self.probes[ch.num] = probe
            self.recorders[ch.num] = recorder

        log.info("Flowgraph built: %d channels, center=%.4f MHz, rate=%d Msps, preroll=%.1fs",
                 len(channels), cfg.center_hz / 1e6, cfg.sample_rate, cfg.preroll_s)


class GmrsMonitor:
    def __init__(self, cfg: MonitorConfig, channels: list[Channel],
                 on_open: Callable[[Channel, float, float], None],
                 on_rssi: Callable[[Channel, float], None],
                 on_close: Callable[[Channel, float], None],
                 on_tick: Callable[[Channel, float], None] | None = None):
        self.cfg = cfg
        self.channels = channels
        self.fg = GmrsFlowgraph(channels, cfg)
        self._on_open = on_open
        self._on_rssi = on_rssi
        self._on_close = on_close
        self._on_tick = on_tick

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[int, dict] = {
            ch.num: {"open": False, "above_since": None, "below_since": None,
                     "opened_at": None}
            for ch in channels
        }
        self._ch_by_num = {ch.num: ch for ch in channels}

    @property
    def recorders(self) -> dict[int, AudioRecorder]:
        return self.fg.recorders

    def start(self):
        self.fg.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="gmrs-poll", daemon=True)
        self._thread.start()
        log.info("GmrsMonitor started")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        # Close any open recordings cleanly
        for rec in self.fg.recorders.values():
            rec.stop_record()
        self.fg.stop()
        self.fg.wait()
        log.info("GmrsMonitor stopped")

    def _poll_loop(self):
        period = 1.0 / self.cfg.poll_hz
        threshold_lin = 10 ** (self.cfg.squelch_db / 10.0)
        max_rec_s = self.cfg.max_record_s
        while not self._stop.is_set():
            now = time.time()
            for ch_num, probe in self.fg.probes.items():
                mag_sq = probe.level()
                rssi_db = 10 * math.log10(max(mag_sq, 1e-20))
                ch = self._ch_by_num[ch_num]
                st = self._state[ch_num]

                if self._on_tick is not None:
                    try:
                        self._on_tick(ch, rssi_db)
                    except Exception:
                        log.exception("on_tick handler failed")

                above = mag_sq > threshold_lin

                if above and not st["open"]:
                    st["below_since"] = None
                    if st["above_since"] is None:
                        st["above_since"] = now
                    elif (now - st["above_since"]) >= self.cfg.open_hold_s:
                        st["open"] = True
                        st["opened_at"] = now
                        st["above_since"] = None
                        try:
                            self._on_open(ch, now, rssi_db)
                        except Exception:
                            log.exception("on_open handler failed")
                elif above and st["open"]:
                    st["below_since"] = None
                    try:
                        self._on_rssi(ch, rssi_db)
                    except Exception:
                        log.exception("on_rssi handler failed")
                    # Safety: cap runaway recording
                    if st["opened_at"] and (now - st["opened_at"]) > max_rec_s:
                        log.warning("ch=%d: hit max_record_s, force-closing", ch_num)
                        st["open"] = False
                        st["opened_at"] = None
                        try:
                            self._on_close(ch, now)
                        except Exception:
                            log.exception("on_close (force) handler failed")
                elif not above and st["open"]:
                    st["above_since"] = None
                    if st["below_since"] is None:
                        st["below_since"] = now
                    elif (now - st["below_since"]) >= self.cfg.close_hold_s:
                        st["open"] = False
                        st["opened_at"] = None
                        st["below_since"] = None
                        try:
                            self._on_close(ch, now)
                        except Exception:
                            log.exception("on_close handler failed")
                else:
                    st["above_since"] = None
                    st["below_since"] = None

            time.sleep(period)


def default_channels() -> list[Channel]:
    """Channels covered by a single 2 Msps tune @ 462.6375 MHz."""
    return CHANNELS_462
