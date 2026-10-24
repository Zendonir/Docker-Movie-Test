from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import DetectedError, Library, MediaFile, ScanJob, ScanResult, utcnow
from ..services.labels import error_type_label
from ..services.library import storage_info
from ..services.settings import load_settings

router = APIRouter(prefix="/api/stats", tags=["Statistik"])

FULL_LEVELS = ("full", "container_full")


@router.get("")
def stats(db: Session = Depends(get_db)):
    present = MediaFile.present.is_(True)
    row = db.execute(select(
        func.count(),
        func.coalesce(func.sum(MediaFile.size), 0),
        func.sum(case((MediaFile.check_level.in_(FULL_LEVELS), 1), else_=0)),
        func.coalesce(func.sum(case((MediaFile.check_level.in_(FULL_LEVELS), MediaFile.size), else_=0)), 0),
        func.sum(case((MediaFile.check_level != "none", 1), else_=0)),
        func.coalesce(func.sum(case((MediaFile.check_level != "none", MediaFile.size), else_=0)), 0),
        func.sum(case((MediaFile.check_level == "none", 1), else_=0)),
        func.sum(case(((MediaFile.status == "ok") & MediaFile.check_level.in_(FULL_LEVELS), 1), else_=0)),
    ).where(present)).one()
    status_counts = dict(db.execute(select(MediaFile.status, func.count()).where(present)
                                    .group_by(MediaFile.status)).all())
    missing = db.scalar(select(func.count()).select_from(MediaFile).where(MediaFile.present.is_(False)))
    active_jobs = db.scalar(select(func.count()).select_from(ScanJob).where(
        ScanJob.state.in_(("queued", "running", "paused"))))
    storage = []
    for lib in db.scalars(select(Library).order_by(Library.name)):
        storage.append({"library_id": lib.id, "name": lib.name, "path": lib.path, "storage": storage_info(lib.path)})
    return {
        "total_files": row[0] or 0,
        "total_size": int(row[1] or 0),
        "fully_checked": int(row[2] or 0),
        "fully_checked_bytes": int(row[3] or 0),
        "checked_files": int(row[4] or 0),
        "checked_bytes": int(row[5] or 0),
        "unchecked": int(row[6] or 0),
        "ok_full": int(row[7] or 0),
        "status": {k: status_counts.get(k, 0) for k in ("critical", "error", "warning", "ok", "incomplete", "unchecked")},
        "missing": missing or 0,
        "active_jobs": active_jobs or 0,
        "scanner_paused": load_settings(db).scanner_paused,
        "storage": storage,
    }


@router.get("/history")
def history(db: Session = Depends(get_db), days: int = Query(30, ge=1, le=730)):
    since = utcnow() - timedelta(days=days)
    day = func.date(ScanResult.finished_at)
    rows = db.execute(
        select(day, ScanResult.status, func.count(func.distinct(ScanResult.file_id)), func.sum(ScanResult.file_size))
        .where(ScanResult.finished_at >= since, ScanResult.method != "quick")
        .group_by(day, ScanResult.status).order_by(day)
    ).all()
    out: dict[str, dict] = {}
    for d, status, n, size in rows:
        e = out.setdefault(str(d), {"date": str(d), "ok": 0, "warning": 0, "error": 0, "critical": 0,
                                    "incomplete": 0, "bytes": 0})
        e[status] = e.get(status, 0) + n
        e["bytes"] += int(size or 0)
    return list(out.values())


@router.get("/error-types")
def error_types(db: Session = Depends(get_db)):
    rows = db.execute(
        select(DetectedError.error_type, DetectedError.severity, func.count(), func.count(func.distinct(DetectedError.file_id)))
        .join(ScanResult, ScanResult.id == DetectedError.result_id)
        .join(MediaFile, MediaFile.id == DetectedError.file_id)
        .where(ScanResult.is_current.is_(True), MediaFile.present.is_(True),
               ScanResult.fingerprint_version == MediaFile.fingerprint_version,
               DetectedError.severity.in_(("critical", "error", "warning")))
        .group_by(DetectedError.error_type, DetectedError.severity)
    ).all()
    agg: dict[str, dict] = {}
    for t, sev, n, files in rows:
        e = agg.setdefault(t, {"error_type": t, "label": error_type_label(t), "critical": 0, "error": 0,
                               "warning": 0, "files": 0, "total": 0})
        e[sev] += n
        e["total"] += n
        e["files"] = max(e["files"], files)
    return sorted(agg.values(), key=lambda x: -x["total"])
