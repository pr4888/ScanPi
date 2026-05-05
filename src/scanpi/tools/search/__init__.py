"""Search tool — hybrid lexical (FTS5) + semantic (bge-small) transcript search.

Indexes the read-only gmrs.db and op25.db sibling tools; owns its own search.db
which holds an FTS5 virtual table and (optionally) embedding vectors.

INTEGRATION HOOK — add these 3 lines to src/scanpi/app_v3.py inside run_v3(),
right after the GmrsTool / OP25Tool / YardstickTool registrations
(around line 388 of app_v3.py):

    from .tools.search import SearchTool
    registry.register(SearchTool(config={"data_dir": str(data_dir)}))
    # SearchTool has needs_sdr=False so it auto-starts via coord.start_non_sdr_tools().
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from ...tools import Tool, ToolStatus
from .api import build_router
from .db import SearchDB

log = logging.getLogger(__name__)


def _feature_enabled(name: str) -> bool:
    """Profile feature flag — fall back to env var if profile.py not yet shipped."""
    try:
        from ...profile import feature_enabled  # type: ignore
        return bool(feature_enabled(name))
    except Exception:
        return os.environ.get(f"SCANPI_FEATURE_{name.upper()}", "0") == "1"


class SearchTool(Tool):
    id = "search"
    name = "Search"
    description = "Hybrid lexical + semantic transcript search across all sources"
    needs_sdr = False

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        cfg = self.config
        data_dir = Path(cfg.get("data_dir", Path.home() / "scanpi"))
        self._data_dir = data_dir
        self._search_db_path = data_dir / "search.db"
        self._gmrs_db_path = Path(cfg.get("gmrs_db", data_dir / "gmrs.db"))
        self._op25_db_path = Path(cfg.get("op25_db", data_dir / "op25.db"))

        # Sync interval for the FTS5 reindex thread
        self._sync_interval_s = float(cfg.get("sync_interval_s", 10.0))

        # Backfill / embedding worker config
        self._backfill_limit = int(cfg.get("backfill_limit", 5000))
        self._embed_batch_size = int(cfg.get("embed_batch_size", 16))
        self._model_name = str(cfg.get("model_name", "BAAI/bge-small-en-v1.5"))
        self._model_dir = Path(cfg.get(
            "model_dir",
            Path.home() / "scanpi" / "models" / "bge-small-en-v1.5",
        ))

        # Will be populated in start()
        self._db: SearchDB | None = None
        self._sync_thread: threading.Thread | None = None
        self._embed_worker: Any | None = None  # EmbeddingWorker, lazy import
        self._stop_evt = threading.Event()
        self._semantic_enabled = _feature_enabled("semantic_search")
        self._semantic_status = "disabled"  # 'disabled' | 'loading' | 'ready' | 'failed'
        self._started_ts: float | None = None
        self._last_sync_ts: float | None = None
        self._last_sync_added = 0
        self._router: APIRouter | None = None

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        # Open our own DB and create FTS5 virtual table + watermarks
        self._db = SearchDB(
            self._search_db_path,
            gmrs_db_path=self._gmrs_db_path,
            op25_db_path=self._op25_db_path,
        )
        self._db.connect()

        self._stop_evt.clear()
        self._sync_thread = threading.Thread(
            target=self._sync_loop, name="search-fts-sync", daemon=True,
        )
        self._sync_thread.start()

        if self._semantic_enabled:
            try:
                from .embed import EmbeddingWorker
                self._embed_worker = EmbeddingWorker(
                    db=self._db,
                    model_dir=self._model_dir,
                    model_name=self._model_name,
                    backfill_limit=self._backfill_limit,
                    batch_size=self._embed_batch_size,
                    on_status=self._on_embed_status,
                )
                self._embed_worker.start()
                self._semantic_status = "loading"
            except Exception:
                log.exception("embedding worker failed to start; semantic search disabled")
                self._semantic_status = "failed"
        else:
            self._semantic_status = "disabled"
            log.info("semantic_search profile flag is off — running lexical-only")

        self._started_ts = time.time()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._embed_worker is not None:
            try:
                self._embed_worker.stop()
            except Exception:
                log.exception("embed worker stop failed")
            self._embed_worker = None
        if self._sync_thread is not None:
            self._sync_thread.join(timeout=2.0)
            self._sync_thread = None
        # Keep DB open for historical browsing if needed; close on full shutdown.
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None
        self._started_ts = None

    # --- workers --------------------------------------------------------

    def _on_embed_status(self, status: str):
        """Callback from EmbeddingWorker."""
        self._semantic_status = status

    def _sync_loop(self):
        """Poll the source DBs every N seconds, reindex new rows into FTS5."""
        while not self._stop_evt.is_set():
            try:
                added = self._db.sync_fts()
                self._last_sync_ts = time.time()
                self._last_sync_added = added
                if added:
                    log.debug("FTS5 sync added %d rows", added)
            except Exception:
                log.exception("FTS5 sync failed")
            # Sleep in small increments so stop() returns quickly.
            for _ in range(int(self._sync_interval_s * 10)):
                if self._stop_evt.is_set():
                    return
                time.sleep(0.1)

    # --- status / summary ----------------------------------------------

    def status(self) -> ToolStatus:
        running = self._sync_thread is not None and self._sync_thread.is_alive()
        msg_parts = []
        last = self._last_sync_ts
        if running:
            counts = self._db.row_counts() if self._db else {"fts": 0, "gmrs": 0, "op25": 0}
            msg_parts.append(f"{counts['fts']} indexed")
            if self._semantic_enabled:
                msg_parts.append(f"semantic:{self._semantic_status}")
            else:
                msg_parts.append("lexical-only")
        else:
            msg_parts.append("stopped")
        return ToolStatus(
            running=running, healthy=True,
            last_activity_ts=last,
            message=" · ".join(msg_parts),
            extra={
                "semantic_enabled": self._semantic_enabled,
                "semantic_status": self._semantic_status,
                "last_sync_added": self._last_sync_added,
                "started_ts": self._started_ts,
            },
        )

    def summary(self) -> dict:
        if not self._db:
            return {"running": False}
        counts = self._db.row_counts()
        return {
            "running": True,
            "indexed_total": counts["fts"],
            "gmrs_count": counts["gmrs"],
            "op25_count": counts["op25"],
            "embedded_count": counts.get("embeddings", 0),
            "semantic_enabled": self._semantic_enabled,
            "semantic_status": self._semantic_status,
            "last_sync_ts": self._last_sync_ts,
        }

    # --- API ------------------------------------------------------------

    def api_router(self):
        if self._router is None:
            self._router = build_router(self)
        return self._router

    def page_html(self) -> str:
        return (Path(__file__).parent / "page.html").read_text(encoding="utf-8")
