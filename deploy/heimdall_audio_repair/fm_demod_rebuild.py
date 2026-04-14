"""Drop-in FM demodulation chain replacing analog.nbfm_rx.

Why: analog.nbfm_rx has an internal simple_squelch_cc with threshold +20 dB.
For RTL-SDR output normalized to [-1, 1] (mag_sq ~1 max), that threshold
NEVER opens — every sample is gated off, producing silence or trickle-through
hiss from the squelch's ramp behavior. The symptoms look exactly like bad
demod but the demod never runs.

Usage:

    from gnuradio import gr, analog, filter as gr_filter
    from gnuradio.filter import firdes

    tb = gr.top_block()
    src = ...               # complex baseband at channel_rate
    sink = ...              # float audio sink at audio_rate

    from fm_demod_rebuild import build_nbfm_chain
    build_nbfm_chain(
        tb, src, sink,
        channel_rate=80_000,
        audio_rate=16_000,
        squelch_db=-45.0,       # ~15 dB above noise floor
        max_dev=2_500.0,        # narrowband FRS/GMRS (use 5000 for wideband)
        deemph_tau=75e-6,       # 75us for US land mobile; use 0 to disable
    )
    tb.start()

If your audio sounds clean but post-TX windows record loud hiss, the squelch
threshold may be too low — raise `squelch_db` closer to signal strength.
"""
from __future__ import annotations

import math

from gnuradio import analog, filter as gr_filter
from gnuradio.filter import firdes
from gnuradio.analog import fm_emph


def build_nbfm_chain(
    top_block,
    source,
    sink,
    channel_rate: int,
    audio_rate: int,
    squelch_db: float = -45.0,
    squelch_alpha: float = 0.05,
    max_dev: float = 2_500.0,
    deemph_tau: float = 75e-6,
    audio_cutoff: float = 3_400.0,
    audio_transition: float = 1_500.0,
) -> dict:
    """Build FM demod chain from complex baseband `source` to float audio `sink`.

    Validates: channel_rate must be an integer multiple of audio_rate.

    Returns a dict with references to each block created, so the caller can
    hold them (they are also automatically connected into `top_block`).

    Chain:
        source → pwr_squelch_cc (gate=False: outputs zeros when closed)
               → quadrature_demod_cf (gain normalizes ±max_dev to ±1)
               → fir_filter_fff (audio lowpass + decimate to audio_rate)
               → fm_deemph (optional; skipped if deemph_tau <= 0)
               → sink
    """
    if channel_rate % audio_rate != 0:
        raise ValueError(
            f"channel_rate {channel_rate} must be an integer multiple of "
            f"audio_rate {audio_rate}")
    audio_decim = channel_rate // audio_rate

    # Audio-path squelch: gate=False emits zeros when below threshold.
    squelch = analog.pwr_squelch_cc(squelch_db, squelch_alpha, 0, False)

    # Quadrature demodulator gain normalizes ±max_dev deviation to ±1 output.
    quad_gain = channel_rate / (2.0 * math.pi * max_dev)
    quad_demod = analog.quadrature_demod_cf(quad_gain)

    # Voice low-pass filter with decimation to audio_rate.
    audio_taps = firdes.low_pass(1.0, channel_rate, audio_cutoff, audio_transition)
    audio_lpf = gr_filter.fir_filter_fff(audio_decim, audio_taps)

    blocks = {
        "squelch": squelch,
        "quad_demod": quad_demod,
        "audio_lpf": audio_lpf,
    }

    if deemph_tau and deemph_tau > 0:
        deemph = fm_emph.fm_deemph(audio_rate, deemph_tau)
        top_block.connect(source, squelch, quad_demod, audio_lpf, deemph, sink)
        blocks["deemph"] = deemph
    else:
        top_block.connect(source, squelch, quad_demod, audio_lpf, sink)

    return blocks
