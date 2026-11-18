"""Unit tests for app.enrich.geo: gazetteer matching, resolver policy, Nominatim client."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from rapidfuzz import fuzz

from app.config import REPO_ROOT
from app.enrich.geo import (
    FUZZY_THRESHOLD,
    Gazetteer,
    LocationResolver,
    NominatimGeocoder,
    find_addresses,
    normalize,
)
from app.schemas import GeocodeMethod

UA = "yale-freefood-finder/0.1 (test)"


@pytest.fixture(scope="module")
def gaz() -> Gazetteer:
    return Gazetteer.from_yaml(REPO_ROOT / "data" / "buildings.yaml")


@pytest.fixture
def resolver(gaz) -> LocationResolver:
    return LocationResolver(gaz)


def test_exact_name(gaz):
    geo = gaz.match("Battell Chapel , 400 College Street , New Haven, CT 06511, United States")
    assert (geo.method, geo.building_id, geo.confidence) == (GeocodeMethod.EXACT, "battell", 1.0)
    assert geo.display_name == "Battell Chapel"


def test_alias(gaz):
    geo = gaz.match("Burke Auditorium, New Haven, CT 06520, United States")
    assert (geo.method, geo.building_id, geo.confidence) == (GeocodeMethod.ALIAS, "kroon", 0.95)


@pytest.mark.parametrize(("raw", "building"), [
    ("WLH 116", "wlh"), ("LC 101", "lc"), ("LC102", "lc"), ("HQ L01", "hq"), ("DL 220", "dl"),
    ("SSS 114", "sss"), ("BASSLB L70", "bass"), ("17 Hillhouse Ave, Room 101", "hillhouse17"),
    ("KT 101", "kbt"), ("BCT CO31", "becton"), ("SLB 129, New Haven, CT 06520, United States", "slb"),
])
def test_room_prefix(gaz, raw, building):
    geo = gaz.match(raw)
    assert geo.building_id == building
    assert geo.lat is not None and geo.confidence >= 0.9


def test_display_name_has_short_code(gaz):
    assert gaz.match("WLH 116, New Haven, CT 06520, United States").display_name == "William L. Harkness Hall (WLH)"


def test_name_beats_later_address(gaz):
    geo = gaz.match("Kroon Hall, Room 321 New Haven, CT 06520, United States, 400 West Campus Drive, Orange, CT")
    assert geo.building_id == "kroon"


def test_address_match(gaz):
    geo = gaz.match("127 Wall Street, New Haven, CT 06511, United States")
    assert (geo.method, geo.building_id, geo.confidence) == (GeocodeMethod.ADDRESS, "slb", 0.9)


def test_address_number_boundary():
    assert find_addresses(normalize("155 Whitney Ave")) == [(0, "155 whitney ave")]
    assert find_addresses(normalize("230 S. Frontage Road")) == [(0, "230 south frontage rd")]


def test_unknown_address_number_does_not_match(gaz):
    assert gaz.match("155 Whitney Ave, New Haven, CT").method is GeocodeMethod.NONE


def test_fuzzy_records_score(gaz):
    geo = gaz.match("Yale Centre for British Art")
    assert geo.method is GeocodeMethod.FUZZY
    assert geo.building_id == "ycba"
    assert geo.score is not None and 88 <= geo.score < 100
    assert geo.confidence == pytest.approx(geo.score / 100 * 0.85, abs=0.001)


def test_fuzzy_near_miss_below_threshold(gaz):
    near_miss = "Yale Center for Brazilian Art"
    assert 75 <= fuzz.ratio(normalize(near_miss), "yale center for british art") < FUZZY_THRESHOLD
    geo = gaz.match(near_miss)
    assert geo.method is GeocodeMethod.NONE and geo.lat is None


@pytest.mark.parametrize("raw", ["Online Event", "-", "TBD", "Zoom", "various public locations in New Haven"])
def test_non_physical(gaz, raw):
    geo = gaz.match(raw)
    assert geo.method is GeocodeMethod.NON_PHYSICAL and geo.lat is None and geo.confidence == 1.0


@pytest.mark.parametrize("raw", ["TBD: Kline Biology Tower (14th floor), 219 Prospect St", "Burke + Zoom, New Haven, CT"])
def test_placeholder_words_do_not_block_a_real_place(gaz, raw):
    assert gaz.match(raw).lat is not None


@pytest.mark.parametrize("raw", [
    "Antalya, Turkey", "New York City", "UN General Assembly Hall",
    "Music City Center, 201 Rep. John Lewis Way South, Nashville, TN 37203, United States",
    "Yale-Myers Forest, 150 Centre Pike, Eastford, CT 06242, United States",
])
def test_out_of_area_stays_unresolved(gaz, raw):
    geo = gaz.match(raw)
    assert geo.method is GeocodeMethod.NONE and geo.lat is None


def test_none_input(gaz):
    assert gaz.match(None).method is GeocodeMethod.NONE


def test_online_attendance_is_non_physical(resolver):
    geo = resolver.resolve_offline("WLH 116", attendance_mode="ONLINE")
    assert geo.method is GeocodeMethod.NON_PHYSICAL and geo.lat is None


def test_hybrid_still_pins(resolver):
    assert resolver.resolve_offline("WLH 116", attendance_mode="HYBRID").building_id == "wlh"


def test_address_argument_used_when_name_unknown(resolver):
    geo = resolver.resolve_offline("Sam's Office", address="1 Prospect Street, New Haven, CT 06511")
    assert (geo.method, geo.building_id) == (GeocodeMethod.ADDRESS, "sss")


@pytest.mark.parametrize(("text", "building"), [
    ("Join us for pizza! Location: WLH 208. Bring a friend.", "wlh"),
    ("Dinner will be served in WLH 208 after the talk.", "wlh"),
    ("Come hang out at the Afro-American Cultural Center this Friday.", "afam"),
])
def test_description_cue_when_hidden(resolver, text, building):
    geo = resolver.resolve_offline(None, description_text=text, location_hidden=True)
    assert (geo.method, geo.building_id) == (GeocodeMethod.DESCRIPTION, building)
    assert geo.confidence <= 0.6


def test_description_topical_mention_does_not_pin(resolver):
    geo = resolver.resolve_offline(None, description_text="A talk about Yale Law School history and its founders.",
                                   location_hidden=True)
    assert geo.method is GeocodeMethod.NONE and geo.lat is None


def test_description_ignored_when_location_known(resolver):
    geo = resolver.resolve_offline("Sam's Office", description_text="Location: WLH 208")
    assert geo.method is GeocodeMethod.NONE


async def test_resolve_never_raises(gaz):
    class Boom:
        async def geocode(self, query):
            raise RuntimeError("boom")

    geo = await LocationResolver(gaz, Boom()).resolve("Sam's Office, New Haven, CT")
    assert geo.method is GeocodeMethod.NONE


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _geocoder(handler, tmp_path, clock=None):
    clock = clock or FakeClock()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    geocoder = NominatimGeocoder(client, tmp_path / "cache.json", user_agent=UA,
                                 sleep=clock.sleep, monotonic=clock.monotonic)
    return geocoder, clock


def _hit(lat=41.3, lon=-72.93):
    return httpx.Response(200, json=[{"lat": str(lat), "lon": str(lon), "name": "Somewhere", "importance": 0.5}])


async def test_nominatim_params_and_cache_hit(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return _hit()

    geocoder, _ = _geocoder(handler, tmp_path)
    geo = await geocoder.geocode("Sam's Office, New Haven, CT")
    again = await geocoder.geocode("Sam's Office, New Haven, CT")
    assert len(seen) == 1
    params = seen[0].url.params
    assert params["viewbox"] == "-73.05,41.36,-72.85,41.22"
    assert params["bounded"] == "1" and params["countrycodes"] == "us"
    assert seen[0].headers["user-agent"] == UA
    assert geo == again and geo.method is GeocodeMethod.NOMINATIM
    assert 0.5 <= geo.confidence <= 0.7
    assert json.loads((tmp_path / "cache.json").read_text())


async def test_nominatim_cache_persists_across_instances(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return _hit()

    first, _ = _geocoder(handler, tmp_path)
    await first.geocode("Foo Hall")
    second, _ = _geocoder(handler, tmp_path)
    assert (await second.geocode("Foo Hall")).lat == pytest.approx(41.3)
    assert len(calls) == 1


async def test_nominatim_negative_caching(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[])

    geocoder, _ = _geocoder(handler, tmp_path)
    assert await geocoder.geocode("Nowhere Hall") is None
    assert await geocoder.geocode("Nowhere Hall") is None
    assert len(calls) == 1
    assert json.loads((tmp_path / "cache.json").read_text()) == {"nowhere hall": None}


@pytest.mark.parametrize("response", [httpx.Response(503), httpx.Response(200, text="not json")])
async def test_nominatim_error_returns_none_without_cache(tmp_path, response):
    geocoder, _ = _geocoder(lambda request: response, tmp_path)
    assert await geocoder.geocode("Foo Hall") is None
    assert not (tmp_path / "cache.json").exists()


async def test_nominatim_transport_error(tmp_path):
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    geocoder, _ = _geocoder(handler, tmp_path)
    assert await geocoder.geocode("Foo Hall") is None
    assert not (tmp_path / "cache.json").exists()


async def test_nominatim_rate_limit_across_concurrent_callers(tmp_path):
    times: list[float] = []
    clock = FakeClock()

    def handler(request):
        times.append(clock.t)
        return _hit()

    geocoder, _ = _geocoder(handler, tmp_path, clock)
    await asyncio.gather(*(geocoder.geocode(f"Place {i}") for i in range(3)))
    assert len(times) == 3
    assert all(b - a >= 1.0 for a, b in zip(times, times[1:], strict=False))
    assert clock.sleeps == [1.0, 1.0]


async def test_resolver_uses_geocoder_only_for_physical_misses(gaz, tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.params["q"])
        return _hit()

    geocoder, _ = _geocoder(handler, tmp_path)
    resolver = LocationResolver(gaz, geocoder)
    assert (await resolver.resolve("WLH 116")).method is GeocodeMethod.ALIAS
    assert (await resolver.resolve("Online Event")).method is GeocodeMethod.NON_PHYSICAL
    assert (await resolver.resolve("TBD")).method is GeocodeMethod.NON_PHYSICAL
    assert (await resolver.resolve("Room 5", attendance_mode="ONLINE")).method is GeocodeMethod.NON_PHYSICAL
    geo = await resolver.resolve("Sam's Office")
    assert geo.method is GeocodeMethod.NOMINATIM
    assert calls == ["Sam's Office, New Haven, CT"]
