"""Sync orchestration: fetch -> parse -> plan -> enrich (changed only) -> upsert -> absence.

* Full sync: next ``settings.full_sync_days`` days, every ``settings.full_sync_minutes`` minutes.
* Hot sync: next ``settings.hot_sync_hours`` hours, every ``settings.hot_sync_minutes`` minutes.
  Pages are keyed by (kind, mode, page index), so a page whose body did not change since the previous
  run is not parsed at all. Its stored source_urls are just marked as seen.
* Authenticated mode runs an anonymous pass and an authenticated pass over the same window. An event
  seen only in the authenticated pass gets ``visibility = authenticated``.
* Removal: an event missing from two consecutive complete syncs of the same auth mode becomes
  ``canceled_or_removed``. Anonymous syncs never count absence for authenticated-only events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import clock
from app.auth import AuthManager, count_collapsed
from app.config import Settings
from app.db.models import AppState, Event, SyncRun
from app.fetch.client import FetchClient
from app.fetch.graphql import fetch_events_page, page_info
from app.fetch.ics import fetch_ics
from app.fetch.redact import install_redaction, redact_text
from app.parse import store
from app.parse.graphql import parse_events_response
from app.parse.ics import ics_events_to_parsed, ics_uid_index, parse_ics_records
from app.parse.normalize import apply_ics_metadata, shift_days, with_visibility
from app.pipeline import Enricher, build_enricher
from app.schemas import (
    AuthMode,
    AuthStatus,
    EnrichedEvent,
    IcsEvent,
    ParsedEvent,
    SyncKind,
    SyncResult,
    Visibility,
)
from app.sources import ICS_SCHOOL_URL

log = logging.getLogger("app.sync")

UNCHANGED = {"unchanged", "not_modified"}
MAX_PAGES = 200  # 10,000 events per window: a runaway-pagination backstop
ENRICH_CONCURRENCY = 8


# ---------------------------------------------------------------------------
# small persistence helpers
# ---------------------------------------------------------------------------


def get_state(session: Session, key: str) -> dict | None:
    row = session.get(AppState, key)
    return dict(row.value) if row is not None else None


def set_state(session: Session, key: str, value: dict) -> None:
    row = session.get(AppState, key)
    if row is None:
        session.add(AppState(key=key, value=value, updated_at=clock.now()))
    else:
        row.value = value
        row.updated_at = clock.now()


def floor_to(dt: datetime, minutes: int) -> datetime:
    epoch = int(dt.timestamp())
    step = minutes * 60
    return datetime.fromtimestamp(epoch - epoch % step, tz=dt.tzinfo)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


# ---------------------------------------------------------------------------
# one GraphQL pass over a window
# ---------------------------------------------------------------------------


@dataclass
class PassResult:
    parsed: dict[str, ParsedEvent] = field(default_factory=dict)  # source_url -> parsed (changed pages only)
    seen: set[str] = field(default_factory=set)  # every source_url returned, parsed or not
    unchanged_seen: set[str] = field(default_factory=set)  # seen only through unchanged pages
    pending_items: list[tuple[str, list[str], dict]] = field(default_factory=list)  # recorded after commit
    pages: int = 0
    unchanged_pages: int = 0
    parse_errors: int = 0
    complete: bool = False
    auth_failure: bool = False
    errors: list[str] = field(default_factory=list)


def _page_info_from_body(body: bytes) -> dict | None:
    """pageInfo by plain dict access (one malformed node never breaks pagination); None if absent."""
    info = page_info(body)
    return None if info is None else {"endCursor": info.end_cursor, "hasNextPage": info.has_next_page}


async def graphql_pass(
    fc: FetchClient,
    session_factory: sessionmaker[Session],
    *,
    start: datetime,
    end: datetime,
    authenticated: bool,
    key_prefix: str,
) -> PassResult:
    result = PassResult()
    visibility = Visibility.AUTHENTICATED if authenticated else Visibility.PUBLIC
    after: str | None = None
    for page_no in range(1, MAX_PAGES + 1):
        logical_key = f"{key_prefix}:p{page_no}"
        fetched = await fetch_events_page(
            fc, start=start, end=end, after=after, authenticated=authenticated, logical_key=logical_key
        )
        if fetched.status == "auth_redirect":
            result.auth_failure = True
            result.errors.append(f"{logical_key}: authenticated request redirected to sign-in")
            return result
        if fetched.status in UNCHANGED:
            with session_factory() as session:
                page_info = get_state(session, f"pageinfo:{logical_key}")
            if page_info is None:
                result.errors.append(f"{logical_key}: unchanged page without stored page info")
                return result
            result.pages += 1
            result.unchanged_pages += 1
            result.seen.update(fetched.item_keys)
            result.unchanged_seen.update(fetched.item_keys)
        elif fetched.status == "ok" and fetched.body is not None:
            page_info = _page_info_from_body(fetched.body)
            events, errors, _info = parse_events_response(fetched.body, visibility=visibility)
            if page_info is None:
                result.errors.append(f"{logical_key}: response without data.events ({len(errors)} errors)")
                if authenticated and any("AUTH_NOT_AUTHENTICATED" in e.message for e in errors):
                    result.auth_failure = True
                return result
            result.pages += 1
            result.parse_errors += len(errors)
            for err in errors:
                log.warning("parse error in %s (%s): %s", logical_key, err.ref, err.message)
            urls = [p.event.source_url for p in events]
            for parsed in events:
                result.parsed[parsed.event.source_url] = parsed
            result.seen.update(urls)
            result.pending_items.append((fetched.request_key, urls, {"key": logical_key, "info": page_info}))
        else:
            result.errors.append(f"{logical_key}: {fetched.status} {redact_text(fetched.error or '')}".strip())
            return result
        if not page_info["hasNextPage"]:
            result.complete = True
            return result
        after = page_info["endCursor"]
    result.errors.append(f"{key_prefix}: stopped after {MAX_PAGES} pages")
    return result


# ---------------------------------------------------------------------------
# ICS
# ---------------------------------------------------------------------------


@dataclass
class IcsOutcome:
    index: dict[int, tuple[str, str | None]]
    stats: dict
    records_in_window: list[IcsEvent] = field(default_factory=list)


async def load_ics_index(
    fc: FetchClient, session_factory: sessionmaker[Session], *, start: datetime, end: datetime
) -> IcsOutcome:
    fetched = await fetch_ics(fc, ICS_SCHOOL_URL, authenticated=False)
    stats: dict = {"last_fetch_at": iso(clock.now()), "status": fetched.status}
    with session_factory() as session:
        stored = get_state(session, "ics_index") or {}
    if fetched.status == "ok" and fetched.body is not None:
        records, errors = parse_ics_records(fetched.body.decode("utf-8", errors="replace"))
        for err in errors[:20]:
            log.warning("ICS parse error (%s): %s", err.ref, err.message)
        by_id = ics_uid_index(records)
        index = {eid: (rec.uid, iso(rec.last_modified)) for eid, rec in by_id.items()}
        last_modified = fetched.headers.get("last-modified")
        with session_factory() as session:
            set_state(session, "ics_index", {
                "last_modified": last_modified,
                "events": {str(k): list(v) for k, v in index.items()},
            })
            session.commit()
        fc.record_items(fetched.request_key, ["ics_index"])
        in_window = [r for r in records if _overlaps(r.dtstart, r.dtend, start, end)]
        stats.update(last_modified=last_modified, events_in_window=len(in_window), parse_errors=len(errors))
        return IcsOutcome(index=index, stats=stats, records_in_window=in_window)
    index = {int(k): (v[0], v[1]) for k, v in (stored.get("events") or {}).items()}
    stats.update(last_modified=stored.get("last_modified"), events_in_window=None)
    if fetched.status not in UNCHANGED:
        stats["error"] = redact_text(fetched.error or "")
    return IcsOutcome(index=index, stats=stats)


def _overlaps(s: datetime, e: datetime | None, ws: datetime, we: datetime) -> bool:
    end = e if e is not None else s + timedelta(hours=1)
    return s < we and end > ws


def apply_ics(parsed: ParsedEvent, index: dict[int, tuple[str, str | None]]) -> ParsedEvent:
    hit = index.get(parsed.extras.cg_event_id)
    if hit is None or parsed.extras.ics_uid:
        return parsed
    uid, last_modified = hit
    rec = IcsEvent(
        uid=uid,
        event_id=parsed.extras.cg_event_id,
        dtstart=parsed.event.starts_at,
        last_modified=datetime.fromisoformat(last_modified) if last_modified else None,
    )
    return apply_ics_metadata(parsed, rec)


# ---------------------------------------------------------------------------
# absence
# ---------------------------------------------------------------------------


def mark_absent(
    session: Session, *, window_start: datetime, window_end: datetime, seen: set[str], mode: AuthMode,
    now: datetime, personal_feeds_ok: bool = False,
) -> int:
    rows = session.scalars(
        select(Event).where(Event.status == "active", Event.starts_at < window_end)
    ).all()
    removed = 0
    for row in rows:
        if row.source_url in seen or not _overlaps(row.starts_at, row.ends_at, window_start, window_end):
            continue
        if row.source == "ics_personal" and not personal_feeds_ok:
            continue
        if mode == AuthMode.ANONYMOUS and row.visibility == Visibility.AUTHENTICATED.value:
            continue
        if row.missing_mode == mode.value:
            row.missing_count += 1
        else:
            row.missing_count, row.missing_mode = 1, mode.value
        if row.missing_count >= 2:
            row.status, row.removed_at = "canceled_or_removed", now
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


@dataclass
class RunPlan:
    kind: SyncKind
    start: datetime
    end: datetime
    use_ics: bool
    allow_auth: bool = True
    shift_days: int = 0
    count_absence: bool = True
    key_scope: str = ""


class SyncRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Session],
        fetch_client: FetchClient,
        enricher: Enricher,
        on_complete: Callable[[SyncResult], None] | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.fc = fetch_client
        self.enricher = enricher
        self.on_complete = on_complete
        self.auth = AuthManager(settings)  # long-lived: warns once per transition to expired, not every run
        self._lock = asyncio.Lock()
        self.extra_clients: list[httpx.AsyncClient] = []

    async def aclose(self) -> None:
        await self.fc.aclose()
        for client in self.extra_clients:
            await client.aclose()

    def plan_for(self, kind: SyncKind) -> RunPlan:
        now = clock.now()
        if kind == SyncKind.HOT:
            start = floor_to(now, 5)
            return RunPlan(kind, start, start + timedelta(hours=self.settings.hot_sync_hours), use_ics=False)
        start = floor_to(now, 5)
        return RunPlan(SyncKind.FULL, start, start + timedelta(days=self.settings.full_sync_days), use_ics=True)

    async def run(self, kind: SyncKind) -> SyncResult:
        return await self.run_plan(self.plan_for(kind))

    async def run_plan(self, plan: RunPlan) -> SyncResult:
        async with self._lock:
            started = clock.now()
            t0 = time.monotonic()
            with self.session_factory() as session:
                run = SyncRun(kind=plan.kind.value, mode=AuthMode.ANONYMOUS.value, auth_status=AuthStatus.NONE.value,
                              started_at=started, window_start=plan.start, window_end=plan.end)
                session.add(run)
                session.commit()
                run_id = run.id
            try:
                result, source_stats = await self._execute(plan, run_id, started)
            except Exception as exc:
                log.exception("sync run %s failed", run_id)
                result = SyncResult(
                    run_id=run_id, kind=plan.kind, mode=AuthMode.ANONYMOUS, auth_status=AuthStatus.NONE,
                    started_at=started, finished_at=clock.now(), window_start=plan.start, window_end=plan.end,
                    complete=False, errors=[redact_text(f"{type(exc).__name__}: {exc}")],
                )
                self._finish_row(run_id, result, {}, int((time.monotonic() - t0) * 1000))
            else:
                self._finish_row(run_id, result, source_stats, int((time.monotonic() - t0) * 1000))
            log.info(
                "sync %s #%s %s/%s complete=%s seen=%d created=%d updated=%d unchanged=%d removed=%d "
                "auth_only=%d parse_errors=%d skipped_responses=%d errors=%d",
                result.kind.value, run_id, result.mode.value, result.auth_status.value, result.complete,
                result.events_seen, result.events_created, result.events_updated, result.events_unchanged,
                result.events_removed, result.authenticated_only_seen, result.parse_errors,
                result.skipped_unchanged_responses, len(result.errors),
            )
            if self.on_complete is not None:
                self.on_complete(result)
            return result

    def _finish_row(self, run_id: int, result: SyncResult, source_stats: dict, duration_ms: int) -> None:
        with self.session_factory() as session:
            row = session.get(SyncRun, run_id)
            row.finished_at = result.finished_at
            row.duration_ms = duration_ms
            row.mode = result.mode.value
            row.auth_status = result.auth_status.value
            row.complete = result.complete
            row.events_seen = result.events_seen
            row.events_created = result.events_created
            row.events_updated = result.events_updated
            row.events_unchanged = result.events_unchanged
            row.events_removed = result.events_removed
            row.authenticated_only_seen = result.authenticated_only_seen
            row.parse_errors = result.parse_errors
            row.skipped_unchanged_responses = result.skipped_unchanged_responses
            row.errors = [redact_text(e) for e in result.errors]
            row.source_stats = source_stats or row.source_stats
            session.commit()

    async def _execute(self, plan: RunPlan, run_id: int, started: datetime) -> tuple[SyncResult, dict]:
        errors: list[str] = []
        stats: dict = {}
        auth = self.auth
        auth_status = AuthStatus.NONE
        if plan.allow_auth and auth.has_cookie:
            auth_status = await auth.check(self.fc)
        want_auth = auth_status == AuthStatus.VALID

        ics = IcsOutcome(index={}, stats={})
        if plan.use_ics:
            ics = await load_ics_index(self.fc, self.session_factory, start=plan.start, end=plan.end)
            stats["ics"] = ics.stats
        else:
            with self.session_factory() as session:
                stored = get_state(session, "ics_index") or {}
            ics.index = {int(k): (v[0], v[1]) for k, v in (stored.get("events") or {}).items()}

        scope = plan.key_scope or plan.kind.value
        anon = await graphql_pass(self.fc, self.session_factory, start=plan.start, end=plan.end,
                                  authenticated=False, key_prefix=f"graphql:{scope}:anonymous")
        errors += anon.errors
        stats["graphql"] = {"last_fetch_at": iso(clock.now()), "pages": anon.pages, "events": len(anon.seen),
                            "status": "ok" if anon.complete else "error", "unchanged_pages": anon.unchanged_pages}

        auth_pass: PassResult | None = None
        if want_auth:
            auth_pass = await graphql_pass(self.fc, self.session_factory, start=plan.start, end=plan.end,
                                           authenticated=True, key_prefix=f"graphql:{scope}:authenticated")
            stats["graphql_auth"] = {"last_fetch_at": iso(clock.now()), "pages": auth_pass.pages,
                                     "events": len(auth_pass.seen), "unchanged_pages": auth_pass.unchanged_pages,
                                     "status": "ok" if auth_pass.complete else "error"}
            previous = self._previous_auth_count(plan.kind)
            collapsed = auth_pass.complete and anon.complete and count_collapsed(
                len(auth_pass.seen), len(anon.seen), previous)
            if auth_pass.auth_failure or collapsed:
                auth_status = auth.mark_expired("event count collapsed" if collapsed else "redirected to sign-in")
                auth_pass = None
            elif not auth_pass.complete:
                errors += auth_pass.errors
                auth_pass = None

        personal: list[ParsedEvent] = []
        if plan.allow_auth and plan.use_ics and auth.ics_feeds:
            personal, personal_stats = await self._personal_feeds(plan, auth)
            stats["ics_personal"] = personal_stats

        mode = AuthMode.AUTHENTICATED if auth_pass is not None else AuthMode.ANONYMOUS
        merged, seen, unchanged_seen, auth_only = self._merge(anon, auth_pass, personal)

        fallback = False
        if not anon.complete and plan.use_ics and ics.records_in_window and not merged:
            parsed, errs = ics_events_to_parsed(ics.records_in_window, visibility=Visibility.PUBLIC)
            merged = {p.event.source_url: p for p in parsed}
            seen = set(merged)
            fallback = True
            errors.append(f"GraphQL unavailable; used {len(merged)} ICS events (locations mostly withheld)")
            anon.parse_errors += len(errs)

        events = [apply_ics(p, ics.index) for p in merged.values()]
        if plan.shift_days:
            events = [shift_days(p, plan.shift_days) for p in events]

        now = clock.now()
        with self.session_factory() as session:
            upsert_plan = store.plan_upsert(session, events)
        enriched = await self._enrich(upsert_plan.to_enrich)
        complete = anon.complete and not fallback and (not want_auth or auth_status != AuthStatus.VALID
                                                       or auth_pass is not None)
        with self.session_factory() as session:
            upsert = store.apply_upsert(session, upsert_plan, enriched, now=now, sync_run_id=run_id)
            touched = store.touch_seen(session, unchanged_seen - set(merged), now=now, sync_run_id=run_id)
            removed = 0
            if complete and plan.count_absence:
                personal_ok = stats.get("ics_personal", {}).get("status") == "ok"
                removed = mark_absent(session, window_start=plan.start, window_end=plan.end, seen=seen,
                                      mode=mode, now=now, personal_feeds_ok=personal_ok)
            if plan.kind != SyncKind.SEED:
                set_state(session, "dataset", {"kind": "live", "time_shift_days": 0})
            set_state(session, "enrichment_version", {"version": self.enricher.version})
            passes = [p for p in (anon, auth_pass) if p is not None]
            for p in passes:
                for _key, _urls, meta in p.pending_items:
                    set_state(session, f"pageinfo:{meta['key']}", meta["info"])
            session.commit()
        # Arm validators only after the data they describe is committed (FetchClient uses its own session,
        # and SQLite allows one writer at a time).
        for p in passes:
            for request_key, urls, _meta in p.pending_items:
                self.fc.record_items(request_key, urls)

        skipped = anon.unchanged_pages + (auth_pass.unchanged_pages if auth_pass else 0)
        if stats.get("ics", {}).get("status") in UNCHANGED:
            skipped += 1
        result = SyncResult(
            run_id=run_id, kind=plan.kind, mode=mode, auth_status=auth_status, started_at=started,
            finished_at=clock.now(), window_start=plan.start, window_end=plan.end, complete=complete,
            events_seen=len(seen), events_created=upsert.created, events_updated=upsert.updated,
            events_unchanged=upsert.unchanged + touched, events_removed=removed,
            authenticated_only_seen=len(auth_only),
            parse_errors=anon.parse_errors + (auth_pass.parse_errors if auth_pass else 0),
            errors=errors, skipped_unchanged_responses=skipped,
        )
        return result, stats

    def _merge(
        self, anon: PassResult, auth_pass: PassResult | None, personal: list[ParsedEvent]
    ) -> tuple[dict[str, ParsedEvent], set[str], set[str], set[str]]:
        merged: dict[str, ParsedEvent] = dict(anon.parsed)
        seen = set(anon.seen)
        unchanged_seen = set(anon.unchanged_seen)
        auth_only: set[str] = set()
        if auth_pass is not None:
            for url, parsed in auth_pass.parsed.items():
                vis = Visibility.PUBLIC if url in anon.seen else Visibility.AUTHENTICATED
                merged[url] = with_visibility(parsed, vis)
            auth_only = auth_pass.seen - anon.seen
            seen |= auth_pass.seen
            unchanged_seen |= auth_pass.unchanged_seen
        for parsed in personal:
            url = parsed.event.source_url
            if url in seen:
                continue  # GraphQL already has it (richer record)
            merged[url] = with_visibility(parsed, Visibility.AUTHENTICATED)
            seen.add(url)
            auth_only.add(url)
        return merged, seen, unchanged_seen, auth_only

    async def _personal_feeds(self, plan: RunPlan, auth: AuthManager) -> tuple[list[ParsedEvent], dict]:
        out: list[ParsedEvent] = []
        stats = {"last_fetch_at": iso(clock.now()), "feeds": len(auth.ics_feeds), "events": 0, "status": "ok"}
        for secret in auth.ics_feeds:
            fetched = await fetch_ics(self.fc, secret.reveal(), authenticated=True)
            if fetched.status != "ok" or fetched.body is None:
                # A 304 has nothing new to parse, but this run cannot vouch for that feed's events either.
                stats["status"] = "not_modified" if fetched.status in UNCHANGED and stats["status"] == "ok" else "error"
                continue
            records, _errors = parse_ics_records(fetched.body.decode("utf-8", errors="replace"))
            window = [r for r in records if _overlaps(r.dtstart, r.dtend, plan.start, plan.end)]
            parsed, _ = ics_events_to_parsed(window, visibility=Visibility.AUTHENTICATED, source="ics_personal")
            out += parsed
        stats["events"] = len(out)
        return out, stats

    def _previous_auth_count(self, kind: SyncKind) -> int | None:
        with self.session_factory() as session:
            row = session.scalars(
                select(SyncRun)
                .where(SyncRun.kind == kind.value, SyncRun.mode == AuthMode.AUTHENTICATED.value,
                       SyncRun.complete.is_(True))
                .order_by(SyncRun.id.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            return int((row.source_stats or {}).get("graphql_auth", {}).get("events") or row.events_seen)

    async def _enrich(self, parsed: list[ParsedEvent]) -> dict[str, EnrichedEvent]:
        sem = asyncio.Semaphore(ENRICH_CONCURRENCY)

        async def one(p: ParsedEvent) -> EnrichedEvent:
            async with sem:
                return await self.enricher.enrich(p)

        done = await asyncio.gather(*(one(p) for p in parsed))
        return {e.parsed.event.source_url: e for e in done}

    async def reenrich_if_needed(self) -> int:
        with self.session_factory() as session:
            current = (get_state(session, "enrichment_version") or {}).get("version")
            if current == self.enricher.version:
                return 0
            parsed = store.load_parsed(session, active_only=False)
        enriched = await self._enrich(parsed)
        with self.session_factory() as session:
            count = store.apply_enrichment(session, enriched)
            set_state(session, "enrichment_version", {"version": self.enricher.version})
            session.commit()
        log.info("re-enriched %d events (lexicon/gazetteer changed)", count)
        return count


async def build_runtime(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    on_complete: Callable[[SyncResult], None] | None = None,
    transport=None,
    online: bool | None = None,
) -> SyncRunner:
    install_redaction(settings)
    fc = FetchClient(settings, session_factory, transport=transport)
    use_online = settings.online_geocoding if online is None else online
    # Nominatim is a different host with its own 1 req/s limit; it gets its own small client.
    geo_client = httpx.AsyncClient(timeout=20, headers={"User-Agent": settings.user_agent}) if use_online else None
    enricher = build_enricher(settings, geo_client, online=use_online)
    runner = SyncRunner(settings=settings, session_factory=session_factory, fetch_client=fc, enricher=enricher,
                        on_complete=on_complete)
    if geo_client is not None:
        runner.extra_clients.append(geo_client)
    await runner.reenrich_if_needed()
    return runner


def create_scheduler(runner: SyncRunner, settings: Settings) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(runner.run, "interval", args=[SyncKind.FULL], minutes=settings.full_sync_minutes,
                      id="full_sync", next_run_time=clock.now(), max_instances=1, coalesce=True,
                      misfire_grace_time=300)
    scheduler.add_job(runner.run, "interval", args=[SyncKind.HOT], minutes=settings.hot_sync_minutes,
                      id="hot_sync", next_run_time=clock.now() + timedelta(seconds=90), max_instances=1,
                      coalesce=True, misfire_grace_time=60)
    return scheduler


async def demo_full_sync(settings: Settings, fc: FetchClient, auth: AuthManager, *, out=None) -> int:
    """``make demo-auth`` valid-session path: one full sync, then anonymous vs authenticated counts."""
    import sys

    from app.db.session import init_db, make_engine, make_session_factory

    stream = out or sys.stdout
    engine = make_engine(settings.db_path)
    init_db(engine)
    factory = make_session_factory(engine)
    try:
        runner = SyncRunner(settings=settings, session_factory=factory, fetch_client=fc,
                            enricher=build_enricher(settings, None, online=False))
        result = await runner.run(SyncKind.FULL)
        with factory() as session:
            stats = session.get(SyncRun, result.run_id).source_stats or {}
            revealed = session.scalar(
                select(func.count()).select_from(Event).where(
                    Event.location_privacy == "SIGN_IN_REQUIRED", Event.location_raw.is_not(None),
                    Event.status == "active"))
        anon_n = (stats.get("graphql") or {}).get("events", 0)
        auth_n = (stats.get("graphql_auth") or {}).get("events")
        print(f"sync #{result.run_id}: mode={result.mode.value} auth_status={result.auth_status.value} "
              f"complete={result.complete}", file=stream)
        if auth_n is None:
            print(f"anonymous events: {anon_n}; authenticated pass did not complete "
                  f"({result.auth_status.value}). Refresh YALECONNECT_COOKIE.", file=stream)
            personal = (stats.get("ics_personal") or {}).get("events")
            if personal is not None:
                print(f"personal ICS feed events: {personal} ({result.authenticated_only_seen} authenticated-only)",
                      file=stream)
            return 0 if result.complete and auth.has_cookie is False else 3
        print(f"anonymous events: {anon_n}, authenticated events: {auth_n} "
              f"({result.authenticated_only_seen} authenticated-only, {revealed} locations revealed)", file=stream)
        return 0 if result.complete else 1
    finally:
        engine.dispose()
