"""Frequency coalescing — merge adjacent bins into actual channels."""
from __future__ import annotations

import logging
from .db import ScanPiDB

log = logging.getLogger("scanpi.coalesce")

# Standard channel spacings (Hz)
CHANNEL_SPACINGS = [
    6_250,    # NXDN narrowband
    12_500,   # NFM, P25, DMR
    25_000,   # WFM (marine VHF, NOAA)
    50_000,   # FM broadcast sub-channels
    200_000,  # FM broadcast
]

# Known frequency allocations (US) for smart labeling
KNOWN_BANDS = [
    (30_000_000, 50_000_000, "VHF Low Band", "land_mobile"),
    (136_000_000, 138_000_000, "NOAA Satellites", "satellite"),
    (144_000_000, 148_000_000, "2m Amateur", "amateur"),
    (148_000_000, 150_800_000, "Federal/Military", "federal"),
    (150_800_000, 154_000_000, "Land Mobile", "land_mobile"),
    (154_000_000, 156_000_000, "Fire/EMS/Public Safety", "public_safety"),
    (156_000_000, 157_425_000, "Marine VHF", "marine"),
    (157_425_000, 162_000_000, "Marine VHF / Land Mobile", "marine"),
    (162_400_000, 162_550_000, "NOAA Weather", "weather"),
    (406_000_000, 420_000_000, "Federal", "federal"),
    (420_000_000, 450_000_000, "UHF Amateur / Land Mobile", "land_mobile"),
    (450_000_000, 470_000_000, "UHF Business/Industrial", "business"),
    (462_562_500, 462_725_000, "GMRS", "gmrs"),
    (467_562_500, 467_725_000, "FRS/GMRS", "gmrs"),
    (769_000_000, 775_000_000, "700 MHz Public Safety", "public_safety"),
    (806_000_000, 824_000_000, "800 MHz Public Safety/SMR", "public_safety"),
    (851_000_000, 869_000_000, "800 MHz Cellular/Pager", "cellular"),
    (869_000_000, 894_000_000, "Cellular", "cellular"),
    (896_000_000, 902_000_000, "900 MHz SMR", "smr"),
    (935_000_000, 940_000_000, "900 MHz Paging", "paging"),
]


def identify_band(freq_hz: int) -> tuple[str, str]:
    """Return (band_name, service_type) for a frequency."""
    for low, high, name, svc in KNOWN_BANDS:
        if low <= freq_hz <= high:
            return name, svc
    return "Unknown", "unknown"


def coalesce_frequencies(db: ScanPiDB, merge_distance_hz: int = 15_000):
    """Merge adjacent frequency bins into single channel entries.

    Adjacent bins within merge_distance_hz are grouped.
    The strongest bin becomes the representative frequency.
    Weaker bins get disabled (still in DB but not scanned).
    """
    freqs = db.get_frequencies()
    if not freqs:
        return 0

    # Sort by frequency
    freqs.sort(key=lambda f: f["freq_hz"])

    groups = []
    current_group = [freqs[0]]

    for f in freqs[1:]:
        if f["freq_hz"] - current_group[-1]["freq_hz"] <= merge_distance_hz:
            current_group.append(f)
        else:
            groups.append(current_group)
            current_group = [f]
    groups.append(current_group)

    merged = 0
    for group in groups:
        if len(group) <= 1:
            # Single bin — just add band label if missing
            f = group[0]
            if not f.get("label"):
                band_name, svc = identify_band(f["freq_hz"])
                db.label_frequency(f["freq_hz"], band_name)
            continue

        # Find strongest bin in group
        strongest = max(group, key=lambda f: f.get("peak_power_db") or f.get("avg_power_db") or -99)

        # Label the strongest with band info
        band_name, svc = identify_band(strongest["freq_hz"])
        if not strongest.get("label"):
            db.label_frequency(strongest["freq_hz"], band_name)

        # Merge observations into strongest
        total_obs = sum(f.get("observation_count", 1) for f in group)
        best_score = max(f.get("activity_score", 0) or 0 for f in group)

        with db.cursor() as c:
            c.execute("""
                UPDATE frequencies SET
                    observation_count = ?,
                    activity_score = ?,
                    bandwidth_hz = ?
                WHERE freq_hz = ?
            """, (total_obs, best_score,
                  group[-1]["freq_hz"] - group[0]["freq_hz"],  # estimated BW
                  strongest["freq_hz"]))

        # Disable weaker bins (keep in DB for noise floor)
        for f in group:
            if f["freq_hz"] != strongest["freq_hz"]:
                with db.cursor() as c:
                    c.execute("UPDATE frequencies SET enabled = 0 WHERE freq_hz = ?",
                              (f["freq_hz"],))
                merged += 1

    log.info(f"Coalesced: {len(freqs)} bins → {len(groups)} channels ({merged} merged)")
    return len(groups)


def auto_label_channels(db: ScanPiDB):
    """Apply band-based labels to unlabeled frequencies."""
    freqs = db.get_frequencies(enabled_only=True)
    labeled = 0
    for f in freqs:
        if f.get("label"):
            continue
        band_name, svc = identify_band(f["freq_hz"])
        db.label_frequency(f["freq_hz"], band_name)
        labeled += 1
    if labeled:
        log.info(f"Auto-labeled {labeled} frequencies")
