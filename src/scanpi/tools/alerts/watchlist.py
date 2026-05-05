"""Watchlist YAML loader/saver.

File: ~/scanpi/watchlist.yaml — human-editable. Each rule is a dict with:
    name (str, unique), pattern (str, regex or plain phrase),
    severity (low|medium|high|critical), categories (list[str]),
    enabled (bool), mqtt_topic_suffix (str, optional).

If `pattern` is a plain phrase (no regex metachars), it is wrapped in
\\bword\\b boundaries automatically before matching.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

VALID_SEVERITIES = ("low", "medium", "high", "critical")
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# --------------------------------------------------------------- YAML I/O

# Prefer ruamel.yaml for round-trip preservation (keeps comments + ordering).
# Fall back to PyYAML if ruamel isn't installed (ships warning).
try:
    from ruamel.yaml import YAML  # type: ignore
    _yaml = YAML(typ="rt")
    _yaml.indent(mapping=2, sequence=4, offset=2)
    _yaml.preserve_quotes = True
    _USING_RUAMEL = True
except ImportError:  # pragma: no cover
    try:
        import yaml as _pyyaml  # type: ignore
        _USING_RUAMEL = False
    except ImportError:
        _pyyaml = None
        _USING_RUAMEL = False


def _load_yaml(path: Path) -> dict | list:
    text = path.read_text(encoding="utf-8")
    if _USING_RUAMEL:
        return _yaml.load(text) or {}
    if _pyyaml is None:
        raise RuntimeError(
            "Neither ruamel.yaml nor pyyaml is installed — install one to use Alerts."
        )
    return _pyyaml.safe_load(text) or {}


def _dump_yaml(data, path: Path):
    if _USING_RUAMEL:
        with path.open("w", encoding="utf-8") as f:
            _yaml.dump(data, f)
        return
    if _pyyaml is None:
        raise RuntimeError(
            "Neither ruamel.yaml nor pyyaml is installed — cannot save watchlist."
        )
    text = _pyyaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    path.write_text(text, encoding="utf-8")


# ------------------------------------------------------------- Rule model


@dataclass
class Rule:
    name: str
    pattern: str
    severity: str = "medium"
    categories: list[str] = field(default_factory=list)
    enabled: bool = True
    mqtt_topic_suffix: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "pattern": self.pattern,
            "severity": self.severity,
            "categories": list(self.categories),
            "enabled": bool(self.enabled),
            "mqtt_topic_suffix": self.mqtt_topic_suffix,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Rule":
        return cls(
            name=str(raw["name"]).strip(),
            pattern=str(raw["pattern"]),
            severity=_validate_severity(raw.get("severity", "medium")),
            categories=list(raw.get("categories") or []),
            enabled=bool(raw.get("enabled", True)),
            mqtt_topic_suffix=str(raw.get("mqtt_topic_suffix", "") or ""),
        )


def _validate_severity(s: str) -> str:
    s = (s or "").strip().lower()
    if s not in VALID_SEVERITIES:
        raise ValueError(f"severity must be one of {VALID_SEVERITIES}, got {s!r}")
    return s


_REGEX_META = re.compile(r"[.^$*+?()[\]{}|\\]")


def _looks_like_regex(pattern: str) -> bool:
    return bool(_REGEX_META.search(pattern))


def compile_pattern(pattern: str) -> re.Pattern:
    """Compile the rule pattern. Plain phrases get \\b boundaries."""
    if _looks_like_regex(pattern):
        return re.compile(pattern, re.IGNORECASE)
    return re.compile(r"\b" + re.escape(pattern) + r"\b", re.IGNORECASE)


def validate_rule_dict(raw: dict) -> Rule:
    """Validate user-submitted rule data — raises ValueError on bad input."""
    if not isinstance(raw, dict):
        raise ValueError("rule must be a dict")
    if not raw.get("name") or not str(raw["name"]).strip():
        raise ValueError("rule.name is required")
    if not raw.get("pattern") or not str(raw["pattern"]):
        raise ValueError("rule.pattern is required")
    rule = Rule.from_dict(raw)
    # Compile to confirm regex is valid before we save it.
    try:
        compile_pattern(rule.pattern)
    except re.error as e:
        raise ValueError(f"invalid regex: {e}") from e
    return rule


# --------------------------------------------------------------- Defaults


DEFAULT_WATCHLIST: list[dict] = [
    # --- emergency / officer-down ---
    {"name": "officer_down", "pattern": "officer down",
     "severity": "critical", "categories": ["police", "emergency"], "enabled": True},
    {"name": "shots_fired", "pattern": "shots fired",
     "severity": "critical", "categories": ["police", "violence"], "enabled": True},
    {"name": "code_3", "pattern": r"\bcode\s*(?:3|three)\b",
     "severity": "high", "categories": ["dispatch"], "enabled": True},
    {"name": "ten_thirty_three", "pattern": r"\b10[-\s]?33\b",
     "severity": "critical", "categories": ["police"], "enabled": True},

    # --- maritime distress ---
    {"name": "mayday", "pattern": "mayday",
     "severity": "critical", "categories": ["maritime", "emergency"], "enabled": True},
    {"name": "pan_pan", "pattern": r"pan[-\s]?pan",
     "severity": "high", "categories": ["maritime"], "enabled": True},

    # --- fire / EMS ---
    {"name": "fire_general", "pattern": r"\b(working\s+fire|structure\s+fire|fully\s+involved)\b",
     "severity": "high", "categories": ["fire"], "enabled": True},
    {"name": "fire", "pattern": "fire",
     "severity": "low", "categories": ["fire"], "enabled": False},
    {"name": "mva", "pattern": r"\bm[vc]a\b",
     "severity": "medium", "categories": ["ems"], "enabled": True},

    # --- person of interest ---
    {"name": "child_missing", "pattern": r"\b(missing\s+child|child\s+missing|amber\s+alert)\b",
     "severity": "critical", "categories": ["amber", "missing"], "enabled": True},

    # --- family-name placeholders (rename / disable as needed) ---
    {"name": "family_susan", "pattern": "susan",
     "severity": "medium", "categories": ["family"], "enabled": False},
    {"name": "family_brianna", "pattern": "brianna",
     "severity": "medium", "categories": ["family"], "enabled": False},
    {"name": "family_james", "pattern": "james",
     "severity": "medium", "categories": ["family"], "enabled": False},
    {"name": "family_surname_placeholder", "pattern": "ryan",
     "severity": "low", "categories": ["family"], "enabled": False},

    # --- regex examples ---
    {"name": "phone_number_local",
     "pattern": r"\b\(?860\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
     "severity": "low", "categories": ["pii"], "enabled": False},
    {"name": "license_plate_ct",
     "pattern": r"\b[A-Z]{2}[-\s]?\d{4,5}\b",
     "severity": "low", "categories": ["pii"], "enabled": False},
]


# ----------------------------------------------------------- Watchlist API


class Watchlist:
    """Thread-safe in-memory watchlist backed by a YAML file.

    Reload-from-disk is supported but not automatic — callers can re-instantiate
    or call .load() to refresh. Save is atomic (write to .tmp then rename).
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._rules: dict[str, Rule] = {}
        self._compiled: dict[str, re.Pattern] = {}
        self.load()

    # -- I/O --

    def load(self):
        with self._lock:
            if not self.path.exists():
                # Seed file with defaults.
                self._seed_default_file()
            try:
                raw = _load_yaml(self.path)
            except Exception:
                log.exception("watchlist YAML load failed; starting empty")
                raw = {}
            entries: Iterable[dict]
            if isinstance(raw, dict):
                entries = raw.get("rules") or raw.get("watchlist") or []
            elif isinstance(raw, list):
                entries = raw
            else:
                entries = []
            self._rules.clear()
            self._compiled.clear()
            for r in entries:
                try:
                    rule = Rule.from_dict(dict(r))
                    self._rules[rule.name] = rule
                    self._compiled[rule.name] = compile_pattern(rule.pattern)
                except Exception:
                    log.exception("skipping bad watchlist entry: %r", r)
            log.info("watchlist loaded %d rules from %s", len(self._rules), self.path)

    def _seed_default_file(self):
        # Use ruamel for nice output if available so seeded file has comments-friendly layout.
        log.info("seeding default watchlist at %s", self.path)
        if _USING_RUAMEL:
            from ruamel.yaml.comments import CommentedMap, CommentedSeq
            doc = CommentedMap()
            seq = CommentedSeq()
            for r in DEFAULT_WATCHLIST:
                seq.append(CommentedMap(r))
            doc["rules"] = seq
            doc.yaml_set_comment_before_after_key(
                "rules",
                before=(
                    "ScanPi alerts watchlist.\n"
                    "Each rule fires an MQTT alert + DB row when matched against any\n"
                    "transcribed call. Patterns may be plain phrases (auto \\b-bounded)\n"
                    "or full regex. Severity: low | medium | high | critical.\n"
                    "Edit this file freely — the Alerts tool reloads on POST /watchlist.\n"
                ),
            )
            with self.path.open("w", encoding="utf-8") as f:
                _yaml.dump(doc, f)
        else:
            _dump_yaml({"rules": DEFAULT_WATCHLIST}, self.path)

    def save(self):
        with self._lock:
            data = {"rules": [r.to_dict() for r in self._rules.values()]}
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            _dump_yaml(data, tmp)
            os.replace(tmp, self.path)

    # -- queries --

    def all(self) -> list[Rule]:
        with self._lock:
            return list(self._rules.values())

    def enabled(self) -> list[tuple[Rule, re.Pattern]]:
        with self._lock:
            return [(r, self._compiled[r.name])
                    for r in self._rules.values() if r.enabled]

    def get(self, name: str) -> Rule | None:
        with self._lock:
            return self._rules.get(name)

    # -- mutations --

    def upsert(self, raw: dict) -> Rule:
        """Validate & store. Persists to disk on success."""
        rule = validate_rule_dict(raw)
        with self._lock:
            self._rules[rule.name] = rule
            self._compiled[rule.name] = compile_pattern(rule.pattern)
            self.save()
        return rule

    def delete(self, name: str) -> bool:
        with self._lock:
            if name not in self._rules:
                return False
            del self._rules[name]
            self._compiled.pop(name, None)
            self.save()
            return True
