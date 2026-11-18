"""FastAPI application factory: /api/events, /api/events/{id}, /api/meta, /healthz and the built frontend."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from app import clock
from app.api import cache
from app.api.events import EventQuery, SortKey, get_event, list_events
from app.api.meta import age_seconds, build_meta, data_as_of
from app.config import Settings, get_settings
from app.db.session import init_db, make_engine, make_session_factory
from app.schemas import EventsResponse, FoodLabel, WindowName

JSON = "application/json; charset=utf-8"
CACHE_CONTROL = "public, max-age=60"
DEFAULT_RADIUS_M = 1500


def _invalid(param: str, value: str, msg: str) -> RequestValidationError:
    return RequestValidationError([{"type": "value_error", "loc": ("query", param), "msg": msg, "input": value}])


def parse_food(raw: str | None) -> frozenset[str] | None:
    if raw is None:
        return None
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    allowed = {label.value for label in FoodLabel}
    if parts - allowed:
        raise _invalid("food", raw, f"food must be a comma list of {', '.join(sorted(allowed))}")
    return frozenset(parts) or None


def parse_near(raw: str | None) -> tuple[float, float] | None:
    if raw is None:
        return None
    parts = raw.split(",")
    try:
        lat, lng = (float(p) for p in parts) if len(parts) == 2 else (math.nan, math.nan)
    except ValueError:
        lat, lng = math.nan, math.nan
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        raise _invalid("near", raw, "near must be 'lat,lng' with -90<=lat<=90 and -180<=lng<=180")
    return lat, lng


def _session(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(_session)]


def _memoised(request: Request, build: Callable[[], bytes]) -> Response:
    """Serve a body from the 60 s memo, building and storing it on a miss."""
    key = cache.cache_key(request.url.path, request.url.query)
    body = cache.get_cached(key)
    if body is None:
        body = build()
        cache.put_cached(key, body)
    return Response(content=body, media_type=JSON)


def _events_handler(
    request: Request,
    session: SessionDep,
    window: WindowName = WindowName.TODAY,
    food: str | None = None,
    free_only: bool = False,
    near: str | None = None,
    radius_m: Annotated[int | None, Query(ge=1, le=20000)] = None,
    q: str | None = None,
    sort: SortKey = SortKey.START,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Response:
    origin = parse_near(near)
    if sort is SortKey.DISTANCE and origin is None:
        raise _invalid("sort", sort.value, "sort=distance requires near")
    params = EventQuery(
        window=window,
        food=parse_food(food),
        free_only=free_only,
        near=origin,
        radius_m=(radius_m or DEFAULT_RADIUS_M) if origin is not None else None,
        q=q or None,
        sort=sort,
        page=page,
        page_size=page_size,
    )

    def build() -> bytes:
        now = clock.now()
        result = list_events(session, params, now)
        as_of = data_as_of(session)
        body = EventsResponse(
            generated_at=now,
            data_as_of=as_of,
            data_age_seconds=age_seconds(as_of, now),
            window=result.window,
            total=result.total,
            page=page,
            page_size=page_size,
            next_page=result.next_page,
            items=result.items,
        )
        return body.model_dump_json().encode()

    return _memoised(request, build)


def _event_detail_handler(request: Request, session: SessionDep, event_id: str) -> Response:
    key = cache.cache_key(request.url.path, request.url.query)
    body = cache.get_cached(key)
    if body is None:
        event = get_event(session, event_id, clock.now())
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")
        body = event.model_dump_json().encode()
        cache.put_cached(key, body)
    return Response(content=body, media_type=JSON)


def _meta_handler(request: Request, session: SessionDep) -> Response:
    settings: Settings = request.app.state.settings
    return _memoised(request, lambda: build_meta(session, settings, clock.now()).model_dump_json().encode())


def _healthz() -> dict[str, bool]:
    return {"ok": True}


def _root_hint() -> dict[str, str]:
    return {
        "message": "frontend not built; run the web build to serve it here",
        "events": "/api/events",
        "meta": "/api/meta",
    }


def _add_api_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def api_headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = CACHE_CONTROL
            if response.headers.get("content-type") == "application/json":
                response.headers["Content-Type"] = JSON
            factory: sessionmaker[Session] = request.app.state.session_factory
            with factory() as session:
                age = age_seconds(data_as_of(session), clock.now())
            if age is not None:
                response.headers["X-Data-Age"] = str(age)
        return response


def _default_session_factory(settings: Settings) -> sessionmaker[Session]:
    engine = make_engine(settings.db_path)
    init_db(engine)
    return make_session_factory(engine)


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    *,
    lifespan=None,
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="freefood", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory or _default_session_factory(settings)

    app.add_api_route("/api/events", _events_handler, methods=["GET"])
    app.add_api_route("/api/events/{event_id:path}", _event_detail_handler, methods=["GET"])
    app.add_api_route("/api/meta", _meta_handler, methods=["GET"])
    app.add_api_route("/healthz", _healthz, methods=["GET"])
    _add_api_headers(app)

    dist = settings.web_dist_dir
    if (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    else:
        app.add_api_route("/", _root_hint, methods=["GET"])
    return app
