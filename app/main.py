"""Process entry point: ``uv run app`` serves the API (and the built frontend) and runs the sync scheduler."""

from __future__ import annotations

import argparse
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api.app import create_app
from app.api.cache import clear_cache
from app.config import Settings, load_settings
from app.db.session import init_db, make_engine, make_session_factory

log = logging.getLogger("app.main")


def build_app(settings: Settings) -> FastAPI:
    engine = make_engine(settings.db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if not settings.scheduler_enabled:
            log.info("scheduler disabled (YALE_BITES_SCHEDULER=0); serving existing data only")
            yield
            return
        from app.sync import build_runtime, create_scheduler

        runner = await build_runtime(settings, session_factory, on_complete=lambda _result: clear_cache())
        scheduler = create_scheduler(runner, settings)
        scheduler.start()
        log.info(
            "scheduler started: full sync every %d min (next %d days), hot sync every %d min (next %d h)",
            settings.full_sync_minutes, settings.full_sync_days, settings.hot_sync_minutes, settings.hot_sync_hours,
        )
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            await runner.aclose()

    return create_app(settings, session_factory, lifespan=lifespan)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="app", description="Yale Bites: API + live sync")
    parser.add_argument("--host", help="bind address (default YALE_BITES_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port (default YALE_BITES_PORT or 8000)")
    parser.add_argument("--no-sync", action="store_true", help="serve the existing database without syncing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if args.no_sync:
        from dataclasses import replace

        settings = replace(settings, scheduler_enabled=False)
    from app.fetch.redact import install_redaction

    install_redaction(settings)
    app = build_app(settings)
    uvicorn.run(app, host=args.host or settings.host, port=args.port or settings.port, log_config=None)


if __name__ == "__main__":
    main()
