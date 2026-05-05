"""FRS/GMRS channel plan + CTCSS/DCS tone tables.

22 channels total:
  Ch 1-7   — FRS/GMRS shared (462 MHz block), 2W FRS / 5W GMRS
  Ch 8-14  — FRS-only (467 MHz block), 0.5W max
  Ch 15-22 — GMRS-only (462 MHz block), up to 50W

Consumer walkie-talkies (Motorola Talkabout, Midland, Cobra, etc.) ship with
default channel 1, subtone 0 (carrier squelch). Kids who never change settings
cluster there. Ch 15-22 are common on GMRS licensees.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Channel:
    num: int
    freq_hz: int
    service: str  # "FRS", "GMRS", "FRS/GMRS"
    max_power_w: float


# 462 MHz block — 15 channels covered by a single 2 Msps tune @ 462.6375 MHz
CHANNELS_462: list[Channel] = [
    Channel(1,  462_562_500,  "FRS/GMRS", 5.0),
    Channel(2,  462_587_500,  "FRS/GMRS", 5.0),
    Channel(3,  462_612_500,  "FRS/GMRS", 5.0),
    Channel(4,  462_637_500,  "FRS/GMRS", 5.0),
    Channel(5,  462_662_500,  "FRS/GMRS", 5.0),
    Channel(6,  462_687_500,  "FRS/GMRS", 5.0),
    Channel(7,  462_712_500,  "FRS/GMRS", 5.0),
    Channel(15, 462_550_000,  "GMRS",    50.0),
    Channel(16, 462_575_000,  "GMRS",    50.0),
    Channel(17, 462_600_000,  "GMRS",    50.0),
    Channel(18, 462_625_000,  "GMRS",    50.0),
    Channel(19, 462_650_000,  "GMRS",    50.0),
    Channel(20, 462_675_000,  "GMRS",    50.0),
    Channel(21, 462_700_000,  "GMRS",    50.0),
    Channel(22, 462_725_000,  "GMRS",    50.0),
]

# 467 MHz block — FRS-only low-power channels (rarely used by kids)
CHANNELS_467: list[Channel] = [
    Channel(8,  467_562_500,  "FRS", 0.5),
    Channel(9,  467_587_500,  "FRS", 0.5),
    Channel(10, 467_612_500,  "FRS", 0.5),
    Channel(11, 467_637_500,  "FRS", 0.5),
    Channel(12, 467_662_500,  "FRS", 0.5),
    Channel(13, 467_687_500,  "FRS", 0.5),
    Channel(14, 467_712_500,  "FRS", 0.5),
]


def all_channels() -> list[Channel]:
    return CHANNELS_462 + CHANNELS_467


# Standard CTCSS tones (38 codes, Hz). Index = subtone code (1-based).
CTCSS_TONES_HZ: list[float] = [
    67.0,  71.9,  74.4,  77.0,  79.7,  82.5,  85.4,  88.5,  91.5,  94.8,
    97.4,  100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8,
    136.5, 141.3, 146.2, 151.4, 156.7, 162.2, 167.9, 173.8, 179.9, 186.2,
    192.8, 203.5, 210.7, 218.1, 225.7, 233.6, 241.8, 250.3,
]


def ctcss_code_for_tone(tone_hz: float, tolerance_hz: float = 1.5) -> int | None:
    """Return the subtone code (1-38) matching a measured tone, or None if no match."""
    for i, t in enumerate(CTCSS_TONES_HZ, start=1):
        if abs(t - tone_hz) <= tolerance_hz:
            return i
    return None
