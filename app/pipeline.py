"""Enrichment glue: ParsedEvent -> EnrichedEvent (food, free-to-attend, location).

Owned by the integrator. Sync and seed call ``build_enricher`` once and reuse the result.
Enrichment only runs for new or content-changed events (see ``app.parse.store.plan_upsert``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.enrich.food import FoodClassifier
from app.enrich.geo import Gazetteer, LocationResolver, NominatimGeocoder
from app.schemas import EnrichedEvent, ParsedEvent


@dataclass
class Enricher:
    classifier: FoodClassifier
    resolver: LocationResolver
    version: str  # changes whenever the lexicon or gazetteer file changes -> re-enrich everything

    async def enrich(self, parsed: ParsedEvent) -> EnrichedEvent:
        ev, ex = parsed.event, parsed.extras
        food = self.classifier.classify(
            title=ev.title,
            description_text=ev.description_text,
            food_details=ex.food_details,
            is_food_included=ex.is_food_included,
        )
        free = self.classifier.free_to_attend(
            cost_raw=ev.cost_raw, title=ev.title, description_text=ev.description_text
        )
        geo = await self.resolver.resolve(
            ev.location_raw,
            description_text=ev.description_text,
            location_hidden=ex.location_hidden,
            attendance_mode=ex.attendance_mode,
            address=ex.location_address,
        )
        return EnrichedEvent(parsed=parsed, food=food, free=free, geo=geo)


def enrichment_version(settings: Settings) -> str:
    digest = hashlib.sha256()
    for path in (settings.food_lexicon_path, settings.buildings_path):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def build_enricher(settings: Settings, client: httpx.AsyncClient | None = None, *, online: bool | None = None) -> Enricher:
    """``online`` defaults to ``settings.online_geocoding``; Nominatim also needs an httpx client."""
    use_online = settings.online_geocoding if online is None else online
    classifier = FoodClassifier.from_yaml(settings.food_lexicon_path)
    gazetteer = Gazetteer.from_yaml(settings.buildings_path)
    geocoder = None
    if use_online and client is not None:
        geocoder = NominatimGeocoder(client, settings.geocode_cache_path, user_agent=settings.user_agent)
    return Enricher(classifier=classifier, resolver=LocationResolver(gazetteer, geocoder),
                    version=enrichment_version(settings))
