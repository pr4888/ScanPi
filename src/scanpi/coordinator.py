"""SDR coordinator — single-active arbitration for tools that need the SDR.

Only one tool with `needs_sdr = True` can run at a time. Tools that don't need
the SDR run independently. The coordinator persists the last-active tool id so
it resumes on restart.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from .tools import Tool, ToolRegistry

log = logging.getLogger(__name__)


class SdrCoordinator:
    def __init__(self, registry: ToolRegistry, state_file: Path | None = None):
        self.registry = registry
        self.state_file = state_file
        self._lock = threading.Lock()
        self._active_id: str | None = None

    # --- lifecycle -------------------------------------------------------

    def start_non_sdr_tools(self) -> None:
        """Start every non-SDR tool; they run continuously."""
        for t in self.registry.non_sdr_tools():
            try:
                t.start()
            except Exception:
                log.exception("failed to start non-SDR tool %s", t.id)

    def resume_last(self) -> str | None:
        """Activate the last-selected SDR tool from disk, if any."""
        last = self._load_state()
        if last and self.registry.get(last):
            log.info("resuming SDR tool: %s", last)
            self.activate(last)
            return last
        return None

    def shutdown(self) -> None:
        with self._lock:
            if self._active_id:
                t = self.registry.get(self._active_id)
                if t:
                    try:
                        t.stop()
                    except Exception:
                        log.exception("stop failed on %s", self._active_id)
                self._active_id = None
        for t in self.registry.non_sdr_tools():
            try:
                t.stop()
            except Exception:
                log.exception("stop failed on %s", t.id)

    # --- SDR switching ---------------------------------------------------

    @property
    def active(self) -> str | None:
        return self._active_id

    def activate(self, tool_id: str) -> None:
        tool = self.registry.get(tool_id)
        if not tool:
            raise ValueError(f"unknown tool: {tool_id}")
        if not tool.needs_sdr:
            raise ValueError(f"tool {tool_id} does not need the SDR — start it as a non-SDR tool")
        with self._lock:
            if self._active_id == tool_id:
                return
            if self._active_id:
                prev = self.registry.get(self._active_id)
                if prev:
                    log.info("deactivating %s", self._active_id)
                    try:
                        prev.stop()
                    except Exception:
                        log.exception("stop failed on %s", self._active_id)
            log.info("activating %s", tool_id)
            tool.start()
            self._active_id = tool_id
            self._save_state()

    def deactivate(self) -> None:
        with self._lock:
            if not self._active_id:
                return
            tool = self.registry.get(self._active_id)
            if tool:
                try:
                    tool.stop()
                except Exception:
                    log.exception("stop failed on %s", self._active_id)
            self._active_id = None
            self._save_state()

    # --- persistence -----------------------------------------------------

    def _load_state(self) -> str | None:
        if not self.state_file or not self.state_file.exists():
            return None
        try:
            data = json.loads(self.state_file.read_text())
            return data.get("active_tool")
        except Exception:
            return None

    def _save_state(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"active_tool": self._active_id}))
        except Exception:
            log.exception("failed to persist coordinator state")
