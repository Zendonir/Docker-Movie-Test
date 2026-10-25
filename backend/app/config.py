"""Laufzeitkonfiguration aus Umgebungsvariablen.

Alle Werte haben sichere Standardwerte. Einstellungen, die zur Laufzeit über die
Weboberfläche geändert werden können, liegen zusätzlich in der Tabelle
``settings`` (siehe ``services/settings.py``); die Umgebungsvariablen liefern
dort nur die Startwerte.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "ja", "on"}


def _int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    try:
        value = int(raw) if raw not in (None, "") else default
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


@dataclass(frozen=True)
class AppConfig:
    config_dir: Path
    media_roots: tuple[Path, ...]
    static_dir: Path
    host: str
    port: int
    max_concurrent_scans: int
    ffmpeg_threads: int
    nice_level: int
    io_class: str
    auto_create_library: bool
    auth_enabled: bool
    auth_username: str | None
    auth_password: str | None
    repair_enabled: bool
    tmp_dir: Path
    optimizer_output_dir: Path
    log_level: str
    tz: str
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.config_dir / "media-integrity.db"

    @property
    def log_dir(self) -> Path:
        return self.config_dir / "logs"

    @property
    def scan_log_dir(self) -> Path:
        return self.config_dir / "logs" / "scans"

    @property
    def repair_dir(self) -> Path:
        return self.config_dir / "repaired"


def load_config() -> AppConfig:
    config_dir = Path(os.environ.get("CONFIG_DIR", "/config"))
    roots_raw = os.environ.get("MEDIA_ROOTS") or os.environ.get("MEDIA_ROOT") or "/media"
    media_roots = tuple(Path(p) for p in roots_raw.split(os.pathsep) if p.strip())
    default_static = Path(__file__).resolve().parent.parent / "static"
    return AppConfig(
        config_dir=config_dir,
        media_roots=media_roots,
        static_dir=Path(os.environ.get("STATIC_DIR", str(default_static))),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=_int("PORT", 8080, 1, 65535),
        max_concurrent_scans=_int("MAX_CONCURRENT_SCANS", 1, 1, 32),
        ffmpeg_threads=_int("FFMPEG_THREADS", 2, 0, 64),
        nice_level=_int("NICE_LEVEL", 10, 0, 19),
        io_class=os.environ.get("IO_PRIORITY", "best-effort-low"),
        auto_create_library=_bool("AUTO_CREATE_LIBRARY", True),
        auth_enabled=_bool("AUTH_ENABLED", False),
        auth_username=os.environ.get("AUTH_USERNAME") or None,
        auth_password=os.environ.get("AUTH_PASSWORD") or None,
        repair_enabled=_bool("REPAIR_ENABLED", False),
        tmp_dir=Path(os.environ.get("SCAN_TMP_DIR", str(config_dir / "tmp"))),
        optimizer_output_dir=Path(os.environ.get("OPTIMIZER_OUTPUT_DIR", "/output")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        tz=os.environ.get("TZ", "Europe/Berlin"),
    )


_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """Nur für Tests: Konfiguration neu aus der Umgebung laden."""
    global _config
    _config = None
