"""ScanPi tool framework — plugin pattern for specialty modules.

A Tool is a self-contained module (backend service + API + UI page + dashboard
widget). Tools that need exclusive SDR access declare `needs_sdr = True` and
are arbitrated by the SDR coordinator — only one SDR-holding tool runs at a
time.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ToolStatus:
    """Runtime state snapshot for a tool. Rendered on the dashboard."""
    running: bool = False
    healthy: bool = True
    last_activity_ts: float | None = None
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Tool(ABC):
    """Base class for every specialty module.

    Lifecycle: __init__(config) → start() → ... → stop().
    UI contract: api_router() returns routes mounted at /tools/<id>/api/*,
    page_html() returns the HTML served at /tools/<id>/.
    """

    #: Short ID used in URLs + coordinator (e.g., "gmrs", "scanner").
    id: str = ""
    #: Human-friendly name shown in nav (e.g., "GMRS Monitor").
    name: str = ""
    #: One-line description for the dashboard.
    description: str = ""
    #: True if this tool holds the SDR exclusively while running.
    needs_sdr: bool = False

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @property
    def sdr_device_index(self) -> int:
        """Which RTL-SDR device this tool wants. Tools with different indexes
        can run concurrently; tools sharing an index are arbitrated by the
        coordinator. Defaults to 0 (the sole SDR on most systems).
        """
        return int(self.config.get("sdr_device", 0))

    @abstractmethod
    def start(self) -> None:
        """Spin up the backend (flowgraph, threads, workers)."""

    @abstractmethod
    def stop(self) -> None:
        """Shut down cleanly."""

    @abstractmethod
    def status(self) -> ToolStatus:
        """Current runtime state for dashboard + health checks."""

    def api_router(self):
        """Optional FastAPI APIRouter; return None if the tool has no HTTP routes."""
        return None

    def page_html(self) -> str | None:
        """Optional single-page UI HTML; return None if no page."""
        return None

    def summary(self) -> dict:
        """Optional dashboard widget payload. Shown on the home dashboard."""
        return {}


# ---------------------------------------------------------------- registry


class ToolRegistry:
    """Holds the set of available tools and exposes lookups."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.id:
            raise ValueError("tool.id is required")
        if tool.id in self._tools:
            raise ValueError(f"duplicate tool id: {tool.id}")
        self._tools[tool.id] = tool
        log.info("registered tool: %s (%s)", tool.id, tool.name)

    def get(self, tool_id: str) -> Tool | None:
        return self._tools.get(tool_id)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def sdr_tools(self) -> list[Tool]:
        return [t for t in self._tools.values() if t.needs_sdr]

    def non_sdr_tools(self) -> list[Tool]:
        return [t for t in self._tools.values() if not t.needs_sdr]
