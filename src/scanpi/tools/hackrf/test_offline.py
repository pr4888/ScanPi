"""Offline tests for the HackRF tool — no hardware required.

Three tiers:

  1. Profile / DB tests run on any machine (numpy + tomllib only).
  2. The flowgraph topology test runs IFF gnuradio is importable. It builds
     a top_block from a synthesized IQ file (a complex sinewave at a known
     offset) and verifies the channelizer wires the expected number of
     output streams.
  3. A short start/stop smoke run exercises the gr scheduler with the file
     source for ~250 ms — proves the recorder block can sink samples
     without a real radio.

Run with:  python -m pytest src/scanpi/tools/hackrf/test_offline.py -v
or:        python -m scanpi.tools.hackrf.test_offline
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


# Make the package importable when run as a script.
HERE = Path(__file__).resolve()
SRC_ROOT = HERE.parents[3]   # ...\src
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _make_synth_iq(path: Path, sample_rate: int, duration_s: float = 0.5,
                    tone_offset_hz: float = 250_000.0):
    """Write a complex64 IQ file with a single tone at the given offset.

    Pi-portable: uses array module + struct rather than numpy when numpy is
    absent. Numpy path is used when available for speed.
    """
    n = int(sample_rate * duration_s)
    try:
        import numpy as np
        t = np.arange(n) / sample_rate
        sig = np.exp(1j * 2 * np.pi * tone_offset_hz * t).astype(np.complex64)
        path.write_bytes(sig.tobytes())
    except Exception:
        # Fallback: write zeros (topology test still works).
        with open(path, "wb") as fh:
            zero = struct.pack("<ff", 0.0, 0.0)
            for _ in range(n):
                fh.write(zero)


# --------------------------------------------------------------------- tests


class ProfileTests(unittest.TestCase):
    def test_load_preset(self):
        from scanpi.tools.hackrf.profiles import list_presets, load_profile
        presets = list_presets()
        self.assertTrue(presets, "no presets bundled in profiles/sdrs/presets/")
        for p in presets:
            with self.subTest(preset=p.name):
                prof = load_profile(p)
                self.assertGreater(prof.channelizer.num_chans, 0)
                self.assertGreater(prof.sdr.sample_rate, 0)
                # All channels must validate inside the window.
                lo = prof.sdr.center_hz - prof.sdr.sample_rate / 2
                hi = prof.sdr.center_hz + prof.sdr.sample_rate / 2
                for c in prof.channels:
                    self.assertGreaterEqual(c.freq_hz, lo, f"{p.name}:{c.name} below window")
                    self.assertLessEqual(c.freq_hz, hi, f"{p.name}:{c.name} above window")
                    self.assertIn(c.demod, ("nfm", "wfm", "am"))
                    self.assertGreaterEqual(c.output_index, 0)
                    self.assertLess(c.output_index, prof.channelizer.num_chans)

    def test_validation_rejects_out_of_window(self):
        from scanpi.tools.hackrf.profiles import parse_text
        bad = textwrap.dedent("""
            [sdr]
            id = "test"
            center_hz = 462_500_000
            sample_rate = 8_000_000
            [channelizer]
            num_chans = 32
            [[channels]]
            name = "out-of-band"
            freq_hz = 462_700_000
            [[channels]]
            name = "way-out"
            freq_hz = 470_000_000
        """).strip()
        with self.assertRaises(ValueError):
            parse_text(bad)

    def test_round_trip_save_load(self):
        from scanpi.tools.hackrf.profiles import (
            load_profile, save_profile, list_presets,
        )
        presets = list_presets()
        if not presets:
            self.skipTest("no presets")
        prof = load_profile(presets[0])
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "round.toml"
            save_profile(prof, dest)
            re = load_profile(dest)
        self.assertEqual(prof.sdr.center_hz, re.sdr.center_hz)
        self.assertEqual(len(prof.channels), len(re.channels))
        for a, b in zip(prof.channels, re.channels):
            self.assertEqual(a.name, b.name)
            self.assertEqual(a.freq_hz, b.freq_hz)


class DbTests(unittest.TestCase):
    def test_open_close_roundtrip(self):
        from scanpi.tools.hackrf.db import HackrfDB
        with tempfile.TemporaryDirectory() as tmp:
            db = HackrfDB(Path(tmp) / "h.db")
            db.connect()
            try:
                evt = db.open_event("GMRS-1", 462_562_500, time.time(), -28.0)
                db.update_event_rssi(evt, -22.0)
                db.close_event(evt, time.time(), clip_path=str(Path(tmp)/"x.wav"))
                row = db.get_event(evt)
                self.assertIsNotNone(row)
                self.assertEqual(row["channel"], "GMRS-1")
                self.assertGreaterEqual(row["peak_rssi"], -28.0)
                self.assertEqual(row["transcript_status"], None)
                rows = db.recent_events(limit=5)
                self.assertEqual(len(rows), 1)
                stats = db.channel_stats()
                self.assertEqual(stats[0]["tx_count"], 1)
            finally:
                db.close()


def _gnuradio_available() -> bool:
    try:
        import gnuradio  # noqa
        from gnuradio import gr, blocks, analog, filter as gr_filter  # noqa
        from gnuradio.filter import pfb  # noqa
        return True
    except Exception:
        return False


@unittest.skipUnless(_gnuradio_available(), "gnuradio not installed on this host")
class FlowgraphTests(unittest.TestCase):
    """Topology + brief scheduler smoke test using a synthesized IQ file."""

    def test_build_topology(self):
        from scanpi.tools.hackrf.profiles import parse_text
        from scanpi.tools.hackrf.flowgraph import build_top_block

        prof_text = textwrap.dedent("""
            [sdr]
            id = "test"
            center_hz = 462_500_000
            sample_rate = 8_000_000
            fake_iq_path = "__IQ__"
            [channelizer]
            num_chans = 32
            [[channels]]
            name = "GMRS-1"
            freq_hz = 462_562_500
            demod = "nfm"
            bw_hz = 12_500
            squelch_db = -40.0
            [[channels]]
            name = "GMRS-15"
            freq_hz = 462_550_000
            demod = "nfm"
            bw_hz = 12_500
            squelch_db = -40.0
        """).strip()

        with tempfile.TemporaryDirectory() as tmp:
            iq_path = Path(tmp) / "synth.iq"
            _make_synth_iq(iq_path, sample_rate=8_000_000, duration_s=0.25,
                           tone_offset_hz=62_500)
            prof = parse_text(prof_text.replace("__IQ__", str(iq_path).replace("\\", "/")))

            tb, recorders, probes, meta = build_top_block(prof, audio_rate=16_000,
                                                          preroll_s=0.1, audio_gain=1.0)
            self.assertEqual(len(recorders), 2)
            self.assertEqual(len(probes), 2)
            self.assertEqual(meta["channels_wired"], 2)
            self.assertEqual(meta["channel_rate"], 8_000_000 // 32)

    def test_run_briefly(self):
        from scanpi.tools.hackrf.profiles import parse_text
        from scanpi.tools.hackrf.flowgraph import build_top_block

        prof_text = textwrap.dedent("""
            [sdr]
            id = "test"
            center_hz = 462_500_000
            sample_rate = 8_000_000
            fake_iq_path = "__IQ__"
            [channelizer]
            num_chans = 32
            [[channels]]
            name = "GMRS-1"
            freq_hz = 462_562_500
            demod = "nfm"
            bw_hz = 12_500
            squelch_db = -40.0
        """).strip()

        with tempfile.TemporaryDirectory() as tmp:
            iq_path = Path(tmp) / "synth.iq"
            _make_synth_iq(iq_path, sample_rate=8_000_000, duration_s=0.5)
            prof = parse_text(prof_text.replace("__IQ__", str(iq_path).replace("\\", "/")))
            tb, recorders, probes, meta = build_top_block(prof, audio_rate=16_000,
                                                          preroll_s=0.1, audio_gain=1.0)
            tb.start()
            time.sleep(0.25)
            # Probe has a level (>= 0)
            level = probes["GMRS-1"].level()
            self.assertGreaterEqual(level, 0.0)
            tb.stop()
            tb.wait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
