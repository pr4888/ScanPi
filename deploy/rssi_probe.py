#!/usr/bin/env python3
"""Raw RSSI probe — bypass the scanpi-gmrs flowgraph entirely.

Tunes the NESDR straight to the Ch 1 carrier (462.5625 MHz) at 250 kS/s,
measures in-band power every 100 ms for 20 s, prints to stdout.

Use this to prove whether the RF signal actually stays continuous during
a long key-up or whether it truly drops.

USAGE (on scanpi):
  sudo systemctl stop scanpi-v3
  python3 /tmp/rssi_probe.py
  # ... key up, talk continuously for 15+ seconds ...
  sudo systemctl start scanpi-v3
"""
import math
import time
import numpy as np
from gnuradio import gr, blocks, analog
import osmosdr


class Probe(gr.top_block):
    def __init__(self, center_hz=462_562_500, sample_rate=250_000, gain=10.0):
        super().__init__("Raw RSSI Probe")
        src = osmosdr.source(args="numchan=1 rtl=0")
        src.set_sample_rate(sample_rate)
        src.set_center_freq(center_hz)
        src.set_gain_mode(False)
        src.set_gain(gain)
        src.set_bandwidth(sample_rate)
        # Fast IIR averaging so we catch dips
        self.probe = analog.probe_avg_mag_sqrd_c(-50.0, 0.1)
        self.connect(src, self.probe)


def main():
    tb = Probe()
    tb.start()
    start = time.time()
    last_print = start
    print(f"{'time_s':>6} {'rssi_dBFS':>10}")
    try:
        while time.time() - start < 20.0:
            if time.time() - last_print >= 0.1:
                last_print = time.time()
                lvl = tb.probe.level()
                rssi = 10 * math.log10(max(lvl, 1e-20))
                t = time.time() - start
                flag = "*** SIGNAL ***" if rssi > -40 else ""
                print(f"{t:6.1f} {rssi:10.1f}  {flag}")
            time.sleep(0.02)
    finally:
        tb.stop()
        tb.wait()


if __name__ == "__main__":
    main()
