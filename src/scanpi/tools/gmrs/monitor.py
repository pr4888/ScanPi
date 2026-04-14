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
    # gr-osmosdr device selector. Examples:
    #   "rtl=0"                   → first RTL-SDR enumerated
    #   "rtl=serial=00000001"    → specific stick (stable across reboots/replugs)
    sdr_args: str = "numchan=1 rtl=0"
    squelch_db: float = -45.0  # well above noise floor (~-66), tolerant of dropouts
    squelch_alpha: float = 0.05
    # Edge detection
    open_hold_s: float = 0.2   # declare OPEN this long after RSSI first exceeds threshold
    close_hold_s: float = 0.5  # declare CLOSE this long after RSSI first drops
    poll_hz: float = 20.0
    # Audio recording.  16 kHz is a standard WAV rate; browsers play it reliably.
    # With sample_rate=2_000_000 and decim=25 → channel_rate=80_000,
    # audio_decim = 80_000 / 16_000 = 5 (integer — required by fir_filter_fff).
    audio_rate: int = 16_000
    preroll_s: float = 0.3    # seconds of audio buffered before squelch opens
    max_record_s: float = 120  # safety cap on any single TX
    audio_gain: float = 3.0   # post-demod gain to fill int16 range for typical voice


class AudioRecorder(gr.sync_block):
    """Sink block: buffers a rolling preroll and writes WAV on demand.

    Thread-safe: start_record/stop_record are called from the poll thread
    while work() runs in the GR scheduler thread. Guarded by a mutex.
    """

    def __init__(self, channel_num: int, sample_rate: int, preroll_samples: int,
                 audio_gain: float = 1.0):
        gr.sync_block.__init__(
            self, name=f"ch{channel_num}_rec",
            in_sig=[np.float32], out_sig=None,
        )
        self._ch = channel_num
        self._rate = sample_rate
        self._gain = audio_gain
        self._buf = collections.deque(maxlen=preroll_samples)
        self._lock = threading.Lock()
        self._wav: wave.Wave_write | None = None
        self._path: str | None = None
        self._samples_written = 0

    def work(self, input_items, output_items):
        samples = input_items[0]
        # Post-demod gain + hard clip to int16 range.
        boosted = np.clip(samples * self._gain, -1.0, 1.0)
        with self._lock:
            if self._wav is not None:
                self._wav.writeframes((boosted * 24000).astype(np.int16).tobytes())
                self._samples_written += len(samples)
            else:
                self._buf.extend(boosted)
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
                # Buffer is already clipped in work(); scale consistent with main path.
                self._wav.writeframes((pre * 24000).astype(np.int16).tobytes())
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

        src = osmosdr.source(args=cfg.sdr_args)
        src.set_sample_rate(cfg.sample_rate)
        src.set_center_freq(cfg.center_hz)
        src.set_gain_mode(False)
        src.set_gain(cfg.rtl_gain)
        src.set_bandwidth(cfg.sample_rate)
        self.src = src

        decim = 25  # 2 Msps / 25 = 80 kHz channel rate (→ clean divide to 16 kHz audio)
        channel_rate = cfg.sample_rate // decim  # 80 kHz
        audio_rate = cfg.audio_rate

        import math
        from gnuradio.analog import fm_emph

        # Channel filter: ±8 kHz passband.
        # GMRS max deviation 5 kHz + voice to 3 kHz → Carson's rule BW ≈ 16 kHz.
        ch_taps = firdes.low_pass(1.0, cfg.sample_rate, 8_000, 3_000)
        # Audio low-pass: ~3.4 kHz (voice) at channel_rate; then decimate to audio_rate.
        audio_decim = channel_rate // audio_rate
        audio_taps = firdes.low_pass(1.0, channel_rate, 3_400, 1_500)
        # Quadrature demod gain: normalizes output to ±1 for ±max_dev input.
        # FRS/GMRS narrowband max deviation is 2.5 kHz (not 5 kHz), so setting
        # max_dev here gives proper voice amplitude scaling.
        max_dev = 2_500.0
        quad_gain = channel_rate / (2.0 * math.pi * max_dev)
        preroll_samples = int(cfg.preroll_s * audio_rate)

        self.probes: dict[int, analog.probe_avg_mag_sqrd_c] = {}
        self.recorders: dict[int, AudioRecorder] = {}

        # Per-channel manual FM chain:
        #   xlate → quadrature_demod → audio_lowpass+decim → de-emphasis → recorder
        # (Replaces analog.nbfm_rx which has a +20 dB internal squelch that never
        # opens on RTL-SDR-normalized signals, causing silence-only output.)
        for ch in channels:
            offset = ch.freq_hz - cfg.center_hz
            xlate = gr_filter.freq_xlating_fir_filter_ccc(
                decim, ch_taps, offset, cfg.sample_rate,
            )
            probe = analog.probe_avg_mag_sqrd_c(cfg.squelch_db - 5.0, 0.05)
            # Power squelch gates the audio: below squelch_db it outputs zeros
            # (gate=False), so pre-roll and close-hold windows stay silent
            # instead of recording loud demodulated noise.
            audio_squelch = analog.pwr_squelch_cc(
                cfg.squelch_db, cfg.squelch_alpha, 0, False,
            )
            quad_demod = analog.quadrature_demod_cf(quad_gain)
            audio_lpf = gr_filter.fir_filter_fff(audio_decim, audio_taps)
            deemph = fm_emph.fm_deemph(audio_rate, tau=75e-6)
            recorder = AudioRecorder(ch.num, audio_rate, preroll_samples,
                                      audio_gain=cfg.audio_gain)

            self.connect(src, xlate, probe)
            self.connect(xlate, audio_squelch, quad_demod, audio_lpf, deemph, recorder)

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
                        # Use the time the signal FIRST went above threshold, not now.
                        true_open = st["above_since"]
                        st["open"] = True
                        st["opened_at"] = true_open
                        st["above_since"] = None
                        try:
                            self._on_open(ch, true_open, rssi_db)
                        except Exception:
                            log.exception("on_open handler failed")
                elif above and st["open"]:
                    if st["below_since"] is not None:
                        log.debug("ch=%d: rssi recovered to %.1f dBFS, below-timer reset", ch_num, rssi_db)
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
                        log.debug("ch=%d: rssi dropped to %.1f dBFS, below-timer started", ch_num, rssi_db)
                    elif (now - st["below_since"]) >= self.cfg.close_hold_s:
                        # Use the time the signal FIRST dropped, not now. This makes
                        # reported duration reflect true TX window.
                        true_close = st["below_since"]
                        st["open"] = False
                        st["opened_at"] = None
                        st["below_since"] = None
                        try:
                            self._on_close(ch, true_close)
                        except Exception:
                            log.exception("on_close handler failed")
                else:
                    st["above_since"] = None
                    st["below_since"] = None

            time.sleep(period)


def default_channels() -> list[Channel]:
    """Channels covered by a single 2 Msps tune @ 462.6375 MHz."""
    return CHANNELS_462
