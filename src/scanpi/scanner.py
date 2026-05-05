"""Scanner — tunes SDR to discovered frequencies, records with VAD gating."""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import tempfile
import time
import wave
from pathlib import Path

from .config import ScanConfig
from .db import ScanPiDB

log = logging.getLogger("scanpi.scanner")

AUDIO_RATE = 48000  # output sample rate for rtl_fm


class Scanner:
    """Priority-queue scanner with VAD-gated recording."""

    def __init__(self, cfg: ScanConfig, db: ScanPiDB):
        self.cfg = cfg
        self.db = db
        self._running = False
        self._current_freq: int | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._vad = None  # lazy-loaded Silero VAD

    @property
    def current_freq(self) -> int | None:
        return self._current_freq

    async def start(self):
        """Main scanner loop — cycles through frequency queue."""
        self._running = True
        self._load_vad()
        log.info("Scanner started")

        while self._running:
            queue = self.db.get_scan_queue(limit=50)
            if not queue:
                log.info("No frequencies in catalog — waiting for survey")
                await asyncio.sleep(10)
                continue

            for freq_info in queue:
                if not self._running:
                    break
                freq_hz = freq_info["freq_hz"]
                dwell = self._calc_dwell(freq_info)
                await self._dwell_on(freq_hz, dwell, freq_info)

    async def _dwell_on(self, freq_hz: int, dwell_s: float, freq_info: dict):
        """Tune to frequency, record if voice detected."""
        self._current_freq = freq_hz
        mode = freq_info.get("mode", "analog_fm") or "analog_fm"

        # Build rtl_fm command based on mode
        cmd = self._build_rtl_cmd(freq_hz, mode)

        try:
            import os
            env = {**os.environ, "LD_LIBRARY_PATH": "/usr/local/lib:" + os.environ.get("LD_LIBRARY_PATH", "")}
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            self._process = proc

            # Read audio for dwell period
            audio_chunks = []
            start = time.monotonic()
            chunk_size = AUDIO_RATE * 2  # 1 second of 16-bit mono

            while time.monotonic() - start < dwell_s and self._running:
                try:
                    data = await asyncio.wait_for(
                        proc.stdout.read(chunk_size),
                        timeout=2.0
                    )
                    if not data:
                        break
                    audio_chunks.append(data)
                except asyncio.TimeoutError:
                    break

            proc.kill()
            await proc.wait()
            self._process = None
            await asyncio.sleep(0.3)  # let SDR USB settle between tunes

        except FileNotFoundError:
            log.error("rtl_fm not found")
            self._running = False
            return
        except Exception as e:
            log.error(f"Scanner error on {freq_hz}: {type(e).__name__}: {e}")
            await asyncio.sleep(0.5)  # let SDR settle after error
            return

        if not audio_chunks:
            log.debug(f"{freq_hz/1e6:.4f}: no audio data")
            return

        raw_audio = b"".join(audio_chunks)
        duration = len(raw_audio) / (AUDIO_RATE * 2)
        if len(raw_audio) < AUDIO_RATE:  # less than 0.5s
            log.debug(f"{freq_hz/1e6:.4f}: too short ({duration:.1f}s)")
            return

        # Energy check
        energy = self._calc_energy(raw_audio)
        if energy < self.cfg.energy_threshold_db:
            log.debug(f"{freq_hz/1e6:.4f}: below energy threshold ({energy:.1f} < {self.cfg.energy_threshold_db})")
            return

        # VAD check
        vad_score = self._run_vad(raw_audio)
        if self.cfg.vad_enabled and vad_score < self.cfg.vad_threshold:
            log.debug(f"{freq_hz/1e6:.4f}: VAD reject ({vad_score:.2f} < {self.cfg.vad_threshold})")
            return

        log.info(f"{freq_hz/1e6:.4f} MHz: VOICE detected! energy={energy:.1f}dB vad={vad_score:.2f} duration={duration:.1f}s")
        # Save recording
        await self._save_recording(freq_hz, freq_info, raw_audio, energy, vad_score)

    def _build_rtl_cmd(self, freq_hz: int, mode: str) -> list[str]:
        """Build rtl_fm command for the given frequency and mode."""
        cmd = [
            "rtl_fm",
            "-f", str(freq_hz),
            "-M", "fm",
            "-s", str(AUDIO_RATE),
            "-g", str(self.cfg.sdr_gain) if self.cfg.sdr_gain != "auto" else "40",
            "-p", str(self.cfg.sdr_ppm),
            "-d", str(self.cfg.sdr_device),
            "-l", str(self.cfg.squelch_level),
            "-E", "deemp",
        ]
        # Wideband for marine VHF
        if freq_hz >= 156_000_000 and freq_hz <= 162_000_000:
            cmd.extend(["-W"])  # wideband FM
        return cmd

    def _calc_dwell(self, freq_info: dict) -> float:
        """Calculate dwell time based on activity score."""
        if not self.cfg.adaptive_dwell:
            return self.cfg.dwell_time_s
        score = freq_info.get("activity_score", 0) or 0
        # More active = longer dwell (1.5x), less active = shorter (0.5x)
        multiplier = 0.5 + score
        return max(2.0, min(15.0, self.cfg.dwell_time_s * multiplier))

    def _calc_energy(self, raw_audio: bytes) -> float:
        """Calculate audio energy in dB."""
        import numpy as np
        samples = np.frombuffer(raw_audio, dtype=np.int16).astype(np.float32)
        if len(samples) == 0:
            return -100.0
        rms = np.sqrt(np.mean(samples ** 2))
        if rms < 1:
            return -100.0
        return 20 * np.log10(rms / 32768)

    def _load_vad(self):
        """Load Silero VAD ONNX model if available."""
        try:
            import onnxruntime as ort
            model_path = self.cfg.data_dir / "models" / "silero_vad.onnx"
            if model_path.exists():
                self._vad = ort.InferenceSession(
                    str(model_path),
                    providers=["CPUExecutionProvider"]
                )
                log.info("Silero VAD loaded")
            else:
                log.warning(f"VAD model not found at {model_path} — recording without VAD")
        except ImportError:
            log.warning("onnxruntime not installed — recording without VAD")

    def _run_vad(self, raw_audio: bytes) -> float:
        """Run Silero VAD on audio, return max speech probability."""
        if self._vad is None:
            return 1.0  # pass through if no VAD

        import numpy as np
        samples = np.frombuffer(raw_audio, dtype=np.int16).astype(np.float32) / 32768.0

        # Silero expects 16kHz — downsample from 48kHz
        samples_16k = samples[::3]

        # Process in 512-sample windows (32ms at 16kHz)
        window = 512
        max_prob = 0.0
        h = np.zeros((2, 1, 64), dtype=np.float32)
        c = np.zeros((2, 1, 64), dtype=np.float32)

        for i in range(0, len(samples_16k) - window, window):
            chunk = samples_16k[i:i + window].reshape(1, -1)
            ort_inputs = {
                "input": chunk,
                "h": h, "c": c,
                "sr": np.array([16000], dtype=np.int64),
            }
            try:
                out, h_out, c_out = self._vad.run(None, ort_inputs)
                h, c = h_out, c_out
                prob = float(out[0][0])
                max_prob = max(max_prob, prob)
            except Exception:
                break

        return max_prob

    async def _save_recording(self, freq_hz: int, freq_info: dict,
                              raw_audio: bytes, energy: float, vad_score: float):
        """Save audio as WAV and add to database."""
        freq_id = freq_info["id"]
        ts = time.strftime("%Y%m%d_%H%M%S")
        freq_label = (freq_info.get("label") or f"{freq_hz / 1e6:.4f}MHz").replace("/", "-")
        filename = f"{ts}_{freq_label}.wav"
        filepath = self.cfg.recordings_dir / filename

        # Write WAV
        with wave.open(str(filepath), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(AUDIO_RATE)
            wf.writeframes(raw_audio)

        size = filepath.stat().st_size
        duration = len(raw_audio) / (AUDIO_RATE * 2)

        rec_id = self.db.add_recording(
            freq_id=freq_id, freq_hz=freq_hz,
            filepath=str(filepath), duration_s=duration,
            size_bytes=size, vad_confidence=vad_score, energy_db=energy
        )

        # Update activity score (simple exponential moving average)
        old_score = freq_info.get("activity_score", 0) or 0
        new_score = 0.3 + 0.7 * old_score  # bump toward 1.0 on each recording
        self.db.update_activity_score(freq_id, min(1.0, new_score))

        self.db.log_event("recording_saved", freq_hz,
                          f'{{"id": {rec_id}, "duration": {duration:.1f}, "vad": {vad_score:.2f}}}')
        log.info(f"Saved: {filename} ({duration:.1f}s, VAD={vad_score:.2f})")

    def stop(self):
        self._running = False
        if self._process:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
