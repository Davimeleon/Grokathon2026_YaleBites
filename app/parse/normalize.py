"""Normalization helpers shared by the GraphQL and ICS parsers: ids, content hash, cost and location."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from app.parse.text import collapse_ws
from app.parse.times import shift_local_days
from app.schemas import (
    EventExtras,
    GqlExternalRegistration,
    GqlLocation,
    GqlPrice,
    GqlRegistration,
    IcsEvent,
    NormalizedEvent,
    ParsedEvent,
    Visibility,
)


def make_event_id(source_url: str, ics_uid: str | None) -> str:
    """The ICS UID when known, else sha256(source_url)."""
    if ics_uid:
        return ics_uid
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()


def _utc_iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt is not None else None


def compute_content_hash(event: NormalizedEvent, extras: EventExtras) -> str:
    """sha256 over the semantic fields plus the food inputs (docs/sources.md section 5).

    ``id``, ``source_url`` and ``updated_at_source`` are deliberately excluded.
    """
    payload = [
        event.title,
        event.description_raw,
        event.description_text,
        event.host_group,
        _utc_iso(event.starts_at),
        _utc_iso(event.ends_at),
        event.location_raw,
        event.cost_raw,
        Visibility(event.visibility).value,
        extras.is_food_included,
        extras.food_details,
    ]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rehash(parsed: ParsedEvent, **event_updates: object) -> ParsedEvent:
    """Apply updates to the NormalizedEvent and recompute its content hash."""
    event = parsed.event.model_copy(update=event_updates)
    content_hash = compute_content_hash(event, parsed.extras)
    return ParsedEvent(event=event.model_copy(update={"content_hash": content_hash}), extras=parsed.extras)


def _render_price(price: GqlPrice | None) -> str | None:
    if price is None:
        return None
    if price.is_flexible:
        return "pay what you want"
    if price.amount is None:
        return None
    rendered = f"${price.amount:,.2f} {price.currency or 'USD'}"
    return f"free ({rendered})" if price.amount == 0 else rendered


def _render_option(option_name: str | None, price: GqlPrice | None) -> str | None:
    name = collapse_ws(option_name)
    rendered = _render_price(price)
    if name and rendered:
        return f"{name}: {rendered}"
    return name or rendered


def build_cost_raw(
    registration: GqlRegistration | None, external_registration: GqlExternalRegistration | None
) -> str | None:
    """Render registration options, capacity and external sign-up as one text line, or None."""
    parts: list[str] = []
    if registration is not None:
        parts += [r for r in (_render_option(o.name, o.price) for o in registration.options) if r]
        if registration.capacity_mode:
            parts.append(f"capacity: {registration.capacity_mode.lower()}")
    if external_registration is not None and external_registration.url:
        parts.append(f"external registration: {external_registration.url.strip()}")
    return "; ".join(parts) or None


def _address_string(location: GqlLocation) -> tuple[str | None, str | None]:
    """(street, "street, city, state zip") from the location address, empty parts dropped."""
    address = location.address
    if address is None:
        return None, None
    street = collapse_ws(address.street)
    city = collapse_ws(address.city)
    state_zip = collapse_ws(" ".join(p for p in (address.state or "", address.zipcode or "") if p))
    text = ", ".join(p for p in (street, city, state_zip) if p)
    return street, text or None


def build_location_raw(location: GqlLocation | None) -> tuple[str | None, str | None]:
    """(location_raw, location_address) per docs/sources.md section 5."""
    if location is None:
        return None, None
    name = collapse_ws(location.name)
    street, address = _address_string(location)
    if name is None:
        return address, address
    if street and street.lower() not in name.lower() and address:
        return f"{name}, {address}", address
    return name, address


def apply_ics_metadata(parsed: ParsedEvent, rec: IcsEvent) -> ParsedEvent:
    """Attach the ICS UID (as id and extras.ics_uid) and LAST-MODIFIED; recompute the hash."""
    extras = parsed.extras.model_copy(update={"ics_uid": rec.uid})
    with_extras = ParsedEvent(event=parsed.event, extras=extras)
    return rehash(with_extras, id=rec.uid, updated_at_source=rec.last_modified)


def with_visibility(parsed: ParsedEvent, visibility: Visibility) -> ParsedEvent:
    return rehash(parsed, visibility=visibility)


def shift_days(parsed: ParsedEvent, days: int) -> ParsedEvent:
    """Shift start/end by whole campus-local days (DST-safe) and recompute the hash."""
    event = parsed.event
    ends_at = shift_local_days(event.ends_at, days) if event.ends_at is not None else None
    return rehash(parsed, starts_at=shift_local_days(event.starts_at, days), ends_at=ends_at)
