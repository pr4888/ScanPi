"""Background Whisper transcription worker for GMRS audio clips.

Runs faster-whisper (tiny.en by default) on a worker thread pool so the
flowgraph + UI stay responsive. Updates the DB's transcript / transcript_status
columns when done.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class TranscribeJob:
    event_id: int
    clip_path: str
    channel: int


class TranscriptionWorker:
    """Single-worker thread that drains a queue of TranscribeJobs.

    Whisper model is lazy-loaded on first job (~30 s first time, cached after).
    If faster-whisper or the model fails to load, worker logs and marks jobs
    as 'failed' without crashing the service.
    """

    def __init__(
        self,
        on_result: Callable[[int, str | None, str], None],
        model_name: str = "tiny.en",
        model_dir: Path | None = None,
        min_duration_s: float = 0.5,
    ):
        self._on_result = on_result
        self._model_name = model_name
        self._model_dir = model_dir
        self._min_duration_s = min_duration_s
        self._q: queue.Queue[TranscribeJob | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._model = None
        self._model_failed = False

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="gmrs-transcriber", daemon=True,
        )
        self._thread.start()
        log.info("transcription worker started (model=%s)", self._model_name)

    def stop(self, timeout: float = 2.0):
        self._stop.set()
        self._q.put(None)  # unblock
        if self._thread:
            self._thread.join(timeout=timeout)

    def submit(self, job: TranscribeJob):
        """Enqueue a job for transcription. Non-blocking."""
        self._q.put(job)

    # -----------------------------------------------------------------

    def _ensure_model(self) -> bool:
        if self._model is not None:
            return True
        if self._model_failed:
            return False
        try:
            from faster_whisper import WhisperModel
            log.info("loading Whisper model '%s' (first call may download)...", self._model_name)
            kwargs = {"compute_type": "int8"}
            if self._model_dir is not None:
                kwargs["download_root"] = str(self._model_dir)
            self._model = WhisperModel(self._model_name, device="cpu", **kwargs)
            log.info("Whisper model loaded")
            return True
        except Exception:
            log.exception("failed to load Whisper model — transcription disabled")
            self._model_failed = True
            return False

    def _run(self):
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            if job is None:
                break
            try:
                self._process(job)
            except Exception:
                log.exception("transcription crashed on event %d", job.event_id)
                try:
                    self._on_result(job.event_id, None, "failed")
                except Exception:
                    log.exception("on_result callback also failed")

    def _process(self, job: TranscribeJob):
        p = Path(job.clip_path)
        if not p.exists():
            self._on_result(job.event_id, None, "missing")
            return
        # Skip trivially-short clips (save CPU)
        import wave
        try:
            with wave.open(str(p), "rb") as w:
                dur = w.getnframes() / float(w.getframerate())
        except Exception:
            self._on_result(job.event_id, None, "bad_wav")
            return
        if dur < self._min_duration_s:
            self._on_result(job.event_id, None, "too_short")
            return

        if not self._ensure_model():
            self._on_result(job.event_id, None, "no_model")
            return

        segments, _info = self._model.transcribe(
            str(p),
            language="en",
            beam_size=1,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        status = "ok" if text else "silent"
        self._on_result(job.event_id, text or None, status)
        log.info("transcribed ev=%d ch=%d (%.1fs) -> %r",
                 job.event_id, job.channel, dur, (text or "")[:60])
