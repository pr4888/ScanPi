"""Match a transcript against the watchlist."""
from __future__ import annotations

from dataclasses import dataclass

from .watchlist import Rule, SEVERITY_ORDER, Watchlist


@dataclass
class Hit:
    rule: Rule
    matched_text: str
    span: tuple[int, int]

    def to_dict(self) -> dict:
        return {
            "rule": self.rule.name,
            "severity": self.rule.severity,
            "categories": list(self.rule.categories),
            "matched_text": self.matched_text,
            "span": list(self.span),
            "mqtt_topic_suffix": self.rule.mqtt_topic_suffix,
        }


def match_transcript(text: str | None, watchlist: Watchlist) -> list[Hit]:
    """Return all watchlist hits in `text`.

    Returns empty list for blank text. Each enabled rule is checked once;
    only the FIRST match per rule is returned (we don't care about repeats
    for alerting purposes — one hit per rule is enough).
    """
    if not text:
        return []
    hits: list[Hit] = []
    for rule, rx in watchlist.enabled():
        m = rx.search(text)
        if m:
            hits.append(Hit(rule=rule, matched_text=m.group(0), span=m.span()))
    return hits


def aggregate_severity(hits: list[Hit]) -> str:
    """Return the highest severity among hits. Defaults to 'low'."""
    if not hits:
        return "low"
    best = max(hits, key=lambda h: SEVERITY_ORDER.get(h.rule.severity, 0))
    return best.rule.severity
