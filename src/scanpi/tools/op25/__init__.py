"""P25 Trunking tool — wraps OP25 multi_rx.py + audio capture as a ScanPi Tool."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from ...tools import Tool, ToolStatus
from ..gmrs import _serve_file_with_range  # reuse range-aware audio serving
from ..gmrs.transcriber import TranscribeJob, TranscriptionWorker
from .bridge import ActiveCall, OP25Bridge, BridgeConfig
from .channels import classify, load_talkgroups
from .db import OP25DB

log = logging.getLogger(__name__)


class OP25Tool(Tool):
    id = "op25"
    name = "P25 Trunking"
    description = "Listen to trunked P25 systems (police/fire/EMS) via OP25 + whisper transcription"
    needs_sdr = True

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        cfg = self.config
        data_dir = Path(cfg.get("data_dir", Path.home() / "scanpi"))
        self._db_path = data_dir / "op25.db"
        self._audio_dir = data_dir / "op25_audio"
        self._log_path = data_dir / "logs" / "op25.log"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

        self._op25_dir = Path(cfg.get(
            "op25_dir",
            Path.home() / "op25" / "op25" / "gr-op25_repeater" / "apps",
        ))
        self._config_json = str(cfg.get("op25_config", "clmrn_cfg.json"))
        self._tg_tsv_rel = str(cfg.get("talkgroups_tsv", "clmrn_talkgroups.tsv"))
        # OP25 sends decoded voice to the channel's `destination` UDP port.
        # clmrn_cfg.json uses 2345; override via config if your setup differs.
        self._udp_port = int(cfg.get("udp_port", 2345))

        self._bridge_cfg = BridgeConfig(
            op25_dir=self._op25_dir,
            config_json=self._config_json,
            log_path=self._log_path,
            audio_dir=self._audio_dir,
            udp_port=self._udp_port,
        )
        self._db: OP25DB | None = None
        self._bridge: OP25Bridge | None = None
        self._transcriber: TranscriptionWorker | None = None
        self._talkgroups: dict[int, dict] = {}

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if not self._op25_dir.exists():
            raise RuntimeError(
                f"OP25 not found at {self._op25_dir} — install per "
                "https://github.com/boatbod/op25 before activating this tool"
            )
        self._db = OP25DB(self._db_path)
        self._db.connect()
        orphaned = self._db.orphan_cleanup()
        if orphaned:
            log.info("cleaned %d orphan p25_calls from prior run", orphaned)

        # Load talkgroups (best-effort)
        tg_path = self._op25_dir / self._tg_tsv_rel
        self._talkgroups = load_talkgroups(tg_path)
        log.info("loaded %d talkgroups from %s", len(self._talkgroups), tg_path)

        self._bridge = OP25Bridge(
            self._bridge_cfg,
            on_call_open=self._open_call,
            on_call_close=self._close_call,
        )
        self._bridge.start()

        # Transcription — reuse GMRS worker pattern
        if bool(self.config.get("transcribe", True)):
            model_dir = Path(self.config.get(
                "whisper_model_dir",
                self._audio_dir.parent / "models",
            ))
            model_dir.mkdir(parents=True, exist_ok=True)
            self._transcriber = TranscriptionWorker(
                on_result=self._on_transcribe_result,
                model_name=str(self.config.get("whisper_model", "tiny.en")),
                model_dir=model_dir,
            )
            self._transcriber.start()

    def stop(self) -> None:
        if self._transcriber is not None:
            try: self._transcriber.stop()
            except Exception: log.exception("transcriber stop failed")
            self._transcriber = None
        if self._bridge is not None:
            try: self._bridge.stop()
            except Exception: log.exception("bridge stop failed")
            self._bridge = None
        if self._db is not None:
            self._db.close()
            self._db = None

    # --- callbacks from bridge -----------------------------------------

    def _open_call(self, ac: ActiveCall) -> int:
        tg = self._talkgroups.get(ac.tgid)
        name = tg["name"] if tg else f"TG-{ac.tgid}"
        category = tg["category"] if (tg and tg.get("category")) else classify(name)
        return self._db.open_call(ac.tgid, name, category, ac.rid, ac.freq_mhz, ac.start_ts)

    def _close_call(self, call_id: int, end_ts: float, clip_path: str | None):
        if self._db is None:
            return
        self._db.close_call(call_id, end_ts, clip_path=clip_path)
        if clip_path and self._transcriber is not None:
            self._db.set_transcript(call_id, None, "pending")
            # Pull tgid for context in log
            self._transcriber.submit(TranscribeJob(
                event_id=call_id,
                clip_path=clip_path,
                channel=0,  # not used for P25
            ))

    def _on_transcribe_result(self, call_id: int, text: str | None, status: str):
        if self._db is not None:
            try:
                self._db.set_transcript(call_id, text, status)
            except Exception:
                log.exception("failed to write transcript for call %d", call_id)

    # --- status / summary -----------------------------------------------

    def status(self) -> ToolStatus:
        running = self._bridge is not None
        last = self._db.last_call_end_ts() if self._db else None
        if running and self._bridge:
            snap = self._bridge.snapshot()
            msg = f"active calls: {len(snap['active_calls'])}"
            if snap.get("cc_freq_mhz"):
                msg += f" · cc {snap['cc_freq_mhz']:.5f} MHz"
        else:
            msg = "stopped"
        return ToolStatus(
            running=running, healthy=True,
            last_activity_ts=last,
            message=msg,
            extra={"op25_dir": str(self._op25_dir), "config": self._config_json,
                   "talkgroups_loaded": len(self._talkgroups)},
        )

    def summary(self) -> dict:
        if not self._db:
            return {"running": False}
        since = time.time() - 24 * 3600
        stats = self._db.talkgroup_stats(since_ts=since)
        top = stats[0] if stats else None
        return {
            "running": True,
            "total_calls_24h": sum(s["call_count"] for s in stats),
            "active_tgs_24h": len(stats),
            "top_tg": top["tgid"] if top else None,
            "top_tg_name": top["tg_name"] if top else None,
            "top_count": top["call_count"] if top else 0,
            "last_activity_ts": self._db.last_call_end_ts(),
        }

    # --- API ------------------------------------------------------------

    def api_router(self):
        r = APIRouter()

        @r.get("/live")
        def live():
            if self._bridge is None:
                return {"running": False, "active_calls": []}
            return self._bridge.snapshot()

        @r.get("/stats")
        def stats(hours: float = 24.0):
            if not self._db:
                return {"hours": hours, "talkgroups": []}
            since = time.time() - hours * 3600 if hours > 0 else 0.0
            return {"hours": hours, "talkgroups": self._db.talkgroup_stats(since_ts=since)}

        @r.get("/recent")
        def recent(limit: int = 50):
            if not self._db:
                return {"calls": []}
            return {"calls": self._db.recent(limit)}

        @r.get("/talkgroups")
        def talkgroups():
            return {"count": len(self._talkgroups),
                    "talkgroups": list(self._talkgroups.values())}

        @r.get("/clip/{call_id}")
        def clip(call_id: int, request: Request):
            if not self._db:
                raise HTTPException(404, "db offline")
            row = self._db.conn.execute(
                "SELECT clip_path FROM p25_calls WHERE id = ?", (call_id,),
            ).fetchone()
            if not row or not row["clip_path"] or not os.path.exists(row["clip_path"]):
                raise HTTPException(404, "clip not found")
            return _serve_file_with_range(row["clip_path"], "audio/wav", request)

        return r

    def page_html(self) -> str:
        return (Path(__file__).parent / "page.html").read_text(encoding="utf-8")
