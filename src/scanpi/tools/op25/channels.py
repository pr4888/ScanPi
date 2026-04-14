"""OP25 tool — helpers for reading the talkgroup TSV."""
from __future__ import annotations

from pathlib import Path


def load_talkgroups(tsv_path: Path) -> dict[int, dict]:
    """Parse an OP25 talkgroup TSV into {tgid: {"name": str, "category": str, ...}}.

    Format: first column is tgid, second is tag/name, optional extra columns
    for category / priority / hint. Commented (#) and blank lines are skipped.
    """
    tgs: dict[int, dict] = {}
    if not tsv_path.exists():
        return tgs
    for line in tsv_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            tgid = int(parts[0].strip())
        except ValueError:
            continue
        name = parts[1].strip() or f"TG-{tgid}"
        cat = parts[2].strip().lower() if len(parts) > 2 else ""
        prio = int(parts[3]) if len(parts) > 3 and parts[3].strip().isdigit() else 0
        tgs[tgid] = {"tgid": tgid, "name": name, "category": cat, "priority": prio}
    return tgs


# Rough tag-based category classifier — used when TSV column doesn't specify.
KEYWORD_CATEGORY = [
    ("police", ["pd", "police", "troop", "csp", "sheriff", "state police"]),
    ("fire",   ["fire", "fd", "engine", "ladder", "rescue"]),
    ("ems",    ["ems", "medic", "ambulance", "hospital"]),
    ("transit",["transit", "bus", "ct transit"]),
    ("utility",["dpw", "utility", "works", "sanitation"]),
    ("school", ["school", "college", "univ"]),
]


def classify(name: str) -> str:
    """Return a category for a TG name when not explicitly set in the TSV."""
    n = name.lower()
    for cat, keywords in KEYWORD_CATEGORY:
        for kw in keywords:
            if kw in n:
                return cat
    return "other"
