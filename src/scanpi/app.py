"""Main application — orchestrates all components."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import uvicorn

from .api import create_app
from .classifier import Classifier
from .coalesce import coalesce_frequencies, auto_label_channels
from .config import ScanConfig
from .db import ScanPiDB
from .scanner import Scanner
from .storage import StorageManager
from .surveyor import Surveyor
from .transcriber import Transcriber
from .trunking import TrunkingManager

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
        self.trunking = TrunkingManager(cfg, self.db)
        self.storage = StorageManager(cfg, self.db)
        self._sdr_lock = asyncio.Lock()  # single SDR mutex
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        """Start all services."""
        log.info("ScanPi starting...")

        # Initialize database
        self.db.connect()
        log.info(f"Database: {self.cfg.db_path}")

        # Storage maintenance on startup
        self.storage.maintenance()

        # Check for OP25/trunking support
        self.trunking.detect_op25()

        # Create FastAPI app
        app = create_app(
            self.cfg, self.db,
            scanner=self.scanner,
            surveyor=self.surveyor,
            transcriber=self.transcriber,
            trunking=self.trunking,
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
        """Single-SDR orchestrator: survey → coalesce → scan loop (with periodic re-surveys)."""
        try:
            # Phase 1: Initial survey (SDR exclusive)
            stats = self.db.get_stats()
            if stats["total_frequencies"] == 0:
                log.info("No frequencies in DB — running initial survey...")
                detections = await self.surveyor.full_survey()
                log.info(f"Initial survey found {len(detections)} signals")
                if detections:
                    channels = coalesce_frequencies(self.db)
                    auto_label_channels(self.db)
                    log.info(f"Coalesced to {channels} channels")
            else:
                log.info(f"DB has {stats['total_frequencies']} frequencies — skipping initial survey")

            # Phase 2: Scan loop — scanner owns the SDR, yields periodically for re-survey
            scan_cycles = 0
            while self.scanner._running or not scan_cycles:
                # Scan for N cycles
                queue = self.db.get_scan_queue(limit=50)
                if not queue:
                    log.info("No scannable frequencies — running survey")
                    await self.surveyor.full_survey()
                    coalesce_frequencies(self.db)
                    auto_label_channels(self.db)
                    await asyncio.sleep(10)
                    continue

                self.scanner._running = True
                log.info(f"Scanning {len(queue)} frequencies...")
                for freq_info in queue:
                    if not self.scanner._running:
                        break
                    freq_hz = freq_info["freq_hz"]
                    dwell = self.scanner._calc_dwell(freq_info)
                    await self.scanner._dwell_on(freq_hz, dwell, freq_info)

                scan_cycles += 1

                # Every 10 cycles, yield SDR for a re-survey
                if scan_cycles % 10 == 0:
                    log.info("Pausing scanner for re-survey...")
                    self.scanner._current_freq = None
                    await self.surveyor.full_survey()
                    coalesce_frequencies(self.db)

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
