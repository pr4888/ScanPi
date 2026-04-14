"""Transcript keyword alerting — flag calls mentioning interesting events.

Shared by all tools. Returns a tuple (alert_kind, matched_keyword) or
(None, None). Non-blocking (pure string matching), runs on the transcription
worker result callback.
"""
from __future__ import annotations

import re


# Keyword categories. Each entry: (alert_kind, [keywords]).
# Order matters: first match wins, so put the most specific / urgent first.
KEYWORD_RULES: list[tuple[str, list[str]]] = [
    # Fire-side
    ("fire",     ["working fire", "structure fire", "smoke", "fully involved"]),
    # Violent / weapons
    ("violence", ["shots fired", "gunshot", "gun", "armed", "weapon",
                  "stabbing", "assault", "robbery"]),
    # Pursuit / fleeing
    ("pursuit",  ["pursuit", "chase", "fleeing", "eluding"]),
    # Medical urgent
    ("medical",  ["cardiac", "not breathing", "unconscious", "unresponsive",
                  "cpr", "overdose", "stroke"]),
    # Emergency codes / general
    ("emergency",["mayday", "code 3", "code three", "officer down",
                  "10-33", "10-99", "10-50", "signal 13", "all units"]),
    # Accident / crash
    ("accident", ["mvc", "mva", "crash", "accident", "rollover", "collision"]),
]

# Pre-compile into one big OR pattern per kind for fast scanning.
_COMPILED: list[tuple[str, re.Pattern]] = []
for kind, kws in KEYWORD_RULES:
    pattern = "|".join(r"\b" + re.escape(kw) + r"\b" for kw in kws)
    _COMPILED.append((kind, re.compile(pattern, re.IGNORECASE)))


def scan(text: str | None) -> tuple[str | None, str | None]:
    """Return (alert_kind, matched_keyword) or (None, None)."""
    if not text:
        return None, None
    for kind, rx in _COMPILED:
        m = rx.search(text)
        if m:
            return kind, m.group(0)
    return None, None
