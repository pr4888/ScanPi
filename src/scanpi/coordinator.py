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
    """Per-device single-active arbitration.

    Tools that request different `sdr_device_index` values can run
    concurrently. Tools sharing a device are mutually exclusive — activating
    a new tool on a device stops whatever was running on that device.
    """

    def __init__(self, registry: ToolRegistry, state_file: Path | None = None):
        self.registry = registry
        self.state_file = state_file
        self._lock = threading.Lock()
        # Map device_index -> tool_id currently holding it.
        self._active_by_device: dict[int, str] = {}

    # --- lifecycle -------------------------------------------------------

    def start_non_sdr_tools(self) -> None:
        """Start every non-SDR tool; they run continuously."""
        for t in self.registry.non_sdr_tools():
            try:
                t.start()
            except Exception:
                log.exception("failed to start non-SDR tool %s", t.id)

    def resume_last(self) -> list[str]:
        """Activate the previously-active SDR tools from disk. Multi-SDR safe."""
        last = self._load_state() or []
        resumed = []
        for tid in last:
            if self.registry.get(tid):
                log.info("resuming SDR tool: %s", tid)
                try:
                    self.activate(tid)
                    resumed.append(tid)
                except Exception:
                    log.exception("resume failed for %s", tid)
        return resumed

    def shutdown(self) -> None:
        with self._lock:
            for dev, tid in list(self._active_by_device.items()):
                t = self.registry.get(tid)
                if t:
                    try:
                        t.stop()
                    except Exception:
                        log.exception("stop failed on %s", tid)
            self._active_by_device.clear()
        for t in self.registry.non_sdr_tools():
            try:
                t.stop()
            except Exception:
                log.exception("stop failed on %s", t.id)

    # --- SDR switching ---------------------------------------------------

    @property
    def active(self) -> str | None:
        """Backwards-compat: returns the first active tool id, or None."""
        if not self._active_by_device:
            return None
        # Prefer device 0 for the legacy single-SDR expectation.
        return self._active_by_device.get(0) or next(iter(self._active_by_device.values()))

    @property
    def active_by_device(self) -> dict[int, str]:
        return dict(self._active_by_device)

    def active_tools(self) -> list[str]:
        return list(self._active_by_device.values())

    def activate(self, tool_id: str) -> None:
        tool = self.registry.get(tool_id)
        if not tool:
            raise ValueError(f"unknown tool: {tool_id}")
        if not tool.needs_sdr:
            raise ValueError(f"tool {tool_id} does not need the SDR — start it as a non-SDR tool")
        device = tool.sdr_device_index
        with self._lock:
            # Already running on the same device?
            cur = self._active_by_device.get(device)
            if cur == tool_id:
                return
            # Something else is on this device — stop it first.
            if cur:
                prev = self.registry.get(cur)
                if prev:
                    log.info("deactivating %s (device %d)", cur, device)
                    try:
                        prev.stop()
                    except Exception:
                        log.exception("stop failed on %s", cur)
            log.info("activating %s (device %d)", tool_id, device)
            tool.start()
            self._active_by_device[device] = tool_id
            self._save_state()

    def deactivate(self, tool_id: str | None = None) -> None:
        """Deactivate a specific tool, or all SDR tools if none given."""
        with self._lock:
            if tool_id is None:
                for dev, tid in list(self._active_by_device.items()):
                    tool = self.registry.get(tid)
                    if tool:
                        try:
                            tool.stop()
                        except Exception:
                            log.exception("stop failed on %s", tid)
                self._active_by_device.clear()
            else:
                for dev, tid in list(self._active_by_device.items()):
                    if tid == tool_id:
                        tool = self.registry.get(tid)
                        if tool:
                            try:
                                tool.stop()
                            except Exception:
                                log.exception("stop failed on %s", tid)
                        del self._active_by_device[dev]
                        break
            self._save_state()
            self._save_state()

    # --- persistence -----------------------------------------------------

    def _load_state(self) -> list[str]:
        """Return list of tool ids to resume. Handles both legacy single
        'active_tool' and new 'active_tools' [list] formats.
        """
        if not self.state_file or not self.state_file.exists():
            return []
        try:
            data = json.loads(self.state_file.read_text())
            if isinstance(data.get("active_tools"), list):
                return list(data["active_tools"])
            t = data.get("active_tool")
            return [t] if t else []
        except Exception:
            return []

    def _save_state(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({
                "active_tool": next(iter(self._active_by_device.values()), None),  # legacy compat
                "active_tools": list(self._active_by_device.values()),
            }))
        except Exception:
            log.exception("failed to persist coordinator state")
