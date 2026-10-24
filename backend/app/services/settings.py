"""Zur Laufzeit änderbare Einstellungen (persistent in SQLite)."""

from __future__ import annotations

import threading
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ..config import get_config
from ..models import Setting

DEFAULT_EXCLUDES = ["@eaDir", "#recycle", ".recycle", ".Trash-*", "*.part", "*.partial", "*.!qB", "*.crdownload", ".*"]
DEFAULT_EXTENSIONS = ["mkv", "mp4", "avi", "mov", "m4v", "ts"]


class AppSettings(BaseModel):
    max_concurrent_scans: int = Field(1, ge=1, le=32)
    ffmpeg_threads: int = Field(2, ge=0, le=64)
    nice_level: int = Field(10, ge=0, le=19)
    io_priority: Literal["idle", "best-effort-low", "best-effort", "normal"] = "best-effort-low"
    read_rate_limit: float = Field(0.0, ge=0.0, le=1000.0, description="FFmpeg -readrate (0 = unbegrenzt)")
    hwaccel: Literal["none", "auto", "vaapi", "qsv", "cuda", "vdpau", "drm", "vulkan"] = "none"
    strict_decode: bool = False
    pause_mode: Literal["immediate", "after_file"] = "immediate"
    scanner_paused: bool = False
    default_methods: list[Literal["quick", "container", "full"]] = ["quick", "container", "full"]
    container_ebml: bool = True
    container_mkvmerge: bool = True
    container_ffmpeg_demux: bool = True
    container_remux_check: bool = False
    ts_jump_warning_s: float = Field(3.0, ge=0.5, le=3600)
    ts_jump_error_s: float = Field(30.0, ge=1.0, le=36000)
    track_gap_warning_s: float = Field(5.0, ge=0.5, le=3600)
    duration_tolerance_s: float = Field(5.0, ge=0.0, le=3600)
    duration_tolerance_pct: float = Field(2.0, ge=0.0, le=100)
    hash_mode: Literal["none", "quick", "full"] = "quick"
    exclude_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    extensions: list[str] = Field(default_factory=lambda: list(DEFAULT_EXTENSIONS))
    follow_symlinks: bool = False
    auto_resume_after_restart: bool = True
    max_findings_per_result: int = Field(400, ge=10, le=10000)
    stall_timeout_s: int = Field(1800, ge=60, le=86400)
    keep_scan_logs: bool = True
    auth_enabled: bool = False
    auth_username: str | None = None
    auth_password_hash: str | None = None

    @field_validator("extensions")
    @classmethod
    def _ext(cls, v: list[str]) -> list[str]:
        out = []
        for e in v:
            e = e.strip().lower().lstrip(".")
            if not e or not e.isalnum() or len(e) > 8:
                raise ValueError(f"Ungültige Dateiendung: {e!r}")
            out.append(e)
        return sorted(set(out))

    @field_validator("exclude_patterns")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        out = []
        for p in v:
            p = p.strip()
            if not p:
                continue
            if len(p) > 512 or "\x00" in p:
                raise ValueError("Ungültiges Ausschlussmuster")
            out.append(p)
        return out


SECRET_KEYS = {"auth_password_hash"}

_lock = threading.Lock()
_cache: AppSettings | None = None


def _env_defaults() -> dict:
    cfg = get_config()
    io = cfg.io_class if cfg.io_class in ("idle", "best-effort-low", "best-effort", "normal") else "best-effort-low"
    return {
        "max_concurrent_scans": cfg.max_concurrent_scans,
        "ffmpeg_threads": cfg.ffmpeg_threads,
        "nice_level": cfg.nice_level,
        "io_priority": io,
    }


def load_settings(db: Session, refresh: bool = False) -> AppSettings:
    global _cache
    with _lock:
        if _cache is not None and not refresh:
            return _cache
        data = _env_defaults()
        for row in db.query(Setting).all():
            if row.key in AppSettings.model_fields:
                data[row.key] = row.value
        try:
            settings = AppSettings(**data)
        except Exception:
            # Ungültige gespeicherte Werte nicht zum Absturz führen lassen
            valid = {}
            for k, v in data.items():
                try:
                    AppSettings(**{k: v})
                    valid[k] = v
                except Exception:
                    pass
            settings = AppSettings(**valid)
        _cache = settings
        return settings


def update_settings(db: Session, changes: dict) -> AppSettings:
    global _cache
    current = load_settings(db, refresh=True)
    merged = current.model_dump() | changes
    validated = AppSettings(**merged)
    for key in changes:
        if key not in AppSettings.model_fields:
            continue
        value = getattr(validated, key)
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(key=key, value=value))
        else:
            row.value = value
    db.flush()
    with _lock:
        _cache = validated
    return validated


def invalidate_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def public_settings(s: AppSettings) -> dict:
    d = s.model_dump()
    for k in SECRET_KEYS:
        d.pop(k, None)
    d["auth_password_set"] = bool(s.auth_password_hash)
    return d
