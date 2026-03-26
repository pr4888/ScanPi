"""Trunking support — P25/DMR trunking via OP25 integration."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path

from .config import ScanConfig
from .db import ScanPiDB

log = logging.getLogger("scanpi.trunking")

# Known P25 control channel bands (US)
P25_BANDS = [
    (769_000_000, 775_000_000, "700 MHz rebanded"),
    (851_000_000, 869_000_000, "800 MHz conventional/trunked"),
    (935_000_000, 940_000_000, "900 MHz trunked"),
]


class TrunkingManager:
    """Manages P25 trunking decoder (OP25) for police/fire/EMS."""

    def __init__(self, cfg: ScanConfig, db: ScanPiDB):
        self.cfg = cfg
        self.db = db
        self._op25_proc: asyncio.subprocess.Process | None = None
        self._running = False
        self._op25_dir: Path | None = None
        self._config_dir = cfg.data_dir / "trunking"
        self._config_dir.mkdir(parents=True, exist_ok=True)

    def detect_op25(self) -> bool:
        """Check if OP25 is installed."""
        candidates = [
            Path.home() / "op25" / "op25" / "gr-op25_repeater" / "apps",
            Path("/opt/op25/op25/gr-op25_repeater/apps"),
        ]
        for p in candidates:
            if (p / "multi_rx.py").exists():
                self._op25_dir = p
                log.info(f"OP25 found at: {p}")
                return True
        log.warning("OP25 not installed — trunking unavailable. Install: https://github.com/boatbod/op25")
        return False

    async def discover_control_channels(self) -> list[dict]:
        """Scan P25 bands for control channels (always-transmitting, high duty cycle)."""
        discovered = []

        for band_start, band_end, band_name in P25_BANDS:
            log.info(f"Scanning for P25 control channels in {band_name} ({band_start/1e6:.0f}-{band_end/1e6:.0f} MHz)")

            # Get frequencies we already discovered in these bands
            freqs = self.db.get_frequencies()
            candidates = [
                f for f in freqs
                if band_start <= f["freq_hz"] <= band_end
                and (f.get("peak_power_db") or -99) > -30  # reasonably strong
            ]

            if not candidates:
                continue

            # Sort by power — control channels are usually strongest
            candidates.sort(key=lambda f: -(f.get("peak_power_db") or -99))

            # Test top candidates for continuous transmission
            for freq_info in candidates[:10]:
                freq_hz = freq_info["freq_hz"]
                is_control = await self._test_control_channel(freq_hz)
                if is_control:
                    discovered.append({
                        "freq_hz": freq_hz,
                        "freq_mhz": freq_hz / 1e6,
                        "band": band_name,
                        "power_db": freq_info.get("peak_power_db", -99),
                    })
                    log.info(f"P25 control channel candidate: {freq_hz/1e6:.4f} MHz ({band_name})")

        return discovered

    async def _test_control_channel(self, freq_hz: int) -> bool:
        """Test if a frequency is a P25 control channel (continuous digital signal)."""
        # Capture 2 seconds and check for continuous transmission
        cmd = [
            "rtl_fm", "-f", str(freq_hz), "-M", "fm", "-s", "48000",
            "-g", "40", "-p", str(self.cfg.sdr_ppm), "-d", str(self.cfg.sdr_device),
            "-",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            data = await asyncio.wait_for(proc.stdout.read(48000 * 2 * 2), timeout=5)
            proc.kill()
            await proc.wait()

            if len(data) < 48000:
                return False

            import numpy as np
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)

            # Control channels have:
            # 1. High energy (always transmitting)
            rms = np.sqrt(np.mean(samples ** 2))
            if rms < 500:  # too quiet
                return False

            # 2. High zero-crossing rate (digital modulation)
            zcr = np.sum(np.abs(np.diff(np.sign(samples))) > 0) / len(samples)
            if zcr < 0.2:  # too low for digital
                return False

            # 3. Low amplitude variance (constant envelope like C4FM)
            envelope = np.abs(samples)
            amp_var = envelope.std() / (envelope.mean() + 1e-10)
            if amp_var > 0.5:  # too variable for constant envelope
                return False

            return True

        except Exception as e:
            log.debug(f"Control channel test failed for {freq_hz}: {e}")
            return False

    def generate_op25_config(self, control_channels: list[dict],
                             system_name: str = "Local") -> Path:
        """Generate OP25 multi_rx config for discovered control channels."""
        config = {
            "devices": [{
                "args": f"rtl={self.cfg.sdr_device}",
                "gains": f"LNA:{self.cfg.sdr_gain if self.cfg.sdr_gain != 'auto' else '40'}",
                "gain_mode": False,
                "name": "sdr0",
                "offset": 0,
                "ppm": float(self.cfg.sdr_ppm),
                "rate": 2400000,
                "usable_bw_pct": 0.85,
                "tunable": True,
            }],
            "channels": [{
                "demod_type": "cqpsk",
                "destination": f"udp://127.0.0.1:2345",
                "filter_type": "rc",
                "frequency": ch["freq_hz"],
                "if_rate": 24000,
                "name": f"{system_name}_{i}",
                "phase2_tdma": False,
                "source": "sdr0",
                "trunked": True,
            } for i, ch in enumerate(control_channels)],
        }

        config_path = self._config_dir / "scanpi_trunking.json"
        config_path.write_text(json.dumps(config, indent=2))

        # Also create empty talkgroup file for labeling
        tg_path = self._config_dir / "talkgroups.tsv"
        if not tg_path.exists():
            tg_path.write_text(
                "# Talkgroup\tTag\tDescription\n"
                "# Add talkgroups here as you discover them\n"
                "# Format: TGID<tab>TAG<tab>Description\n"
            )

        log.info(f"OP25 config generated: {config_path}")
        return config_path

    async def start_op25(self, config_path: Path | None = None):
        """Start OP25 multi_rx.py for P25 trunking."""
        if not self._op25_dir:
            if not self.detect_op25():
                return

        config_path = config_path or self._config_dir / "scanpi_trunking.json"
        if not config_path.exists():
            log.error(f"OP25 config not found: {config_path}")
            return

        cmd = [
            "python3", "multi_rx.py",
            "-v", "1",
            "-c", str(config_path),
        ]

        log.info(f"Starting OP25: {' '.join(cmd)}")
        self._running = True

        try:
            self._op25_proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self._op25_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            # Read OP25 output and log talkgroup activity
            async for line in self._op25_proc.stdout:
                if not self._running:
                    break
                text = line.decode().strip()
                if text:
                    self._parse_op25_output(text)

        except Exception as e:
            log.error(f"OP25 error: {e}")
        finally:
            self._running = False

    def _parse_op25_output(self, line: str):
        """Parse OP25 output for talkgroup activity."""
        # OP25 multi_rx outputs voice channel grants, talkgroup IDs
        if "voice update" in line.lower() or "tg=" in line.lower():
            self.db.log_event("trunking_activity", detail=line[:200])

    async def stop_op25(self):
        """Stop OP25."""
        self._running = False
        if self._op25_proc:
            self._op25_proc.terminate()
            try:
                await asyncio.wait_for(self._op25_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._op25_proc.kill()
            self._op25_proc = None
            log.info("OP25 stopped")

    def get_status(self) -> dict:
        return {
            "op25_installed": self._op25_dir is not None,
            "op25_running": self._op25_proc is not None and self._op25_proc.returncode is None,
            "config_exists": (self._config_dir / "scanpi_trunking.json").exists(),
            "talkgroups_file": str(self._config_dir / "talkgroups.tsv"),
        }
