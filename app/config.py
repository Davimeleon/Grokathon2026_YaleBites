"""Runtime configuration, read once from the environment.

Credentials (``YALECONNECT_COOKIE``, ``YALECONNECT_ICS_FEEDS``) are wrapped in ``Secret`` so
that ``repr``/``str``/logging/pydantic serialisation of a Settings object can never print
them. Code that genuinely needs the value calls ``secret.reveal()``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VAR_DIR = REPO_ROOT / "var"
DATA_DIR = REPO_ROOT / "data"
FIXTURES_DIR = REPO_ROOT / "tests" / "data" / "fixtures"
WEB_DIST_DIR = REPO_ROOT / "web" / "dist"

DEFAULT_CONTACT = "operator@example.invalid"


class Secret:
    """A string that refuses to be printed. Use ``reveal()`` at the single point of use."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret('**********')" if self._value else "Secret('')"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("Secret", self._value))

    def __reduce__(self):
        raise TypeError("Secret values cannot be pickled")


def _env_bool(env: dict[str, str] | os._Environ, name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    var_dir: Path = DEFAULT_VAR_DIR
    db_path: Path = DEFAULT_VAR_DIR / "yale_bites.sqlite3"
    cache_dir: Path = DEFAULT_VAR_DIR / "cache"
    geocode_cache_path: Path = DEFAULT_VAR_DIR / "nominatim_cache.json"
    food_lexicon_path: Path = DATA_DIR / "food_lexicon.yaml"
    buildings_path: Path = DATA_DIR / "buildings.yaml"
    web_dist_dir: Path = WEB_DIST_DIR

    # Authenticated mode inputs. Never logged, never written to disk, never returned by the API.
    yaleconnect_cookie: Secret = field(default_factory=lambda: Secret(""))
    yaleconnect_ics_feeds: tuple[Secret, ...] = ()

    contact: str = DEFAULT_CONTACT
    scheduler_enabled: bool = True
    online_geocoding: bool = True
    host: str = "127.0.0.1"
    port: int = 8000

    full_sync_minutes: int = 30
    hot_sync_minutes: int = 3
    full_sync_days: int = 14
    hot_sync_hours: int = 6

    @property
    def user_agent(self) -> str:
        return f"yale-bites/0.1 (+Yale Bites; contact: {self.contact})"

    @property
    def auth_configured(self) -> bool:
        return bool(self.yaleconnect_cookie) or bool(self.yaleconnect_ics_feeds)

    @property
    def public_cache_dir(self) -> Path:
        return self.cache_dir / "public"

    @property
    def auth_cache_dir(self) -> Path:
        return self.cache_dir / "authenticated"


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    var_dir = Path(env.get("YALE_BITES_VAR_DIR") or DEFAULT_VAR_DIR)
    feeds = tuple(
        Secret(part.strip()) for part in (env.get("YALECONNECT_ICS_FEEDS") or "").split(",") if part.strip()
    )
    return Settings(
        var_dir=var_dir,
        db_path=Path(env.get("YALE_BITES_DB") or var_dir / "yale_bites.sqlite3"),
        cache_dir=var_dir / "cache",
        geocode_cache_path=var_dir / "nominatim_cache.json",
        yaleconnect_cookie=Secret((env.get("YALECONNECT_COOKIE") or "").strip()),
        yaleconnect_ics_feeds=feeds,
        contact=(env.get("YALE_BITES_CONTACT") or DEFAULT_CONTACT).strip(),
        scheduler_enabled=_env_bool(env, "YALE_BITES_SCHEDULER", True),
        online_geocoding=_env_bool(env, "YALE_BITES_ONLINE_GEOCODING", True),
        # Cloud hosts (Render, etc.) set PORT and expect 0.0.0.0; local defaults stay loopback.
        host=env.get("YALE_BITES_HOST") or ("0.0.0.0" if env.get("PORT") else "127.0.0.1"),
        port=int(env.get("YALE_BITES_PORT") or env.get("PORT") or 8000),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
