"""Geo-reference extractor for radio transcripts.

Pulls candidate place mentions out of a free-form English transcript:
  - towns (gazetteer match — strict word boundary)
  - route numbers ("Route 27", "I-95", "I 95", "Interstate 95")
  - intersections ("at the intersection of X and Y", "X and Y")
  - street addresses ("123 Main Street")
  - bare street names (gazetteer match against a streets table)

Returns a list of `Candidate` dicts:
  {kind, raw_text, span: (start, end), name, town?, route_num?,
   street?, cross_street?, number?}

The geocoder consumes these and produces lat/lon pins.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


# ----- constants ------------------------------------------------------

# Suffix part is case-insensitive; the rest of the regex is intentionally
# CASE-SENSITIVE so we only pick up Title-Cased proper nouns (Whisper +
# transcribers emit street names in Title Case). Mixing word boundaries
# with title-case discipline keeps "and the intersection" out of the
# bare-street match.
STREET_SUFFIX_PATTERN = r"(?:[Ss]treet|[Ss]t|[Rr]oad|[Rr]d|[Aa]venue|[Aa]ve|" \
    r"[Dd]rive|[Dd]r|[Bb]oulevard|[Bb]lvd|[Ll]ane|[Ll]n|[Ww]ay|" \
    r"[Cc]ourt|[Cc]t|[Pp]lace|[Pp]l|[Hh]ighway|[Hh]wy|" \
    r"[Pp]arkway|[Pp]kwy|[Tt]errace|[Tt]er|[Cc]ircle|[Cc]ir|" \
    r"[Tt]rail|[Tt]r|[Aa]lley|[Aa]l|[Rr]ow|[Ss]quare|[Ss]q)\.?"

# A "title-cased word": starts with capital, optional rest. Intentionally
# does NOT have re.IGNORECASE applied so "and"/"the"/"of" don't qualify.
_TWORD = r"[A-Z][a-zA-Z'\-]+"

# A street fragment is 1-3 title-cased words followed by a recognized suffix.
_STREET_FRAGMENT = r"(?:" + _TWORD + r"\s+){0,2}" + _TWORD + r"\s+" + STREET_SUFFIX_PATTERN

_RE_NUMBERED_ADDRESS = re.compile(
    r"\b(?P<num>\d{1,5})\s+(?P<street>" + _STREET_FRAGMENT + r")\b",
)

_RE_ROUTE = re.compile(
    r"\b(?:"
    r"(?:U\.?S\.?\s+)?Route\s+(?P<num1>\d{1,3}[A-Z]?)|"
    r"(?:Highway|Hwy)\s+(?P<num2>\d{1,3}[A-Z]?)|"
    r"(?:Interstate)\s*-?\s*(?P<num3>\d{1,3})|"
    r"I-(?P<num5>\d{1,3})|"
    r"R[Tt]\.?\s*(?P<num4>\d{1,3}[A-Z]?)"
    r")\b",
    re.IGNORECASE,
)

# "intersection of X and Y" / "corner of X and Y"
_RE_INTERSECTION_FORMAL = re.compile(
    r"\b(?:[Ii]ntersection|[Cc]orner)\s+of\s+"
    r"(?P<a>" + _STREET_FRAGMENT + r"|" + _TWORD + r"(?:\s+" + _TWORD + r"){0,2})"
    r"\s+and\s+"
    r"(?P<b>" + _STREET_FRAGMENT + r"|" + _TWORD + r"(?:\s+" + _TWORD + r"){0,2})\b"
)

# "<street> at <street>" — both sides must end with a known suffix.
_RE_INTERSECTION_AT = re.compile(
    r"\b(?P<a>" + _STREET_FRAGMENT + r")"
    r"\s+(?:at|@)\s+"
    r"(?P<b>" + _STREET_FRAGMENT + r")\b"
)

# Bare street name — same fragment, but word boundaries to avoid matching
# trailing "and Bridge Street" (the intersection regex covers those).
_RE_BARE_STREET = re.compile(r"\b(?P<street>" + _STREET_FRAGMENT + r")\b")


@dataclass
class Candidate:
    kind: str           # street | town | route | intersection | landmark
    raw_text: str       # original text from transcript
    span: tuple[int, int]
    name: str = ""
    town: str | None = None
    route_num: str | None = None
    street: str | None = None
    cross_street: str | None = None
    number: str | None = None
    confidence_hint: float = 0.5     # extractor's prior; geocoder may downgrade

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "raw_text": self.raw_text, "span": list(self.span),
            "name": self.name, "town": self.town, "route_num": self.route_num,
            "street": self.street, "cross_street": self.cross_street,
            "number": self.number, "confidence_hint": self.confidence_hint,
        }


# ----- helpers --------------------------------------------------------


def _route_label(num: str) -> str:
    if num.upper().startswith("I"):
        return num
    return f"Route {num}"


def _normalize_street(s: str) -> str:
    """Title-case and tidy a street fragment."""
    return " ".join(w.capitalize() if not w.isupper() else w for w in s.split())


_SUFFIX_SET = {
    "street", "st", "road", "rd", "avenue", "ave", "drive", "dr",
    "boulevard", "blvd", "lane", "ln", "way", "court", "ct", "place",
    "pl", "highway", "hwy", "parkway", "pkwy", "terrace", "ter",
    "circle", "cir", "trail", "tr", "alley", "al", "row", "square", "sq",
}


def _looks_like_street(text: str) -> bool:
    """Cheap check: does this end with a known street suffix?"""
    last = text.strip().split()[-1].rstrip(".").lower() if text.strip() else ""
    return last in _SUFFIX_SET


# ----- main extractor -------------------------------------------------


def extract(transcript: str, town_names: Iterable[str] = ()) -> list[Candidate]:
    """Pull all geo candidates from a transcript.

    `town_names` is the lowercase set of town names (from gazetteer) used
    for whole-word town matching. Pass `db.all_towns()` results in.
    """
    if not transcript:
        return []
    out: list[Candidate] = []
    seen_spans: list[tuple[int, int]] = []

    def _overlaps(span: tuple[int, int]) -> bool:
        for s, e in seen_spans:
            if not (span[1] <= s or span[0] >= e):
                return True
        return False

    def _add(c: Candidate):
        if _overlaps(c.span):
            return
        seen_spans.append(c.span)
        out.append(c)

    text = transcript

    # 1. Numbered addresses ("123 Main Street")
    for m in _RE_NUMBERED_ADDRESS.finditer(text):
        street = _normalize_street(m.group("street"))
        _add(Candidate(
            kind="street", raw_text=m.group(0), span=m.span(),
            name=street, number=m.group("num"), street=street,
            confidence_hint=0.7,
        ))

    # 2. Route numbers ("Route 27", "I-95", "Highway 1", "Interstate 95")
    for m in _RE_ROUTE.finditer(text):
        num = (m.group("num1") or m.group("num2")
               or m.group("num3") or m.group("num4")
               or m.group("num5"))
        if not num:
            continue
        # Distinguish interstate from state route.
        raw_lower = m.group(0).lower()
        if raw_lower.startswith(("interstate", "i-")):
            label = f"I-{num}"
        else:
            label = f"Route {num}"
        _add(Candidate(
            kind="route", raw_text=m.group(0), span=m.span(),
            name=label, route_num=num, confidence_hint=0.6,
        ))

    # 3. Formal intersections ("intersection of X and Y")
    for m in _RE_INTERSECTION_FORMAL.finditer(text):
        a = _normalize_street(m.group("a"))
        b = _normalize_street(m.group("b"))
        _add(Candidate(
            kind="intersection", raw_text=m.group(0), span=m.span(),
            name=f"{a} & {b}", street=a, cross_street=b,
            confidence_hint=0.65,
        ))

    # 4. "<street> at <street>" intersections — only if both sides have suffixes
    for m in _RE_INTERSECTION_AT.finditer(text):
        a, b = m.group("a"), m.group("b")
        if not (_looks_like_street(a) and _looks_like_street(b)):
            continue
        a, b = _normalize_street(a), _normalize_street(b)
        _add(Candidate(
            kind="intersection", raw_text=m.group(0), span=m.span(),
            name=f"{a} & {b}", street=a, cross_street=b,
            confidence_hint=0.6,
        ))

    # 5. Bare street names (must end with suffix; gazetteer will verify)
    for m in _RE_BARE_STREET.finditer(text):
        street = _normalize_street(m.group("street"))
        _add(Candidate(
            kind="street", raw_text=m.group(0), span=m.span(),
            name=street, street=street, confidence_hint=0.4,
        ))

    # 6. Town names (gazetteer-driven). Match whole words, case-insensitive.
    if town_names:
        # Sort by length desc so "North Stonington" beats "Stonington".
        sorted_towns = sorted({t.lower() for t in town_names}, key=len, reverse=True)
        for town in sorted_towns:
            pattern = r"\b" + re.escape(town) + r"\b"
            for m in re.finditer(pattern, text, re.IGNORECASE):
                _add(Candidate(
                    kind="town", raw_text=m.group(0), span=m.span(),
                    name=town.title(), town=town.title(),
                    confidence_hint=0.8,
                ))

    # Stable order by span start.
    out.sort(key=lambda c: c.span[0])
    return out


def attach_town_context(candidates: list[Candidate]) -> list[Candidate]:
    """Pass-1 enrichment: if a town candidate appears near a street/route
    candidate (within ~80 chars), record the town on the street/route.

    This helps disambiguate "Main Street" — there are dozens of those.
    """
    towns = [c for c in candidates if c.kind == "town"]
    if not towns:
        return candidates
    for c in candidates:
        if c.kind in ("town",):
            continue
        for tc in towns:
            distance = min(
                abs(c.span[0] - tc.span[1]),
                abs(tc.span[0] - c.span[1]),
            )
            if distance <= 80:
                c.town = tc.name
                # Slight confidence bump from town context.
                c.confidence_hint = min(0.95, c.confidence_hint + 0.1)
                break
    return candidates
