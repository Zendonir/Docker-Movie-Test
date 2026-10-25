from __future__ import annotations

import gzip
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import asc, desc, func, select
from sqlalchemy.orm import Session

from ..config import get_config
from ..database import get_db
from ..models import DetectedError, Library, MediaFile, MediaStream, ScanResult
from ..services.repairability import assess
from ..services import repair
from ..services.labels import CHECK_LEVEL_LABELS, METHOD_LABELS, OUTCOME_LABELS, STATUS_LABELS, error_type_label
from ..services.pathsec import is_within
from ..services.filters import apply_filters
from ..services.status import current_results
from .schemas import RepairIn

router = APIRouter(prefix="/api/files", tags=["Dateien"])

SORT_COLUMNS = {
    "status": MediaFile.status,
    "name": MediaFile.name_lower,
    "filename": MediaFile.filename,
    "size": MediaFile.size,
    "duration": MediaFile.duration,
    "video": MediaFile.video_codec,
    "last_checked": MediaFile.last_checked_at,
    "errors": MediaFile.error_count,
    "directory": MediaFile.directory,
    "first_seen": MediaFile.first_seen_at,
}
STATUS_ORDER = {"critical": 5, "error": 4, "warning": 3, "incomplete": 2, "unchecked": 1, "ok": 0}


def file_summary(f: MediaFile, libs: dict[int, str] | None = None) -> dict:
    return {
        "id": f.id,
        "library_id": f.library_id,
        "library": (libs or {}).get(f.library_id),
        "filename": f.filename,
        "rel_path": f.rel_path,
        "directory": f.directory,
        "path": f.path,
        "extension": f.extension,
        "size": f.size,
        "duration": f.duration,
        "container_format": f.container_format,
        "video_codec": f.video_codec,
        "video_resolution": f.video_resolution,
        "audio_codecs": f.audio_codecs,
        "status": f.status,
        "status_label": STATUS_LABELS.get(f.status, f.status),
        "check_level": f.check_level,
        "check_level_label": CHECK_LEVEL_LABELS.get(f.check_level, f.check_level),
        "methods_done": f.methods_done or [],
        "error_count": f.error_count,
        "warning_count": f.warning_count,
        "last_checked_at": f.last_checked_at,
        "first_seen_at": f.first_seen_at,
        "present": f.present,
        "missing_since": f.missing_since,
        "changed_at": f.changed_at,
        "moved_from": f.moved_from,
        "status_note": f.status_note,
    }


@router.get("")
def list_files(
    db: Session = Depends(get_db),
    q: str | None = Query(None, max_length=300),
    status: list[str] | None = Query(None),
    ext: list[str] | None = Query(None),
    library_id: int | None = None,
    directory: str | None = Query(None, max_length=4096),
    only_new: bool = False,
    only_damaged: bool = False,
    present: str = Query("yes", pattern="^(yes|no|all)$"),
    check_level: list[str] | None = Query(None),
    sort: str = Query("name"),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    base = apply_filters(select(MediaFile), search=q, status=status, ext=ext, library_id=library_id,
                         directory=directory, only_new=only_new, only_damaged=only_damaged, present=present,
                         check_level=check_level)
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    col = SORT_COLUMNS.get(sort, MediaFile.name_lower)
    direction = desc if order == "desc" else asc
    if sort == "status":
        from sqlalchemy import case
        col = case(*[(MediaFile.status == k, v) for k, v in STATUS_ORDER.items()], else_=0)
    rows = db.scalars(base.order_by(direction(col).nulls_last() if order == "desc" else direction(col),
                                    MediaFile.id).offset((page - 1) * page_size).limit(page_size)).all()
    libs = {lib.id: lib.name for lib in db.scalars(select(Library))}
    return {"total": total, "page": page, "page_size": page_size, "items": [file_summary(f, libs) for f in rows]}


@router.post("/ids")
def list_file_ids(
    db: Session = Depends(get_db),
    q: str | None = Query(None, max_length=300),
    status: list[str] | None = Query(None),
    ext: list[str] | None = Query(None),
    library_id: int | None = None,
    directory: str | None = Query(None, max_length=4096),
    only_new: bool = False,
    only_damaged: bool = False,
    present: str = Query("yes", pattern="^(yes|no|all)$"),
    check_level: list[str] | None = Query(None),
):
    """Alle IDs, die zum Filter passen (für "Alle Treffer auswählen")."""
    base = apply_filters(select(MediaFile.id), search=q, status=status, ext=ext, library_id=library_id,
                         directory=directory, only_new=only_new, only_damaged=only_damaged, present=present,
                         check_level=check_level)
    ids = db.scalars(base.limit(100000)).all()
    return {"ids": ids, "count": len(ids)}


@router.get("/facets")
def facets(db: Session = Depends(get_db), library_id: int | None = None):
    q_ext = select(MediaFile.extension, func.count()).where(MediaFile.present.is_(True))
    q_dir = select(MediaFile.directory, func.count()).where(MediaFile.present.is_(True))
    if library_id is not None:
        q_ext = q_ext.where(MediaFile.library_id == library_id)
        q_dir = q_dir.where(MediaFile.library_id == library_id)
    exts = db.execute(q_ext.group_by(MediaFile.extension).order_by(MediaFile.extension)).all()
    dirs = db.execute(q_dir.group_by(MediaFile.directory).order_by(MediaFile.directory).limit(5000)).all()
    top: dict[str, int] = {}
    for d, c in dirs:
        first = (d or "").split("/")[0]
        top[first] = top.get(first, 0) + c
    return {
        "extensions": [{"value": e, "count": c} for e, c in exts],
        "directories": [{"value": d, "count": c} for d, c in sorted(top.items()) if d],
        "statuses": [{"value": k, "label": v} for k, v in STATUS_LABELS.items()],
    }


def _error_dict(e: DetectedError) -> dict:
    return {
        "id": e.id, "result_id": e.result_id, "method": e.method, "method_label": METHOD_LABELS.get(e.method, e.method),
        "error_type": e.error_type, "error_type_label": error_type_label(e.error_type), "severity": e.severity,
        "heuristic": e.heuristic, "stream_index": e.stream_index, "stream_type": e.stream_type,
        "description": e.description, "time_start": e.time_start, "time_end": e.time_end,
        "time_precision": e.time_precision, "time_source": e.time_source, "byte_offset": e.byte_offset,
        "byte_end": e.byte_end, "tool": e.tool, "raw_message": e.raw_message, "occurrences": e.occurrences,
        "extra": e.extra or {},
    }


def _result_dict(r: ScanResult) -> dict:
    return {
        "id": r.id, "method": r.method, "method_label": METHOD_LABELS.get(r.method, r.method),
        "scanner_version": r.scanner_version, "outcome": r.outcome,
        "outcome_label": OUTCOME_LABELS.get(r.outcome, r.outcome), "status": r.status,
        "status_label": STATUS_LABELS.get(r.status, r.status), "is_current": r.is_current, "job_id": r.job_id,
        "started_at": r.started_at, "finished_at": r.finished_at, "duration_s": r.duration_s,
        "critical_count": r.critical_count, "error_count": r.error_count, "warning_count": r.warning_count,
        "summary": r.summary, "tools": r.tools, "details": r.details, "decoded_duration": r.decoded_duration,
        "container_duration": r.container_duration, "has_log": bool(r.log_path), "raw_message_count": r.raw_message_count,
        "fingerprint_version": r.fingerprint_version,
    }


def _get_file(db: Session, file_id: int) -> MediaFile:
    f = db.get(MediaFile, file_id)
    if f is None:
        raise HTTPException(404, "Datei nicht gefunden")
    return f


@router.get("/{file_id}")
def file_detail(file_id: int, db: Session = Depends(get_db)):
    f = _get_file(db, file_id)
    libs = {lib.id: lib.name for lib in db.scalars(select(Library))}
    streams = db.scalars(select(MediaStream).where(MediaStream.file_id == f.id).order_by(MediaStream.index)).all()
    current = current_results(db, f)
    errors = []
    if current:
        errors = db.scalars(select(DetectedError).where(DetectedError.result_id.in_([r.id for r in current.values()]))
                            .order_by(DetectedError.time_start.is_(None), DetectedError.time_start, DetectedError.id)).all()
    history = db.scalars(select(ScanResult).where(ScanResult.file_id == f.id).order_by(ScanResult.id.desc()).limit(100)).all()
    err_dicts = [_error_dict(e) for e in errors]
    return {
        **file_summary(f, libs),
        "quick_hash": f.quick_hash,
        "full_hash": f.full_hash,
        "fingerprint_version": f.fingerprint_version,
        "bit_rate": f.bit_rate,
        "streams": [{
            "index": s.index, "type": s.type, "codec_name": s.codec_name, "codec_long_name": s.codec_long_name,
            "profile": s.profile, "width": s.width, "height": s.height, "fps": s.fps, "pix_fmt": s.pix_fmt,
            "bit_depth": s.bit_depth, "channels": s.channels, "channel_layout": s.channel_layout,
            "sample_rate": s.sample_rate, "language": s.language, "title": s.title, "bit_rate": s.bit_rate,
            "duration": s.duration, "is_default": s.is_default, "forced": s.forced, "attached_pic": s.attached_pic,
            "decodable": s.decodable,
        } for s in streams],
        "current_results": {m: _result_dict(r) for m, r in current.items()},
        "errors": err_dicts,
        "assessment": assess(f.status, err_dicts),
        "history": [_result_dict(r) for r in history],
        "repair_enabled": repair.repair_enabled(),
        "repair_actions": repair.ACTIONS if repair.repair_enabled() else {},
    }


@router.get("/{file_id}/results/{result_id}/errors")
def result_errors(file_id: int, result_id: int, db: Session = Depends(get_db)):
    r = db.get(ScanResult, result_id)
    if r is None or r.file_id != file_id:
        raise HTTPException(404, "Ergebnis nicht gefunden")
    errs = db.scalars(select(DetectedError).where(DetectedError.result_id == r.id)
                      .order_by(DetectedError.time_start.is_(None), DetectedError.time_start, DetectedError.id)).all()
    return {"result": _result_dict(r), "errors": [_error_dict(e) for e in errs]}


@router.get("/{file_id}/results/{result_id}/log", response_class=PlainTextResponse)
def result_log(file_id: int, result_id: int, db: Session = Depends(get_db), max_lines: int = Query(5000, le=200000)):
    """Vollständiges FFmpeg-Protokoll einer Decodierprüfung (nur Prüfprotokolle, keine Mediendaten)."""
    r = db.get(ScanResult, result_id)
    if r is None or r.file_id != file_id or not r.log_path:
        raise HTTPException(404, "Kein Protokoll vorhanden")
    log_root = str(get_config().scan_log_dir)
    if not is_within(r.log_path, log_root) or not os.path.exists(r.log_path):
        raise HTTPException(404, "Protokoll nicht mehr vorhanden")
    lines = []
    with gzip.open(r.log_path, "rt", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= max_lines:
                lines.append(f"... (gekürzt nach {max_lines} Zeilen)\n")
                break
            lines.append(line)
    return "".join(lines) or "(keine Meldungen – FFmpeg hat keine Warnungen oder Fehler ausgegeben)\n"


@router.post("/{file_id}/repair")
async def start_repair(file_id: int, body: RepairIn, db: Session = Depends(get_db)):
    f = _get_file(db, file_id)
    try:
        task = await repair.start_repair(f.id, f.path, f.filename, body.action, body.confirm, body.confirm_filename)
    except repair.RepairDisabled as exc:
        raise HTTPException(403, str(exc)) from exc
    except repair.RepairError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"state": task.state, "target": task.target}


@router.get("/{file_id}/repair")
def repair_status(file_id: int):
    t = repair.TASKS.get(file_id)
    if t is None:
        return {"state": None, "enabled": repair.repair_enabled()}
    return {"state": t.state, "target": t.target, "message": t.message, "started": t.started, "finished": t.finished,
            "enabled": repair.repair_enabled()}
