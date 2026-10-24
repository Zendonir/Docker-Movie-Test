"""Datenbankmodell (SQLAlchemy 2.0).

Änderungen am Schema immer zusätzlich als Alembic-Migration unter
``app/database/migrations/versions`` anlegen.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Speichert Zeitpunkte als UTC (SQLite kennt keine Zeitzonen) und liefert sie zeitzonenbehaftet zurück."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


# Dateistatus (aggregiert über die aktuellen Ergebnisse aller Methoden)
FILE_STATUSES = ("unchecked", "ok", "warning", "error", "critical", "incomplete")
# Prüftiefe: welche Methoden für die aktuelle Dateiversion vollständig abgeschlossen sind
CHECK_LEVELS = ("none", "quick", "container", "full", "container_full")

JOB_ACTIVE_STATES = ("queued", "running", "paused")
JOB_STATES = ("queued", "running", "paused", "completed", "cancelled", "failed", "interrupted")
ITEM_STATES = ("pending", "running", "done", "skipped", "failed", "cancelled")


class Library(Base):
    __tablename__ = "libraries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(String(4096), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    exclude_patterns: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_inventory_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_inventory_stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    files: Mapped[list["MediaFile"]] = relationship(back_populates="library", cascade="all, delete-orphan",
                                                    passive_deletes=True)


class MediaFile(Base):
    __tablename__ = "media_files"
    __table_args__ = (
        UniqueConstraint("library_id", "rel_path", name="uq_media_files_library_rel_path"),
        Index("ix_media_files_status", "status"),
        Index("ix_media_files_present_status", "present", "status"),
        Index("ix_media_files_extension", "extension"),
        Index("ix_media_files_directory", "library_id", "directory"),
        Index("ix_media_files_name_lower", "name_lower"),
        Index("ix_media_files_last_checked", "last_checked_at"),
        Index("ix_media_files_size_mtime", "size", "mtime_ns"),
        Index("ix_media_files_check_level", "check_level"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    rel_path: Mapped[str] = mapped_column(String(4096))
    path: Mapped[str] = mapped_column(String(4096))
    filename: Mapped[str] = mapped_column(String(1024))
    name_lower: Mapped[str] = mapped_column(String(4096))
    directory: Mapped[str] = mapped_column(String(4096), default="")
    extension: Mapped[str] = mapped_column(String(16))
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    inode: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quick_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fingerprint_version: Mapped[int] = mapped_column(Integer, default=1)
    present: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    missing_since: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    moved_from: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    # Medieninformationen (aus FFprobe)
    container_format: Mapped[str | None] = mapped_column(String(200), nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    video_resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    audio_codecs: Mapped[str | None] = mapped_column(String(256), nullable=True)
    bit_rate: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    probed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    # Aggregierter Prüfstatus der aktuellen Dateiversion
    status: Mapped[str] = mapped_column(String(16), default="unchecked")
    check_level: Mapped[str] = mapped_column(String(16), default="none")
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    methods_done: Mapped[list] = mapped_column(JSON, default=list)
    status_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    library: Mapped[Library] = relationship(back_populates="files")
    streams: Mapped[list["MediaStream"]] = relationship(back_populates="file", cascade="all, delete-orphan",
                                                        order_by="MediaStream.index", passive_deletes=True)


class MediaStream(Base):
    __tablename__ = "media_streams"
    __table_args__ = (Index("ix_media_streams_file", "file_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("media_files.id", ondelete="CASCADE"))
    index: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(16))
    codec_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    codec_long_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    profile: Mapped[str | None] = mapped_column(String(128), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    pix_fmt: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bit_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel_layout: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bit_rate: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    forced: Mapped[bool] = mapped_column(Boolean, default=False)
    attached_pic: Mapped[bool] = mapped_column(Boolean, default=False)
    decodable: Mapped[bool] = mapped_column(Boolean, default=False)

    file: Mapped[MediaFile] = relationship(back_populates="streams")


class ScanSchedule(Base):
    __tablename__ = "scan_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    kind: Mapped[str] = mapped_column(String(16))  # daily, weekly, monthly, custom
    cron: Mapped[str] = mapped_column(String(128))
    task: Mapped[str] = mapped_column(String(32))  # new, changed, new_changed, full, errors
    methods: Mapped[list] = mapped_column(JSON, default=list)
    library_ids: Mapped[list] = mapped_column(JSON, default=list)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScanJob(Base):
    __tablename__ = "scan_jobs"
    __table_args__ = (
        Index("ix_scan_jobs_state_priority", "state", "priority"),
        # Verhindert doppelte aktive Aufträge (z. B. nach Neustart oder Doppelklick)
        Index("uq_scan_jobs_active_dedupe", "dedupe_key", unique=True,
              sqlite_where=text("state IN ('queued','running','paused')")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    task: Mapped[str] = mapped_column(String(32))  # incremental, new_changed, full, errors, files
    methods: Mapped[list] = mapped_column(JSON, default=list)
    library_ids: Mapped[list] = mapped_column(JSON, default=list)
    file_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    force: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(16), default="queued")
    phase: Mapped[str] = mapped_column(String(16), default="prepare")  # prepare, scan, done
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    dedupe_key: Mapped[str] = mapped_column(String(128))
    trigger: Mapped[str] = mapped_column(String(32), default="manual")  # manual, schedule, api
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("scan_schedules.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    done_files: Mapped[int] = mapped_column(Integer, default=0)
    skipped_files: Mapped[int] = mapped_column(Integer, default=0)
    failed_files: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    done_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    files_with_errors: Mapped[int] = mapped_column(Integer, default=0)
    files_with_warnings: Mapped[int] = mapped_column(Integer, default=0)
    findings_count: Mapped[int] = mapped_column(Integer, default=0)
    resume_count: Mapped[int] = mapped_column(Integer, default=0)
    active_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScanJobItem(Base):
    __tablename__ = "scan_job_items"
    __table_args__ = (
        Index("ix_scan_job_items_job_state", "job_id", "state"),
        Index("ix_scan_job_items_file", "file_id"),
        UniqueConstraint("job_id", "file_id", name="uq_scan_job_items_job_file"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("scan_jobs.id", ondelete="CASCADE"))
    file_id: Mapped[int] = mapped_column(ForeignKey("media_files.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    methods: Mapped[list] = mapped_column(JSON, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScanResult(Base):
    __tablename__ = "scan_results"
    __table_args__ = (
        Index("ix_scan_results_file_method_current", "file_id", "method", "is_current"),
        Index("ix_scan_results_job", "job_id"),
        Index("ix_scan_results_finished", "finished_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("media_files.id", ondelete="CASCADE"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("scan_jobs.id", ondelete="SET NULL"), nullable=True)
    method: Mapped[str] = mapped_column(String(16))
    scanner_version: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    fingerprint_version: Mapped[int] = mapped_column(Integer)
    file_size: Mapped[int] = mapped_column(BigInteger)
    file_mtime_ns: Mapped[int] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    critical_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    tools: Mapped[dict] = mapped_column(JSON, default=dict)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    decoded_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    container_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    log_path: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    raw_message_count: Mapped[int] = mapped_column(Integer, default=0)

    errors: Mapped[list["DetectedError"]] = relationship(back_populates="result", cascade="all, delete-orphan",
                                                         order_by="DetectedError.id")


class DetectedError(Base):
    __tablename__ = "detected_errors"
    __table_args__ = (
        Index("ix_detected_errors_result", "result_id"),
        Index("ix_detected_errors_file", "file_id"),
        Index("ix_detected_errors_type", "error_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    result_id: Mapped[int] = mapped_column(ForeignKey("scan_results.id", ondelete="CASCADE"))
    file_id: Mapped[int] = mapped_column(ForeignKey("media_files.id", ondelete="CASCADE"))
    method: Mapped[str] = mapped_column(String(16))
    error_type: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(16))
    heuristic: Mapped[bool] = mapped_column(Boolean, default=False)
    stream_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stream_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str] = mapped_column(Text)
    time_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_end: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_precision: Mapped[str] = mapped_column(String(16), default="unknown")
    time_source: Mapped[str | None] = mapped_column(String(256), nullable=True)
    byte_offset: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    byte_end: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tool: Mapped[str] = mapped_column(String(64))
    raw_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    result: Mapped[ScanResult] = relationship(back_populates="errors")


class JobEvent(Base):
    """Nachvollziehbares Protokoll eines Scanauftrags (Start, Pause, Neustart, Fehler …)."""

    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_job", "job_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("scan_jobs.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
