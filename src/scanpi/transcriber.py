"""Transcriber — whisper.cpp or faster-whisper for on-device speech-to-text."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time

from .config import ScanConfig
from .db import ScanPiDB

log = logging.getLogger("scanpi.transcriber")


class Transcriber:
    """On-device transcription using whisper.cpp (preferred) or faster-whisper."""

    def __init__(self, cfg: ScanConfig, db: ScanPiDB):
        self.cfg = cfg
        self.db = db
        self._running = False
        self._backend: str | None = None
        self._whisper_bin: str | None = None
        self._fw_model = None

    def _detect_backend(self):
        """Find best available transcription backend."""
        # Prefer whisper.cpp (lighter on Pi)
        whisper_bin = shutil.which("whisper-cpp") or shutil.which("main")
        if whisper_bin:
            model_path = self.cfg.data_dir / "models" / f"ggml-{self.cfg.transcribe_model}.bin"
            if model_path.exists():
                self._backend = "whisper_cpp"
                self._whisper_bin = whisper_bin
                log.info(f"Using whisper.cpp: {whisper_bin}, model: {model_path}")
                return

        # Fall back to faster-whisper (Python)
        try:
            from faster_whisper import WhisperModel
            self._fw_model = WhisperModel(
                self.cfg.transcribe_model,
                device="cpu",
                compute_type="int8",
                cpu_threads=self.cfg.transcribe_threads,
            )
            self._backend = "faster_whisper"
            log.info(f"Using faster-whisper: {self.cfg.transcribe_model}")
            return
        except ImportError:
            pass

        log.warning("No transcription backend available (install whisper.cpp or faster-whisper)")
        self._backend = None

    async def transcribe_file(self, filepath: str) -> tuple[str, float] | None:
        """Transcribe a single audio file. Returns (text, confidence) or None."""
        if self._backend is None:
            self._detect_backend()
        if self._backend is None:
            return None

        if self._backend == "whisper_cpp":
            return await self._transcribe_cpp(filepath)
        else:
            return await self._transcribe_fw(filepath)

    async def _transcribe_cpp(self, filepath: str) -> tuple[str, float] | None:
        """Transcribe using whisper.cpp CLI."""
        model_path = self.cfg.data_dir / "models" / f"ggml-{self.cfg.transcribe_model}.bin"
        cmd = [
            self._whisper_bin,
            "-m", str(model_path),
            "-f", filepath,
            "-t", str(self.cfg.transcribe_threads),
            "--no-timestamps",
            "-l", "en",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            text = stdout.decode().strip()
            text = self._clean_transcript(text)
            if text:
                confidence = self._estimate_confidence(text)
                return text, confidence
        except asyncio.TimeoutError:
            log.warning(f"Transcription timeout: {filepath}")
            proc.kill()
        except Exception as e:
            log.error(f"whisper.cpp error: {e}")
        return None

    async def _transcribe_fw(self, filepath: str) -> tuple[str, float] | None:
        """Transcribe using faster-whisper (runs in thread pool to avoid blocking)."""
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, self._fw_transcribe_sync, filepath)
            return result
        except Exception as e:
            log.error(f"faster-whisper error: {e}")
            return None

    def _fw_transcribe_sync(self, filepath: str) -> tuple[str, float] | None:
        segments, info = self._fw_model.transcribe(
            filepath,
            language="en",
            beam_size=1,
            vad_filter=True,
        )
        texts = []
        total_prob = 0
        count = 0
        for seg in segments:
            texts.append(seg.text.strip())
            total_prob += seg.avg_log_prob
            count += 1

        if not texts:
            return None

        text = " ".join(texts)
        text = self._clean_transcript(text)
        if not text:
            return None

        avg_prob = total_prob / count if count > 0 else -1
        confidence = min(1.0, max(0.0, 1.0 + avg_prob))  # log_prob is negative
        return text, confidence

    def _clean_transcript(self, text: str) -> str:
        """Remove hallucinations and clean up transcript."""
        if not text:
            return ""
        # Common Whisper hallucinations
        hallucinations = [
            r"thank you for watching",
            r"thanks for watching",
            r"please subscribe",
            r"like and subscribe",
            r"\[music\]",
            r"\[applause\]",
            r"you$",  # trailing "you"
        ]
        for pattern in hallucinations:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        # Skip if too short after cleanup
        if len(text) < 3:
            return ""
        return text

    def _estimate_confidence(self, text: str) -> float:
        """Heuristic confidence for whisper.cpp output (no log_prob available)."""
        # Longer, coherent text = higher confidence
        words = text.split()
        if len(words) < 2:
            return 0.3
        if len(words) < 5:
            return 0.5
        return 0.7

    async def process_queue(self):
        """Process untranscribed recordings."""
        self._detect_backend()
        if self._backend is None:
            return

        recordings = self.db.get_untranscribed(limit=10)
        if not recordings:
            return

        log.info(f"Transcribing {len(recordings)} recordings")
        for rec in recordings:
            result = await self.transcribe_file(rec["filepath"])
            if result:
                text, confidence = result
                # Extract simple keywords
                keywords = self._extract_keywords(text)
                self.db.set_transcript(rec["id"], text, confidence, keywords)
                self.db.log_event("transcribed", rec["freq_hz"],
                                  f'{{"recording_id": {rec["id"]}, "words": {len(text.split())}}}')
                log.info(f"Transcribed rec#{rec['id']}: {text[:80]}...")

    def _extract_keywords(self, text: str) -> str:
        """Extract notable keywords from transcript."""
        alert_words = [
            "mayday", "emergency", "fire", "accident", "rescue",
            "coast guard", "pan pan", "securite", "police",
            "ambulance", "ems", "dispatch", "officer",
        ]
        found = [w for w in alert_words if w.lower() in text.lower()]
        return ",".join(found)

    async def idle_loop(self):
        """Background transcription loop — runs when scanner is between dwells."""
        self._running = True
        while self._running:
            await self.process_queue()
            await asyncio.sleep(30)  # check every 30s

    def stop(self):
        self._running = False
