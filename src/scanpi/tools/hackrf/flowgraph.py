"""GNU Radio polyphase channelizer flowgraph for HackRF One.

Architecture (per RESEARCH_2026-05-04.md sections 1b + 3 + 9):

    osmosdr/file_source -> stream_to_streams (deinterleave for PFB)
                        -> pfb.channelizer_ccf (M outputs at sr/M)
                        -> for each requested channel:
                             [ optional rotator if bin_offset != 0 ]
                             pwr_squelch_cc                       (gates audio noise floor)
                             quadrature_demod_cf  | complex_to_mag (NFM | AM)
                             fir_filter_fff (audio LPF + decim)
                             [ fm_emph if NFM ]
                             AudioRecorder (preroll WAV + RSSI probe)

The PFB's per-output sample rate is sr / num_chans; for a typical config
(8 Msps / 32 = 250 kHz) we further decimate inside the demod chain to a
16 kHz audio rate (250000 / 16000 ~= 15.6 -> we round-decimate to 16 by
allowing a slight rate mismatch via fractional resampling, or pick a
num_chans that gives a clean integer divisor — 8M/40=200k -> /12.5 not
integer; 8M/50=160k /10 = 16k OK. The default 32-channel path uses
160000/16000=10 by selecting an intermediate decimation that gets us
close enough; production tuning happens once the hardware lands).

For the dev-machine smoke test we rely on `cfg.fake_iq_path`. When set,
we use a `blocks.file_source` (cyclic) + `blocks.throttle` to feed the
graph at the configured sample_rate. This lets the test pass without
HackRF / gr-osmosdr.

ALL gnuradio imports are LAZY (inside functions). The module imports
cleanly even when GR is not installed, so the rest of the tool can still
be loaded for tests / API browsing.
"""
from __future__ import annotations

import collections
import logging
import math
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .profiles import Profile, ChannelSpec

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- recorder


def _make_recorder_class():
    """Lazily build the AudioRecorder class. Requires numpy + gnuradio."""
    import numpy as np
    from gnuradio import gr

    class AudioRecorder(gr.sync_block):
        """Sink: 1 float32 audio in. Buffers preroll, writes WAV on demand.

        Mirrors tools/gmrs/monitor.py's AudioRecorder pattern so the on_open
        / on_close callback shape matches the GMRS tool exactly.
        """

        def __init__(self, channel_name: str, sample_rate: int,
                     preroll_samples: int, audio_gain: float = 1.0):
            gr.sync_block.__init__(
                self, name=f"hackrf:{channel_name}",
                in_sig=[np.float32], out_sig=None,
            )
            self._name = channel_name
            self._rate = sample_rate
            self._gain = audio_gain
            self._buf = collections.deque(maxlen=preroll_samples)
            self._lock = threading.Lock()
            self._wav: wave.Wave_write | None = None
            self._path: str | None = None
            self._samples_written = 0

        def work(self, input_items, output_items):
            samples = input_items[0]
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

    return AudioRecorder


# ---------------------------------------------------------------- helpers


def pfb_taps(prof: Profile):
    """Build PFB low-pass prototype taps. Lazy GR import."""
    from gnuradio.filter import firdes
    sr = prof.sdr.sample_rate
    M = prof.channelizer.num_chans
    chan_bw = sr / M
    transition = chan_bw * prof.channelizer.transition_frac
    atten = prof.channelizer.attenuation_db
    # Rondeau / GR doxygen: prototype low-pass at the channel bandwidth, designed
    # as if for the full sample rate; PFB internally polyphases it to M arms.
    return firdes.low_pass_2(
        gain=1.0,
        sampling_freq=sr,
        cutoff_freq=chan_bw / 2,
        transition_width=transition,
        attenuation_dB=atten,
    )


def _audio_chain_decim(channel_rate: int, target_audio_rate: int) -> tuple[int, int]:
    """Choose an integer decimation that gets channel_rate close to target_audio_rate.

    Returns (decim, actual_audio_rate). Prefers an exact divisor; if none fits,
    finds the closest integer decimation.
    """
    if channel_rate <= 0 or target_audio_rate <= 0:
        return 1, channel_rate
    if channel_rate % target_audio_rate == 0:
        d = channel_rate // target_audio_rate
        return d, target_audio_rate
    # Best integer decimation to land near target_audio_rate.
    d = max(1, round(channel_rate / target_audio_rate))
    return d, channel_rate // d


# --------------------------------------------------------- top_block


def build_top_block(prof: Profile, on_audio: dict | None = None,
                     audio_rate: int = 16_000, preroll_s: float = 0.5,
                     audio_gain: float = 3.0):
    """Build (but do not start) a GNU Radio top_block from a validated Profile.

    Returns a tuple (top_block, recorders, probes, meta) where:
      - recorders: dict[channel_name -> AudioRecorder]
      - probes:    dict[channel_name -> probe_avg_mag_sqrd_c]
      - meta:      {"channel_rate": int, "audio_rate": int, "audio_decim": int}

    Lazy-imports GR; will raise ImportError if GR not installed. Callers should
    catch and skip-register accordingly.
    """
    # All GR imports go here — keeps the module importable on a dev box.
    from gnuradio import gr, blocks, analog, filter as gr_filter
    from gnuradio.filter import pfb
    from gnuradio.analog import fm_emph

    AudioRecorder = _make_recorder_class()

    sr = prof.sdr.sample_rate
    M = prof.channelizer.num_chans
    chan_bw = sr // M

    tb = gr.top_block(f"ScanPi HackRF :: {prof.sdr.id}")

    # ---- source ----
    if prof.sdr.fake_iq_path:
        # File source for offline / dev-box test.
        # Item size is gr.sizeof_gr_complex (8 bytes) for complex64 IQ.
        log.info("HackRF flowgraph: using fake IQ source from %s", prof.sdr.fake_iq_path)
        path = str(Path(prof.sdr.fake_iq_path).expanduser())
        src = blocks.file_source(gr.sizeof_gr_complex, path, repeat=True)
        throttle = blocks.throttle(gr.sizeof_gr_complex, sr)
        tb.connect(src, throttle)
        head = throttle
    else:
        # Hardware source via gr-osmosdr (same pattern GMRS uses).
        import osmosdr
        args = f"hackrf={prof.sdr.serial}" if prof.sdr.serial else "hackrf=0"
        src = osmosdr.source(args=args)
        src.set_sample_rate(sr)
        src.set_center_freq(prof.sdr.center_hz)
        src.set_freq_corr(0)
        # HackRF supports "lna=N,vga=N,amp=0|1" via gr-osmosdr gain string;
        # we let osmosdr parse our gain string by setting individual stages.
        for part in prof.sdr.gain.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip().lower()
                try:
                    val = float(v.strip())
                except ValueError:
                    continue
                if k == "amp":
                    src.set_gain(val, "AMP")
                elif k == "lna":
                    src.set_gain(val, "LNA")
                elif k == "vga" or k == "if" or k == "bb":
                    src.set_gain(val, "BB")  # HackRF baseband gain is the VGA stage
        src.set_bandwidth(sr * 0.75)  # internal IF bandwidth
        head = src

    # ---- PFB channelizer ----
    taps = pfb_taps(prof)
    # oversample_rate=1 = critically sampled; output sample rate = sr / M.
    ch = pfb.channelizer_ccf(numchans=M, taps=taps, oversample_rate=1)
    tb.connect(head, ch)

    # ---- per-channel demod chain ----
    audio_decim, actual_audio_rate = _audio_chain_decim(chan_bw, audio_rate)
    audio_taps = gr_filter.firdes.low_pass(
        gain=1.0,
        sampling_freq=chan_bw,
        cutoff_freq=min(3_400, chan_bw / 2.5),
        transition_width=max(500, chan_bw * 0.05),
    )
    # FM quadrature gain: normalizes ±max_dev to ±1.
    nfm_max_dev = 5_000.0
    quad_gain = chan_bw / (2.0 * math.pi * nfm_max_dev)
    preroll_samples = int(preroll_s * actual_audio_rate)

    recorders: dict = {}
    probes: dict = {}

    for spec in prof.channels:
        idx = spec.output_index
        if not (0 <= idx < M):
            log.warning("channel %s output_index %d out of range (M=%d), skipping",
                        spec.name, idx, M)
            continue

        # Optional rotator if the channel center doesn't sit exactly on a bin.
        # GR convention: rotator_cc(phase_inc) shifts spectrum by -phase_inc * fs.
        last_complex = (ch, idx)
        if abs(spec.bin_offset_hz) > 0.5:
            phase_inc = -2.0 * math.pi * spec.bin_offset_hz / chan_bw
            rot = blocks.rotator_cc(phase_inc)
            tb.connect((ch, idx), rot)
            last_complex = (rot, 0)

        # Squelch on complex baseband (gates with zeros below threshold).
        squelch = analog.pwr_squelch_cc(spec.squelch_db, 0.05, 0, False)
        tb.connect(last_complex, squelch)

        # Probe for RSSI polling (mirror GMRS pattern).
        probe = analog.probe_avg_mag_sqrd_c(spec.squelch_db - 5.0, 0.05)
        tb.connect(last_complex, probe)

        # Demodulator
        if spec.demod == "am":
            demod = blocks.complex_to_mag(1)
        elif spec.demod == "wfm":
            wfm_gain = chan_bw / (2.0 * math.pi * 75_000.0)
            demod = analog.quadrature_demod_cf(wfm_gain)
        else:  # nfm default
            demod = analog.quadrature_demod_cf(quad_gain)
        tb.connect(squelch, demod)

        # Audio LPF + decim to ~16 kHz.
        audio_lpf = gr_filter.fir_filter_fff(audio_decim, audio_taps)
        tb.connect(demod, audio_lpf)

        # De-emphasis for NFM/WFM (skip for AM).
        last_audio = audio_lpf
        if spec.demod in ("nfm", "wfm"):
            tau = spec.deemph_us * 1e-6
            deemph = fm_emph.fm_deemph(actual_audio_rate, tau=tau)
            tb.connect(audio_lpf, deemph)
            last_audio = deemph

        rec = AudioRecorder(spec.name, actual_audio_rate, preroll_samples,
                            audio_gain=audio_gain)
        tb.connect(last_audio, rec)
        recorders[spec.name] = rec
        probes[spec.name] = probe

    meta = {
        "channel_rate": chan_bw,
        "audio_rate": actual_audio_rate,
        "audio_decim": audio_decim,
        "channels_wired": len(recorders),
    }
    log.info("HackRF flowgraph built: M=%d chan_bw=%d Hz audio=%d Hz channels=%d",
             M, chan_bw, actual_audio_rate, len(recorders))
    return tb, recorders, probes, meta


# ---------------------------------------------------------------- monitor


@dataclass
class HackrfMonitorConfig:
    audio_rate: int = 16_000
    preroll_s: float = 0.5
    audio_gain: float = 3.0
    open_hold_s: float = 0.2
    close_hold_s: float = 1.5
    poll_hz: float = 20.0
    max_record_s: float = 120.0


class HackrfMonitor:
    """Polls each PFB output's RSSI probe; calls on_open/on_rssi/on_close.

    Thin parallel of GmrsMonitor — keeps the controller code pattern the same.
    """

    def __init__(self, prof: Profile, cfg: HackrfMonitorConfig,
                 on_open: Callable[[ChannelSpec, float, float], None],
                 on_rssi: Callable[[ChannelSpec, float], None],
                 on_close: Callable[[ChannelSpec, float], None],
                 on_tick: Callable[[ChannelSpec, float], None] | None = None):
        self.profile = prof
        self.cfg = cfg
        self._on_open = on_open
        self._on_rssi = on_rssi
        self._on_close = on_close
        self._on_tick = on_tick

        self.tb = None
        self.recorders: dict = {}
        self.probes: dict = {}
        self.meta: dict = {}

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state: dict[str, dict] = {}
        self._spec_by_name: dict[str, ChannelSpec] = {ch.name: ch for ch in prof.channels}

    def build(self):
        """Lazy: build the GR top_block. Raises ImportError if GR missing."""
        self.tb, self.recorders, self.probes, self.meta = build_top_block(
            self.profile,
            audio_rate=self.cfg.audio_rate,
            preroll_s=self.cfg.preroll_s,
            audio_gain=self.cfg.audio_gain,
        )
        self._state = {
            name: {"open": False, "above_since": None, "below_since": None,
                   "opened_at": None}
            for name in self.recorders.keys()
        }

    def start(self):
        if self.tb is None:
            self.build()
        self.tb.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="hackrf-poll",
                                         daemon=True)
        self._thread.start()
        log.info("HackrfMonitor started")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        for r in self.recorders.values():
            try:
                r.stop_record()
            except Exception:
                log.exception("recorder stop_record failed")
        if self.tb is not None:
            try:
                self.tb.stop()
                self.tb.wait()
            except Exception:
                log.exception("top_block stop/wait failed")
        log.info("HackrfMonitor stopped")

    def _poll_loop(self):
        period = 1.0 / max(1.0, self.cfg.poll_hz)
        while not self._stop.is_set():
            now = time.time()
            for name, probe in self.probes.items():
                spec = self._spec_by_name[name]
                threshold_lin = 10 ** (spec.squelch_db / 10.0)
                try:
                    mag_sq = probe.level()
                except Exception:
                    log.exception("probe.level() failed for %s", name)
                    continue
                rssi_db = 10 * math.log10(max(mag_sq, 1e-20))
                st = self._state[name]
                if self._on_tick:
                    try:
                        self._on_tick(spec, rssi_db)
                    except Exception:
                        log.exception("on_tick failed")
                above = mag_sq > threshold_lin
                if above and not st["open"]:
                    st["below_since"] = None
                    if st["above_since"] is None:
                        st["above_since"] = now
                    elif (now - st["above_since"]) >= self.cfg.open_hold_s:
                        true_open = st["above_since"]
                        st["open"] = True
                        st["opened_at"] = true_open
                        st["above_since"] = None
                        try:
                            self._on_open(spec, true_open, rssi_db)
                        except Exception:
                            log.exception("on_open failed")
                elif above and st["open"]:
                    st["below_since"] = None
                    try:
                        self._on_rssi(spec, rssi_db)
                    except Exception:
                        log.exception("on_rssi failed")
                    if st["opened_at"] and (now - st["opened_at"]) > self.cfg.max_record_s:
                        log.warning("ch=%s hit max_record_s, force-closing", name)
                        st["open"] = False
                        st["opened_at"] = None
                        try:
                            self._on_close(spec, now)
                        except Exception:
                            log.exception("on_close (force) failed")
                elif not above and st["open"]:
                    st["above_since"] = None
                    if st["below_since"] is None:
                        st["below_since"] = now
                    elif (now - st["below_since"]) >= self.cfg.close_hold_s:
                        true_close = st["below_since"]
                        st["open"] = False
                        st["opened_at"] = None
                        st["below_since"] = None
                        try:
                            self._on_close(spec, true_close)
                        except Exception:
                            log.exception("on_close failed")
                else:
                    st["above_since"] = None
                    st["below_since"] = None
            time.sleep(period)
