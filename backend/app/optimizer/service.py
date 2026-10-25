"""Analyse (Spur-Cache) und Vorschau für den MKV-Optimizer."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import session_scope
from ..models import Library, MediaFile, OptimizerAnalysis, OptimizerTrack, utcnow
from ..scanners.process import ResourceLimits, ScanControl
from ..services.events import bus
from ..services.library import inventory_library
from ..services.pathsec import PathSecurityError, resolve_media_file
from ..services.settings import load_settings
from .tracks import Analysis, Track, identify

log = logging.getLogger(__name__)

# Erhöhen, wenn die Analyse neue Angaben liefert – ältere Einträge werden dann beim Suchen neu erstellt
ANALYSIS_VERSION = 2


def dir_filter(q, rel_dir: str, recursive: bool):
    rel_dir = rel_dir.strip("/")
    if rel_dir:
        esc = rel_dir.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if recursive:
            q = q.where((MediaFile.directory == rel_dir) | MediaFile.directory.like(esc + "/%", escape="\\"))
        else:
            q = q.where(MediaFile.directory == rel_dir)
    elif not recursive:
        q = q.where(MediaFile.directory == "")
    return q


def mkv_files(db: Session, library_id: int, rel_dir: str, recursive: bool) -> list[MediaFile]:
    q = select(MediaFile).where(MediaFile.library_id == library_id, MediaFile.present.is_(True),
                                MediaFile.extension == "mkv")
    return list(db.scalars(dir_filter(q, rel_dir, recursive).order_by(MediaFile.rel_path)))


def store_analysis(db: Session, mf: MediaFile, a: Analysis) -> None:
    old = db.get(OptimizerAnalysis, mf.id)
    if old is not None:
        db.delete(old)
        db.flush()
    row = OptimizerAnalysis(file_id=mf.id, fingerprint_version=mf.fingerprint_version,
                            analysis_version=ANALYSIS_VERSION, ok=a.ok, error=a.error,
                            duration=a.duration, title=a.title, audio_count=len(a.audio),
                            subtitle_count=len(a.subtitles), analyzed_at=utcnow())
    db.add(row)
    db.flush()
    for t in a.tracks:
        db.add(OptimizerTrack(file_id=mf.id, **t.to_dict()))


def analysis_from_db(row: OptimizerAnalysis) -> Analysis:
    tracks = [Track(track_id=t.track_id, type=t.type, codec=t.codec, codec_id=t.codec_id, codec_family=t.codec_family,
                    language=t.language, language_ietf=t.language_ietf, name=t.name, is_default=t.is_default,
                    is_forced=t.is_forced, channels=t.channels, sample_rate=t.sample_rate,
                    size_bytes=t.size_bytes, size_source=t.size_source, bit_rate=t.bit_rate) for t in row.tracks]
    return Analysis(row.ok, row.error, row.duration, row.title, tracks)


def load_analyses(db: Session, files: list[MediaFile]) -> dict[int, Analysis]:
    """Nur Analysen, die zur aktuellen Dateiversion gehören."""
    if not files:
        return {}
    versions = {f.id: f.fingerprint_version for f in files}
    out: dict[int, Analysis] = {}
    ids = list(versions)
    for start in range(0, len(ids), 500):
        for row in db.scalars(select(OptimizerAnalysis).where(OptimizerAnalysis.file_id.in_(ids[start:start + 500]))):
            if row.fingerprint_version == versions[row.file_id] and row.analysis_version == ANALYSIS_VERSION:
                out[row.file_id] = analysis_from_db(row)
    return out


@dataclass
class AnalysisState:
    running: bool = False
    library_id: int | None = None
    directory: str = ""
    total: int = 0
    done: int = 0
    analyzed: int = 0
    errors: int = 0
    started: float | None = None
    finished: float | None = None
    message: str | None = None
    cancel: bool = False
    control: ScanControl | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {"running": self.running, "library_id": self.library_id, "directory": self.directory,
                "total": self.total, "done": self.done, "analyzed": self.analyzed, "errors": self.errors,
                "message": self.message,
                "elapsed": (self.finished or time.monotonic()) - self.started if self.started else None}


STATE = AnalysisState()


async def analyze(library_id: int, rel_dir: str, recursive: bool, force: bool = False) -> None:
    """Bestandsaufnahme der Bibliothek, danach mkvmerge -J für alle MKVs ohne aktuelle Analyse."""
    if STATE.running:
        raise RuntimeError("Es läuft bereits eine Analyse")
    STATE.__init__()  # type: ignore[misc]
    STATE.running, STATE.library_id, STATE.directory, STATE.started = True, library_id, rel_dir, time.monotonic()
    STATE.control = ScanControl()
    try:
        def prepare() -> list[tuple[int, str, str]]:
            with session_scope() as db:
                lib = db.get(Library, library_id)
                if lib is None:
                    raise ValueError("Bibliothek nicht gefunden")
                inventory_library(db, lib, load_settings(db))
                files = mkv_files(db, library_id, rel_dir, recursive)
                have = load_analyses(db, files) if not force else {}
                return [(f.id, f.path, lib.path) for f in files if f.id not in have]

        todo = await asyncio.to_thread(prepare)
        STATE.total = len(todo)
        STATE.message = f"{len(todo)} Dateien werden analysiert"
        limits = ResourceLimits(nice=5)
        last_pub = 0.0
        for fid, path, lib_path in todo:
            if STATE.cancel:
                STATE.message = "Analyse abgebrochen"
                break
            try:
                real_path = resolve_media_file(lib_path, path)
                a = await identify(real_path, STATE.control, limits)
            except PathSecurityError as exc:
                a = Analysis(False, str(exc), None, None, [])
            if not a.ok:
                STATE.errors += 1

            def save(a=a, fid=fid) -> None:
                with session_scope() as db:
                    mf = db.get(MediaFile, fid)
                    if mf is not None:
                        store_analysis(db, mf, a)

            await asyncio.to_thread(save)
            STATE.done += 1
            STATE.analyzed += 1
            if time.monotonic() - last_pub > 0.5:
                last_pub = time.monotonic()
                bus.publish("optimizer_analysis", STATE.to_dict())
        else:
            STATE.message = f"Analyse abgeschlossen: {STATE.analyzed} Dateien analysiert" + (
                f", {STATE.errors} nicht lesbar" if STATE.errors else "")
    except Exception as exc:  # pragma: no cover - Fehler sichtbar machen
        log.exception("Analyse fehlgeschlagen")
        STATE.message = f"Analyse fehlgeschlagen: {exc}"
    finally:
        STATE.running = False
        STATE.finished = time.monotonic()
        bus.publish("optimizer_analysis", STATE.to_dict())
