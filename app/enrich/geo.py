"""Location resolution: free-text ``location_raw`` -> pin on the campus map.

Offline first (gazetteer of Yale buildings: names, aliases, room-code prefixes, street
addresses, then fuzzy matching); an explicit location cue in the description only when the
source withheld the location; Nominatim (cached, rate limited) only when configured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml
from rapidfuzz import fuzz

from app.schemas import GeocodeMethod, GeoResult

log = logging.getLogger(__name__)

FUZZY_THRESHOLD = 88.0
FUZZY_MIN_LEN = 6
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# lon_min, lat_max, lon_max, lat_min: New Haven, West Haven and West Campus (Orange).
NEW_HAVEN_VIEWBOX = "-73.05,41.36,-72.85,41.22"

_SUFFIXES = {
    "street": "st", "st": "st", "avenue": "ave", "ave": "ave", "road": "rd", "rd": "rd",
    "drive": "dr", "dr": "dr", "parkway": "pkwy", "pkwy": "pkwy", "place": "pl", "pl": "pl",
    "boulevard": "blvd", "blvd": "blvd", "lane": "ln", "ln": "ln", "broadway": "broadway",
}
_ADDRESS_RE = re.compile(
    r"(?<![a-z0-9])(\d{1,5})\s+((?:[a-z]+\s+){0,3}?)("
    + "|".join(sorted(_SUFFIXES, key=len, reverse=True))
    + r")(?![a-z0-9])"
)
_DIRECTIONS = {"s": "south", "n": "north", "e": "east", "w": "west"}
_CUE_LABEL_RE = re.compile(r"\b(?:location|where|venue|place)\s*:\s*([^\n;.]{2,120})", re.IGNORECASE)
_CUE_INAT_RE = re.compile(r"\b(?:in|at)\s+(?:the\s+)?([A-Z0-9][^\n;,.!?]{1,80})")


def normalize(text: str) -> str:
    """Lowercase, strip accents/emoji/punctuation, collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").lower().replace("&", " and ")
    ascii_text = re.sub(r"(?<=[a-z])'(?=s\b)", "", ascii_text)
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def normalize_address(number: str, street: str, suffix: str) -> str:
    words = [_DIRECTIONS.get(w, w) for w in street.split()]
    return " ".join([number, *words, _SUFFIXES[suffix]]).strip()


def find_addresses(norm_text: str) -> list[tuple[int, str]]:
    """``(position, canonical address)`` for every street address in normalized text."""
    found = []
    for m in _ADDRESS_RE.finditer(norm_text):
        street = m.group(2).strip()
        if m.group(3) != "broadway" and not street:
            continue
        found.append((m.start(), normalize_address(m.group(1), street, m.group(3))))
    return found


@dataclass(frozen=True)
class Building:
    id: str
    name: str
    short: str | None
    lat: float
    lng: float
    source: str

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.short})" if self.short else self.name


@dataclass(frozen=True)
class _Phrase:
    pattern: re.Pattern[str]
    text: str
    building: Building
    is_name: bool


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])")


def _prefix_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])(" + re.escape(prefix) + r")\s*([a-z]{0,3}\d[a-z0-9]*)")


class Gazetteer:
    """Offline building lookup built from ``data/buildings.yaml``."""

    def __init__(self, buildings: list[dict], non_physical: list[str]) -> None:
        self.buildings: dict[str, Building] = {}
        self._phrases: list[_Phrase] = []
        self._prefixes: list[tuple[re.Pattern[str], Building]] = []
        self._addresses: dict[str, Building] = {}
        self._non_physical = {normalize(v) for v in non_physical}
        for entry in buildings:
            self._add(entry)

    @classmethod
    def from_yaml(cls, path: Path) -> Gazetteer:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(data.get("buildings") or [], data.get("non_physical") or [])

    def _add(self, entry: dict) -> None:
        b = Building(id=entry["id"], name=entry["name"], short=entry.get("short"),
                     lat=float(entry["lat"]), lng=float(entry["lng"]), source=entry.get("source", "manual"))
        self.buildings[b.id] = b
        name_norm = normalize(b.name)
        phrases = {name_norm: True}
        for alias in entry.get("aliases") or []:
            phrases.setdefault(normalize(alias), False)
        for text, is_name in phrases.items():
            if text:
                self._phrases.append(_Phrase(_phrase_pattern(text), text, b, is_name))
        for prefix in entry.get("room_prefixes") or []:
            self._prefixes.append((_prefix_pattern(normalize(prefix)), b))
        for address in entry.get("addresses") or []:
            for _, canon in find_addresses(normalize(address)):
                self._addresses[canon] = b

    def match(self, location_raw: str | None) -> GeoResult:
        """exact -> alias / room prefix -> address -> fuzzy -> non_physical -> none."""
        if location_raw is None:
            return GeoResult()
        norm = normalize(location_raw)
        segments = {normalize(s) for s in re.split(r"[,;()\[\]]|\s-\s|:", location_raw)}
        return (self._match_named(norm, segments) or self.match_address(location_raw)
                or self._match_fuzzy(location_raw) or self._match_non_physical(norm) or GeoResult())

    def match_address(self, text: str | None) -> GeoResult | None:
        if not text:
            return None
        for _, canon in find_addresses(normalize(text)):
            b = self._addresses.get(canon)
            if b is not None:
                return _pin(b, GeocodeMethod.ADDRESS, 0.9, canon)
        return None

    def is_non_physical(self, location_raw: str) -> bool:
        return self._match_non_physical(normalize(location_raw)) is not None

    def _match_named(self, norm: str, segments: set[str]) -> GeoResult | None:
        """Earliest name/alias/room-prefix hit in the string wins; ties go to the longest."""
        best: tuple[int, int, GeoResult] | None = None
        for phrase in self._phrases:
            m = phrase.pattern.search(norm)
            if m is None:
                continue
            exact = phrase.is_name and phrase.text in segments
            result = _pin(phrase.building, GeocodeMethod.EXACT if exact else GeocodeMethod.ALIAS,
                          1.0 if exact else 0.95, phrase.text)
            best = _earlier(best, (m.start(), -len(phrase.text), result))
        for pattern, b in self._prefixes:
            m = pattern.search(norm)
            if m is not None:
                matched = f"{m.group(1)} {m.group(2)}"
                best = _earlier(best, (m.start(), -len(m.group(0)), _pin(b, GeocodeMethod.ALIAS, 0.95, matched)))
        return best[2] if best else None

    def _match_fuzzy(self, location_raw: str) -> GeoResult | None:
        """Leading name segment vs long names/aliases with ``fuzz.ratio`` (strict, no partials)."""
        lead = normalize(re.split(r"[,;()]|\s-\s", location_raw, maxsplit=1)[0])
        if len(lead) < FUZZY_MIN_LEN:
            return None
        best_score, best_phrase = 0.0, None
        for phrase in self._phrases:
            if len(phrase.text) < FUZZY_MIN_LEN:
                continue
            score = fuzz.ratio(lead, phrase.text)
            if score > best_score:
                best_score, best_phrase = score, phrase
        if best_phrase is None or best_score < FUZZY_THRESHOLD:
            return None
        result = _pin(best_phrase.building, GeocodeMethod.FUZZY, round(best_score / 100 * 0.85, 3), best_phrase.text)
        return result.model_copy(update={"score": round(best_score, 1)})

    def _match_non_physical(self, norm: str) -> GeoResult | None:
        if norm == "" or norm in self._non_physical:
            return GeoResult(method=GeocodeMethod.NON_PHYSICAL, confidence=1.0, matched=norm or None)
        return None


def _earlier(best: tuple[int, int, GeoResult] | None, cand: tuple[int, int, GeoResult]) -> tuple[int, int, GeoResult]:
    if best is None or cand[:2] < best[:2]:
        return cand
    return best


def _pin(b: Building, method: GeocodeMethod, confidence: float, matched: str) -> GeoResult:
    return GeoResult(lat=b.lat, lng=b.lng, building_id=b.id, display_name=b.display_name,
                     confidence=confidence, method=method, matched=matched)


def _atomic_write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class NominatimGeocoder:
    """OSM Nominatim bounded to New Haven; hits and misses cached forever; <= 1 request/second."""

    def __init__(self, client: httpx.AsyncClient, cache_path: Path, *, user_agent: str,
                 min_interval_s: float = 1.0, sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._cache_path = Path(cache_path)
        self._user_agent = user_agent
        self._min_interval = min_interval_s
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._last_request: float | None = None
        self._cache: dict[str, dict | None] = self._load_cache()

    def _load_cache(self) -> dict[str, dict | None]:
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    async def geocode(self, query: str) -> GeoResult | None:
        key = normalize(query)
        if not key:
            return None
        async with self._lock:
            if key in self._cache:
                return _from_cache(self._cache[key])
            hit = await self._fetch(query)
            if hit is False:
                return None
            self._cache[key] = hit
            _atomic_write_json(self._cache_path, self._cache)
            return _from_cache(hit)

    async def _fetch(self, query: str) -> dict | None | bool:
        """Cacheable outcome (dict hit / None miss) or ``False`` on any failure."""
        await self._wait_turn()
        params = {"q": query, "format": "jsonv2", "limit": "1", "countrycodes": "us",
                  "viewbox": NEW_HAVEN_VIEWBOX, "bounded": "1"}
        try:
            resp = await self._client.get(NOMINATIM_URL, params=params, headers={"User-Agent": self._user_agent})
            if resp.status_code != 200:
                log.warning("nominatim returned HTTP %s", resp.status_code)
                return False
            rows = resp.json()
            if not rows:
                return None
            top = rows[0]
            importance = float(top.get("importance") or 0.0)
            return {"lat": float(top["lat"]), "lng": float(top["lon"]),
                    "display_name": str(top.get("name") or top.get("display_name") or query)[:120],
                    "confidence": round(0.5 + 0.2 * min(max(importance, 0.0), 1.0), 3)}
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
            log.warning("nominatim lookup failed: %s", type(exc).__name__)
            return False

    async def _wait_turn(self) -> None:
        if self._last_request is not None:
            wait = self._min_interval - (self._monotonic() - self._last_request)
            if wait > 0:
                await self._sleep(wait)
        self._last_request = self._monotonic()


def _from_cache(hit: dict | None) -> GeoResult | None:
    if hit is None:
        return None
    return GeoResult(lat=hit["lat"], lng=hit["lng"], display_name=hit.get("display_name"),
                     confidence=hit.get("confidence", 0.5), method=GeocodeMethod.NOMINATIM)


class LocationResolver:
    """Combines gazetteer, address, description cues and (optionally) Nominatim. Never raises."""

    def __init__(self, gazetteer: Gazetteer, geocoder: NominatimGeocoder | None = None) -> None:
        self.gazetteer = gazetteer
        self.geocoder = geocoder

    async def resolve(self, location_raw: str | None, *, description_text: str | None = None,
                      location_hidden: bool = False, attendance_mode: str | None = None,
                      address: str | None = None) -> GeoResult:
        try:
            result = self._offline(location_raw, description_text, location_hidden, attendance_mode, address)
            if result.method is not GeocodeMethod.NONE or self.geocoder is None:
                return result
            query = self._nominatim_query(location_raw, address)
            if query is None:
                return result
            return await self.geocoder.geocode(query) or result
        except Exception as exc:  # noqa: BLE001 - resolution must never break enrichment
            log.warning("location resolution failed: %s", type(exc).__name__)
            return GeoResult()

    def resolve_offline(self, location_raw: str | None, *, description_text: str | None = None,
                        location_hidden: bool = False, attendance_mode: str | None = None,
                        address: str | None = None) -> GeoResult:
        try:
            return self._offline(location_raw, description_text, location_hidden, attendance_mode, address)
        except Exception as exc:  # noqa: BLE001 - resolution must never break enrichment
            log.warning("location resolution failed: %s", type(exc).__name__)
            return GeoResult()

    def _offline(self, location_raw: str | None, description_text: str | None, location_hidden: bool,
                 attendance_mode: str | None, address: str | None) -> GeoResult:
        if (attendance_mode or "").upper() == "ONLINE":
            return GeoResult(method=GeocodeMethod.NON_PHYSICAL, confidence=1.0, matched="attendance_mode=ONLINE")
        result = self.gazetteer.match(location_raw)
        if result.method is not GeocodeMethod.NONE:
            return result
        by_address = self.gazetteer.match_address(address)
        if by_address is not None:
            return by_address
        if location_hidden or not (location_raw or "").strip():
            cued = self._from_description(description_text)
            if cued is not None:
                return cued
        return result

    def _from_description(self, text: str | None) -> GeoResult | None:
        """Explicit cue ("Location: ...", "in WLH 208", "at the X") whose text starts with a known place."""
        if not text:
            return None
        for regex, confidence in ((_CUE_LABEL_RE, 0.6), (_CUE_INAT_RE, 0.5)):
            for m in regex.finditer(text):
                cue = m.group(1).strip()
                hit = self.gazetteer.match(cue)
                if hit.lat is None or hit.method is GeocodeMethod.FUZZY:
                    continue
                if regex is _CUE_INAT_RE and not normalize(cue).startswith(hit.matched or "\0"):
                    continue
                return hit.model_copy(update={"method": GeocodeMethod.DESCRIPTION,
                                              "confidence": min(confidence, hit.confidence)})
        return None

    def _nominatim_query(self, location_raw: str | None, address: str | None) -> str | None:
        """A physical-looking query for Nominatim, or None for online/TBD/blank values."""
        for candidate in (address, location_raw):
            if not candidate or self.gazetteer.is_non_physical(candidate):
                continue
            norm = normalize(candidate)
            if "zoom" in norm.split() or not re.search(r"[a-z]{3}", norm):
                continue
            if "," not in candidate and not re.search(r"\d", candidate) and len(norm.split()) < 2:
                continue
            return candidate if "new haven" in norm else f"{candidate}, New Haven, CT"
        return None
