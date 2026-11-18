"""Frozen SQLAlchemy schema (SQLite, single file). Owned by the integrator.

All datetimes are stored as UTC ISO-8601 text through ``UTCDateTime`` and come back
tz-aware (UTC). Conversion to America/New_York happens at the edges (parse output, API).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator):
    """Tz-aware datetime persisted as naive UTC; refuses naive input."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to UTCDateTime column")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    # --- normalized fields (app.schemas.NormalizedEvent) ---
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_url: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description_raw: Mapped[str | None] = mapped_column(Text)
    description_text: Mapped[str | None] = mapped_column(Text)
    host_group: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    location_raw: Mapped[str | None] = mapped_column(Text)
    cost_raw: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="public")  # public|authenticated
    updated_at_source: Mapped[datetime | None] = mapped_column(UTCDateTime)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # --- side channel (app.schemas.EventExtras) ---
    cg_event_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    graphql_id: Mapped[str | None] = mapped_column(String(64))
    ics_uid: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="graphql")
    is_food_included: Mapped[bool | None] = mapped_column(Boolean)
    food_details: Mapped[str | None] = mapped_column(Text)
    location_privacy: Mapped[str | None] = mapped_column(String(32))
    location_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    location_kind: Mapped[str | None] = mapped_column(String(32))
    location_address: Mapped[str | None] = mapped_column(Text)
    attendance_mode: Mapped[str | None] = mapped_column(String(16))
    all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_multi_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    event_status: Mapped[str | None] = mapped_column(String(32))
    event_type: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    cohost_groups: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    parent_title: Mapped[str | None] = mapped_column(Text)

    # --- enrichment (recomputed only when content_hash changes) ---
    food_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    food_label: Mapped[str] = mapped_column(String(8), nullable=False, default="no")  # yes|likely|no
    food_evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [EvidenceSpan dict]
    is_free_to_attend: Mapped[bool | None] = mapped_column(Boolean)
    free_evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [str]
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    building_id: Mapped[str | None] = mapped_column(String(64))
    building_name: Mapped[str | None] = mapped_column(Text)
    geocode_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    geocode_method: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    geocode_score: Mapped[float | None] = mapped_column(Float)

    # --- lifecycle ---
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")  # active|canceled_or_removed
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)  # last *content* change
    last_seen_sync_id: Mapped[int | None] = mapped_column(ForeignKey("sync_runs.id"))
    # Consecutive complete syncs (of mode `missing_mode`) that covered this event's time but did not return it.
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_mode: Mapped[str | None] = mapped_column(String(16))  # anonymous|authenticated
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        Index("ix_events_starts_at", "starts_at"),
        Index("ix_events_status_starts", "status", "starts_at"),
        Index("ix_events_food_label", "food_label"),
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)  # full|hot|seed
    mode: Mapped[str] = mapped_column(String(16), nullable=False)  # anonymous|authenticated
    auth_status: Mapped[str] = mapped_column(String(16), nullable=False)  # none|valid|expired
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    window_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    events_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    events_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    events_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    events_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    events_removed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    authenticated_only_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parse_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_unchanged_responses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)  # [str], already redacted
    source_stats: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)  # per-source counters

    __table_args__ = (Index("ix_sync_runs_kind_started", "kind", "started_at"),)


class HttpValidator(Base):
    """Conditional-request state per request key (URL + body; tokens redacted before hashing)."""

    __tablename__ = "http_validators"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of redacted method+url+body
    url_redacted: Mapped[str] = mapped_column(Text, nullable=False)
    authenticated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    etag: Mapped[str | None] = mapped_column(String(255))
    last_modified: Mapped[str | None] = mapped_column(String(64))
    body_sha256: Mapped[str | None] = mapped_column(String(64))  # for sources without validators (GraphQL)
    # source_urls contained in the last successfully parsed body, so an unchanged body (304 or identical
    # GraphQL page) can mark its events as seen without parsing anything.
    item_keys: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status_code: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    cache_path: Mapped[str | None] = mapped_column(Text)


class AppState(Base):
    """Small key/value store: dataset kind, fixture time shift, last ICS Last-Modified, ..."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
