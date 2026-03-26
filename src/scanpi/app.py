"""Main application — orchestrates all components."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn

from .api import create_app
from .classifier import Classifier
from .config import ScanConfig
from .db import ScanPiDB
from .scanner import Scanner
from .storage import StorageManager
from .surveyor import Surveyor
from .transcriber import Transcriber

log = logging.getLogger("scanpi")


class ScanPiApp:
    """Main orchestrator — starts all services."""

    def __init__(self, cfg: ScanConfig):
        self.cfg = cfg
        self.db = ScanPiDB(cfg.db_path)
        self.surveyor = Surveyor(cfg, self.db)
        self.classifier = Classifier(cfg, self.db)
        self.scanner = Scanner(cfg, self.db)
        self.transcriber = Transcriber(cfg, self.db)
        self.storage = StorageManager(cfg, self.db)
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        """Start all services."""
        log.info("ScanPi starting...")

        # Initialize database
        self.db.connect()
        log.info(f"Database: {self.cfg.db_path}")

        # Storage maintenance on startup
        self.storage.maintenance()

        # Create FastAPI app
        app = create_app(
            self.cfg, self.db,
            scanner=self.scanner,
            surveyor=self.surveyor,
            transcriber=self.transcriber,
            storage=self.storage,
        )

        # Start background services
        self._tasks = [
            asyncio.create_task(self._survey_then_scan(), name="survey_scan"),
            asyncio.create_task(self._transcription_loop(), name="transcribe"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance"),
        ]

        # Start web server
        config = uvicorn.Config(
            app,
            host=self.cfg.host,
            port=self.cfg.port,
            log_level="info",
            access_log=False,
        )
        server = uvicorn.Server(config)
        log.info(f"Web UI: http://{self.cfg.host}:{self.cfg.port}")

        try:
            await server.serve()
        finally:
            await self.shutdown()

    async def _survey_then_scan(self):
        """Phase 1: initial survey, then start scanning with periodic re-surveys."""
        log.info("Starting initial survey...")
        try:
            detections = await self.surveyor.full_survey()
            log.info(f"Initial survey found {len(detections)} signals")

            # Classify discovered frequencies
            if detections:
                log.info("Classifying discovered signals...")
                await self.classifier.classify_all_unknown()

            # Start scanning loop (interleaved with periodic surveys)
            scan_task = asyncio.create_task(self.scanner.start())
            survey_task = asyncio.create_task(self.surveyor.survey_loop())

            await asyncio.gather(scan_task, survey_task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Survey/scan error: {e}", exc_info=True)

    async def _transcription_loop(self):
        """Background transcription of recorded audio."""
        if not self.cfg.transcribe_enabled:
            log.info("Transcription disabled")
            return
        try:
            await self.transcriber.idle_loop()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Transcription error: {e}", exc_info=True)

    async def _maintenance_loop(self):
        """Periodic storage maintenance."""
        try:
            while True:
                await asyncio.sleep(3600)  # hourly
                self.storage.maintenance()
                # Decay activity scores for frequencies not seen recently
                await self._decay_scores()
        except asyncio.CancelledError:
            pass

    async def _decay_scores(self):
        """Reduce activity scores for frequencies not recently active."""
        import time
        freqs = self.db.get_frequencies()
        now = time.time()
        for f in freqs:
            hours_since = (now - f["last_seen"]) / 3600
            if hours_since > 24:
                old_score = f.get("activity_score", 0) or 0
                new_score = max(0, old_score * 0.9)  # 10% decay per day
                if abs(new_score - old_score) > 0.01:
                    self.db.update_activity_score(f["id"], new_score)

    async def shutdown(self):
        """Clean shutdown."""
        log.info("ScanPi shutting down...")
        self.scanner.stop()
        self.surveyor.stop()
        self.transcriber.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.db.close()
        log.info("ScanPi stopped")


def run(config_path=None):
    """Entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = ScanConfig.load(config_path)
    app = ScanPiApp(cfg)

    try:
        asyncio.run(app.start())
    except KeyboardInterrupt:
        log.info("Interrupted")
