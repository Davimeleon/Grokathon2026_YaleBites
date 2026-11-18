"""Offline coverage of every distinct location_raw in the fixture set (no geocoder)."""

from __future__ import annotations

import json
from pathlib import Path

from app.config import REPO_ROOT
from app.enrich.geo import Gazetteer, LocationResolver
from app.schemas import GeocodeMethod

VALUES_PATH = Path(__file__).parent / "data" / "location_raw_values.json"
# lat_min, lat_max, lng_min, lng_max: New Haven, West Haven and West Campus (Orange).
NEW_HAVEN_BBOX = (41.22, 41.36, -73.05, -72.85)


def test_offline_coverage(terminal_report):
    resolver = LocationResolver(Gazetteer.from_yaml(REPO_ROOT / "data" / "buildings.yaml"))
    values = [raw for raw, _count in json.loads(VALUES_PATH.read_text(encoding="utf-8"))]
    pinned, non_physical, unresolved, outside = 0, 0, [], []
    for raw in values:
        geo = resolver.resolve_offline(raw)
        if geo.lat is not None:
            pinned += 1
            lat_min, lat_max, lng_min, lng_max = NEW_HAVEN_BBOX
            if not (lat_min <= geo.lat <= lat_max and lng_min <= geo.lng <= lng_max):
                outside.append(raw)
        elif geo.method is GeocodeMethod.NON_PHYSICAL:
            non_physical += 1
        else:
            unresolved.append(raw)
    total = len(values)
    hit_rate = (pinned + non_physical) / total
    physical_rate = pinned / (total - non_physical)
    lines = [
        f"distinct={total} pinned={pinned} non_physical={non_physical} unresolved={len(unresolved)}",
        f"hit_rate={hit_rate:.3f} physical_only_rate={physical_rate:.3f}",
        *(f"  unresolved: {raw}" for raw in unresolved),
    ]
    terminal_report("geo coverage (offline)", lines)
    print("\n".join(lines))
    assert total == len(set(values)) == 376
    assert not outside, f"pins outside New Haven bbox: {outside}"
    assert hit_rate >= 0.85
    assert physical_rate >= 0.85
