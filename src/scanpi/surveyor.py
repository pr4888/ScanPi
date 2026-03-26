"""Spectrum surveyor — wideband power sweeps via rtl_power to discover active frequencies."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from dataclasses import dataclass

from .config import ScanConfig, BandRange
from .db import ScanPiDB

log = logging.getLogger("scanpi.surveyor")


@dataclass
class SignalDetection:
    freq_hz: int
    power_db: float
    noise_floor_db: float
    snr_db: float
    bandwidth_hz: int | None = None


class Surveyor:
    """Runs rtl_power sweeps and updates the frequency catalog."""

    def __init__(self, cfg: ScanConfig, db: ScanPiDB):
        self.cfg = cfg
        self.db = db
        self._running = False

    async def run_sweep(self, band: BandRange) -> list[SignalDetection]:
        """Run a single rtl_power sweep on a band range."""
        start_hz = int(band.start_mhz * 1e6)
        end_hz = int(band.end_mhz * 1e6)
        bin_size = 10_000  # 10 kHz bins

        cmd = [
            "rtl_power",
            "-f", f"{start_hz}:{end_hz}:{bin_size}",
            "-g", str(self.cfg.sdr_gain) if self.cfg.sdr_gain != "auto" else "40",
            "-i", "5",   # 5 second integration
            "-e", "10",  # 10 seconds total (2 passes)
            "-p", str(self.cfg.sdr_ppm),
            "-d", str(self.cfg.sdr_device),
            "-",         # stdout
        ]

        log.info(f"Surveying {band.name}: {band.start_mhz}-{band.end_mhz} MHz")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            log.warning(f"Survey timeout on {band.name}")
            proc.kill()
            return []
        except FileNotFoundError:
            log.error("rtl_power not found — install librtlsdr")
            return []

        if proc.returncode != 0:
            log.warning(f"rtl_power exit {proc.returncode}: {stderr.decode()[:200]}")
            return []

        return self._parse_and_detect(stdout.decode(), band)

    def _parse_and_detect(self, csv_data: str, band: BandRange) -> list[SignalDetection]:
        """Parse rtl_power CSV output and detect signals above noise floor."""
        # rtl_power CSV: date, time, freq_low, freq_high, bin_hz, samples, db1, db2, ...
        power_by_freq: dict[int, list[float]] = {}

        reader = csv.reader(io.StringIO(csv_data))
        for row in reader:
            if len(row) < 7:
                continue
            try:
                freq_low = int(float(row[2]))
                freq_high = int(float(row[3]))
                bin_hz = float(row[4])
                powers = [float(x) for x in row[6:] if x.strip()]
            except (ValueError, IndexError):
                continue

            for i, pwr in enumerate(powers):
                freq = freq_low + int(i * bin_hz)
                power_by_freq.setdefault(freq, []).append(pwr)

        if not power_by_freq:
            return []

        # Average power per frequency bin
        avg_power = {f: sum(ps) / len(ps) for f, ps in power_by_freq.items()}

        # Update noise floor in DB
        for freq_hz, pwr in avg_power.items():
            self.db.update_noise_floor(freq_hz, pwr)

        # Detect signals above threshold
        detections = []
        all_powers = list(avg_power.values())
        median_power = sorted(all_powers)[len(all_powers) // 2]

        for freq_hz, pwr in avg_power.items():
            noise = self.db.get_noise_floor(freq_hz) or median_power
            snr = pwr - noise
            if snr >= self.cfg.detection_threshold_db:
                det = SignalDetection(
                    freq_hz=freq_hz,
                    power_db=pwr,
                    noise_floor_db=noise,
                    snr_db=snr,
                )
                detections.append(det)
                # Add to catalog
                self.db.upsert_frequency(freq_hz, pwr)
                self.db.log_event("signal_detected", freq_hz,
                                  f'{{"snr": {snr:.1f}, "band": "{band.name}"}}')

        log.info(f"{band.name}: {len(detections)} signals above {self.cfg.detection_threshold_db} dB threshold")
        return detections

    async def full_survey(self) -> list[SignalDetection]:
        """Survey all enabled bands sequentially (single SDR)."""
        all_detections = []
        for band in self.cfg.survey_bands:
            if not band.enabled:
                continue
            dets = await self.run_sweep(band)
            all_detections.extend(dets)
        log.info(f"Full survey complete: {len(all_detections)} total signals detected")
        return all_detections

    async def survey_loop(self):
        """Continuous survey loop — runs between scanner dwells."""
        self._running = True
        log.info("Survey loop started")
        while self._running:
            try:
                await self.full_survey()
            except Exception as e:
                log.error(f"Survey error: {e}")
            await asyncio.sleep(self.cfg.survey_interval_min * 60)

    def stop(self):
        self._running = False
