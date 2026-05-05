"""Signal classifier — identifies analog FM vs P25 vs DMR vs NXDN."""
from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass

import numpy as np

from .config import ScanConfig
from .db import ScanPiDB

log = logging.getLogger("scanpi.classifier")

# Protocol sync words (bit patterns as bytes for correlation)
P25_FRAME_SYNC = bytes.fromhex("5575F5FF77FF")
DMR_BS_SYNC = bytes.fromhex("755FD7DF75F7")
DMR_MS_SYNC = bytes.fromhex("DFF57D75DF5D")

# CTCSS standard tones (Hz)
CTCSS_TONES = [
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
    94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0,
    127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2,
    165.5, 167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9,
    192.8, 196.6, 199.5, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1,
    233.6, 241.8, 250.3, 254.1,
]


@dataclass
class ClassificationResult:
    mode: str  # 'analog_fm', 'p25', 'dmr', 'nxdn', 'unknown_digital', 'data'
    confidence: float  # 0-1
    bandwidth_hz: int | None = None
    ctcss_tone: float | None = None
    detail: str = ""


class Classifier:
    """Classify signals by capturing short I/Q or FM-demodulated audio."""

    def __init__(self, cfg: ScanConfig, db: ScanPiDB):
        self.cfg = cfg
        self.db = db

    async def classify_frequency(self, freq_hz: int) -> ClassificationResult:
        """Capture a short sample and classify the signal."""
        # Capture 3 seconds of FM-demodulated audio
        audio = await self._capture_audio(freq_hz, duration_s=3)
        if audio is None or len(audio) < 48000:
            return ClassificationResult("unknown", 0.0, detail="no signal captured")

        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32)

        # Step 1: Check for CTCSS tone (subaudible, analog FM indicator)
        ctcss = self._detect_ctcss(samples, 48000)
        if ctcss is not None:
            return ClassificationResult(
                "analog_fm", 0.9,
                bandwidth_hz=12500,
                ctcss_tone=ctcss,
                detail=f"CTCSS {ctcss:.1f} Hz detected"
            )

        # Step 2: Check spectral characteristics
        spectral = self._analyze_spectrum(samples, 48000)

        # Step 3: Check for digital modulation signatures
        digital_score = self._detect_digital(samples, 48000)

        # Step 4: Estimate bandwidth
        bw = self._estimate_bandwidth(samples, 48000)

        # Decision logic
        if digital_score > 0.7:
            # Try to identify specific protocol
            proto = self._identify_protocol(samples, 48000)
            if proto:
                return ClassificationResult(
                    proto, digital_score,
                    bandwidth_hz=bw,
                    detail=f"digital signature score={digital_score:.2f}"
                )
            return ClassificationResult(
                "unknown_digital", digital_score,
                bandwidth_hz=bw,
                detail=f"digital but protocol unknown, score={digital_score:.2f}"
            )

        # Analog FM characteristics: voice-shaped spectrum, low flatness
        if spectral["flatness"] < 0.5 and spectral["voice_energy_ratio"] > 0.3:
            return ClassificationResult(
                "analog_fm", 0.6,
                bandwidth_hz=bw,
                detail=f"analog FM (spectral shape), flatness={spectral['flatness']:.2f}"
            )

        return ClassificationResult(
            "unknown", 0.3,
            bandwidth_hz=bw,
            detail=f"unclassified, flatness={spectral['flatness']:.2f}, digital={digital_score:.2f}"
        )

    async def _capture_audio(self, freq_hz: int, duration_s: float) -> bytes | None:
        """Capture FM-demodulated audio from rtl_fm."""
        cmd = [
            "rtl_fm",
            "-f", str(freq_hz),
            "-M", "fm",
            "-s", "48000",
            "-g", "40",
            "-p", str(self.cfg.sdr_ppm),
            "-d", str(self.cfg.sdr_device),
            "-",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            target_bytes = int(48000 * 2 * duration_s)
            data = await asyncio.wait_for(
                proc.stdout.read(target_bytes),
                timeout=duration_s + 5,
            )
            proc.kill()
            await proc.wait()
            return data
        except Exception as e:
            log.error(f"Capture failed for {freq_hz}: {e}")
            return None

    def _detect_ctcss(self, samples: np.ndarray, sr: int) -> float | None:
        """Detect CTCSS/PL tone in subaudible range (67-254 Hz)."""
        # Look only at low frequencies
        fft = np.fft.rfft(samples[:sr * 2])  # 2 seconds
        freqs = np.fft.rfftfreq(len(samples[:sr * 2]), 1.0 / sr)
        magnitudes = np.abs(fft)

        for tone in CTCSS_TONES:
            # Find bins near this tone (±2 Hz)
            mask = (freqs >= tone - 2) & (freqs <= tone + 2)
            if not mask.any():
                continue
            tone_power = np.max(magnitudes[mask])
            # Compare to nearby noise (tone ± 20 Hz, excluding tone itself)
            noise_mask = ((freqs >= tone - 20) & (freqs < tone - 5)) | \
                         ((freqs > tone + 5) & (freqs <= tone + 20))
            if not noise_mask.any():
                continue
            noise_power = np.median(magnitudes[noise_mask])
            if noise_power > 0 and tone_power / noise_power > 5.0:
                return tone
        return None

    def _analyze_spectrum(self, samples: np.ndarray, sr: int) -> dict:
        """Analyze spectral characteristics."""
        fft = np.fft.rfft(samples[:sr])
        magnitudes = np.abs(fft)
        freqs = np.fft.rfftfreq(len(samples[:sr]), 1.0 / sr)

        # Spectral flatness (Wiener entropy)
        mag_sq = magnitudes ** 2
        mag_sq = mag_sq[mag_sq > 0]
        if len(mag_sq) == 0:
            return {"flatness": 1.0, "voice_energy_ratio": 0.0}
        geo_mean = np.exp(np.mean(np.log(mag_sq + 1e-10)))
        arith_mean = np.mean(mag_sq)
        flatness = geo_mean / (arith_mean + 1e-10)

        # Voice energy ratio (300-3000 Hz vs total)
        voice_mask = (freqs >= 300) & (freqs <= 3000)
        total_energy = np.sum(mag_sq)
        voice_energy = np.sum(mag_sq[voice_mask]) if voice_mask.any() else 0
        voice_ratio = voice_energy / (total_energy + 1e-10)

        return {"flatness": float(flatness), "voice_energy_ratio": float(voice_ratio)}

    def _detect_digital(self, samples: np.ndarray, sr: int) -> float:
        """Score likelihood of digital modulation (0-1)."""
        # Digital signals have:
        # 1. High spectral flatness (uniform energy distribution)
        # 2. Constant envelope (low amplitude variance)
        # 3. Characteristic zero-crossing patterns

        # Amplitude variance (normalized)
        envelope = np.abs(samples)
        if envelope.mean() < 1:
            return 0.0
        amp_variance = envelope.std() / (envelope.mean() + 1e-10)

        # Zero-crossing rate
        signs = np.sign(samples)
        zcr = np.sum(np.abs(np.diff(signs)) > 0) / len(samples)

        # Digital signals: low amp_variance (constant envelope), high zcr
        digital_score = 0.0
        if amp_variance < 0.3:
            digital_score += 0.4
        if zcr > 0.3:
            digital_score += 0.3
        # Spectral flatness in voice band
        spec = self._analyze_spectrum(samples, sr)
        if spec["flatness"] > 0.6:
            digital_score += 0.3

        return min(1.0, digital_score)

    def _identify_protocol(self, samples: np.ndarray, sr: int) -> str | None:
        """Try to identify specific digital protocol from FM audio."""
        # This is a simplified heuristic — full protocol ID would use
        # sync word correlation on 4FSK-demodulated data
        # For now, use bandwidth + spectral characteristics

        bw = self._estimate_bandwidth(samples, sr)

        # P25: ~12.5 kHz, C4FM, 4800 baud
        # Check for 4800 baud symbol rate in the spectrum
        fft = np.fft.rfft(samples[:sr])
        freqs = np.fft.rfftfreq(len(samples[:sr]), 1.0 / sr)
        magnitudes = np.abs(fft)

        # Look for energy peak near 4800 Hz (symbol rate)
        mask_4800 = (freqs >= 4600) & (freqs <= 5000)
        mask_noise = (freqs >= 5500) & (freqs <= 7000)
        if mask_4800.any() and mask_noise.any():
            peak_4800 = np.max(magnitudes[mask_4800])
            noise = np.median(magnitudes[mask_noise])
            if noise > 0 and peak_4800 / noise > 3.0:
                return "p25"

        # DMR: 4800 baud with TDMA slot structure
        # Look for ~33.3ms (30 Hz) periodicity in amplitude
        mask_30 = (freqs >= 28) & (freqs <= 35)
        if mask_30.any():
            peak_30 = np.max(magnitudes[mask_30])
            if noise > 0 and peak_30 / noise > 3.0:
                return "dmr"

        return None

    def _estimate_bandwidth(self, samples: np.ndarray, sr: int) -> int:
        """Estimate signal bandwidth from spectral rolloff."""
        fft = np.fft.rfft(samples[:sr])
        power = np.abs(fft) ** 2
        freqs = np.fft.rfftfreq(len(samples[:sr]), 1.0 / sr)

        total_power = np.sum(power)
        if total_power < 1:
            return 12500  # default

        cumsum = np.cumsum(power) / total_power
        # Find 95% energy bandwidth
        idx_95 = np.searchsorted(cumsum, 0.95)
        bw_hz = freqs[min(idx_95, len(freqs) - 1)]

        # Round to standard bandwidths
        if bw_hz < 4000:
            return 6250
        elif bw_hz < 8000:
            return 12500
        elif bw_hz < 18000:
            return 25000
        else:
            return int(bw_hz)

    async def classify_all_unknown(self):
        """Classify all frequencies that haven't been classified yet."""
        freqs = self.db.get_frequencies()
        unknown = [f for f in freqs if not f.get("mode") or f["mode"] == "unknown"]
        log.info(f"Classifying {len(unknown)} unknown frequencies")

        for freq_info in unknown:
            result = await self.classify_frequency(freq_info["freq_hz"])
            if result.confidence > 0.3:
                self.db.classify_frequency(
                    freq_info["freq_hz"],
                    result.mode,
                    result.confidence,
                    result.bandwidth_hz,
                    result.ctcss_tone,
                )
                log.info(f"{freq_info['freq_hz']/1e6:.4f} MHz → {result.mode} "
                         f"(conf={result.confidence:.2f}) {result.detail}")
