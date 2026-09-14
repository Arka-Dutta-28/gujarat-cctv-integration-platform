"""Resolve a place name to a position, for cameras that arrive without one.

The upstream catalogue gives every camera a location name and no coordinates.
Something has to turn "Majevadi Gate PTZ-2, Junagadh" into a point on the map,
because the whole platform is geospatial: corridor search, coverage polygons,
gap analysis and journey plausibility all need a position.

The previous version of this did it with a table keyed by camera number. That is
the wrong key, and the integration reference says why in one line: camera ids
and the set of available cameras can change. A table keyed by id does not fail
loudly when the organisers renumber the estate. It silently places every camera
at somebody else's junction, and the map still looks plausible. Keying by name
survives renumbering, survives cameras being added, and degrades to the right
thing, a district centroid, flagged, rather than to a confident lie.

Three tiers, tried in order, and the tier used is reported on the result:

    landmark   a specific junction, gate or bridge named in the feed
    city       a town or city named anywhere in the location string
    district   the district centroid, when only the district is recognisable

Anything unresolved is unplaced: the state centroid, with `precision` saying so,
so an operator can filter for the pins that still need a survey position instead
of trusting them. Never invented, never silently dropped.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("gazetteer")

__all__ = ["Place", "resolve", "load_gazetteer", "GAZETTEER_PATH"]

#: Overridable so a deployment can ship its own asset register without a code
#: change — which is the point of keeping this out of Python.
GAZETTEER_PATH = Path(
    os.environ.get("GAZETTEER_PATH", "data/gazetteer/gujarat.json")
)

_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Place:
    """A resolved position, and how much to trust it."""

    lat: float
    lon: float
    district: str
    #: `landmark`, `city`, `district`, or `unplaced`.
    precision: str
    #: The gazetteer key that matched, for the audit trail.
    matched: str | None = None

    @property
    def placed(self) -> bool:
        return self.precision != "unplaced"


@lru_cache(maxsize=4)
def load_gazetteer(path: str | None = None) -> dict:
    target = Path(path) if path else GAZETTEER_PATH
    try:
        with open(target, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("gazetteer %s unreadable (%s); every camera will be unplaced", target, exc)
        return {"landmarks": {}, "cities": {}, "districts": {}}


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _phrase_hit(key: str, tokens: list[str], haystack: str) -> bool:
    """Does `key` occur in the location text as whole words?

    Substring matching would put every camera containing "una" — Junagadh,
    Punagam, Kununa — in Una, Gir Somnath. Multi-word keys are matched as an
    ordered run of tokens; single-word keys as an exact token.
    """
    parts = _tokens(key)
    if not parts:
        return False
    if len(parts) == 1:
        return parts[0] in tokens
    return f" {' '.join(parts)} " in f" {haystack} "


def resolve(location: str, hint: str | None = None, path: str | None = None) -> Place:
    """Best position for this location name. Always returns something.

    `hint` is any extra text worth searching — a device label, a department
    name — appended to the location for matching only.
    """
    data = load_gazetteer(path)
    text = " ".join(t for t in (location or "", hint or "") if t)
    tokens = _tokens(text)
    haystack = " ".join(tokens)

    for tier in ("landmarks", "cities"):
        # Longest key first: "char chowk" should win over "chowk", and
        # "gir somnath" over "somnath".
        for key in sorted(data.get(tier, {}), key=len, reverse=True):
            if _phrase_hit(key, tokens, haystack):
                entry = data[tier][key]
                return Place(
                    lat=float(entry["lat"]),
                    lon=float(entry["lon"]),
                    district=entry.get("district", "Unknown"),
                    precision="landmark" if tier == "landmarks" else "city",
                    matched=key,
                )

    for name in sorted(data.get("districts", {}), key=len, reverse=True):
        if _phrase_hit(name, tokens, haystack):
            entry = data["districts"][name]
            return Place(
                lat=float(entry["lat"]), lon=float(entry["lon"]),
                district=name, precision="district", matched=name,
            )

    fallback = data.get("fallback") or {"lat": 22.69, "lon": 71.57, "district": "Unplaced"}
    return Place(
        lat=float(fallback["lat"]), lon=float(fallback["lon"]),
        district=fallback.get("district", "Unplaced"), precision="unplaced",
    )
