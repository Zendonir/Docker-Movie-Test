"""Aggregation des Dateistatus aus den aktuellen Prüfergebnissen."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DetectedError, MediaFile, ScanResult

STATUS_RANK = {"unchecked": 0, "ok": 1, "incomplete": 2, "warning": 3, "error": 4, "critical": 5}
METHOD_ORDER = ("quick", "container", "full")


def current_results(db: Session, mf: MediaFile) -> dict[str, ScanResult]:
    rows = db.scalars(
        select(ScanResult)
        .where(ScanResult.file_id == mf.id, ScanResult.is_current.is_(True),
               ScanResult.fingerprint_version == mf.fingerprint_version)
        .order_by(ScanResult.finished_at.desc(), ScanResult.id.desc())
    ).all()
    out: dict[str, ScanResult] = {}
    for r in rows:
        out.setdefault(r.method, r)
    return out


def check_level_for(done: set[str]) -> str:
    if "full" in done and "container" in done:
        return "container_full"
    if "full" in done:
        return "full"
    if "container" in done:
        return "container"
    if "quick" in done:
        return "quick"
    return "none"


def recompute_file_status(db: Session, mf: MediaFile) -> None:
    results = current_results(db, mf)
    if not results:
        mf.status = "unchecked"
        mf.check_level = "none"
        mf.methods_done = []
        mf.error_count = 0
        mf.warning_count = 0
        return
    status = "ok"
    done: set[str] = set()
    notes: list[str] = []
    for method in METHOD_ORDER:
        r = results.get(method)
        if r is None:
            continue
        if STATUS_RANK[r.status] > STATUS_RANK[status]:
            status = r.status
        if r.outcome == "completed":
            done.add(method)
        elif r.summary:
            notes.append(r.summary)
    ids = [r.id for r in results.values()]
    errs = db.scalars(select(DetectedError).where(DetectedError.result_id.in_(ids))).all()
    mf.error_count = sum(1 for e in errs if e.severity in ("error", "critical"))
    mf.warning_count = sum(1 for e in errs if e.severity == "warning")
    mf.status = status
    mf.check_level = check_level_for(done)
    mf.methods_done = sorted(done, key=METHOD_ORDER.index)
    mf.last_checked_at = max(r.finished_at for r in results.values())
    if status == "ok" and "full" not in done:
        notes.insert(0, "Ohne erkannte Fehler, aber noch keine vollständige Decodierprüfung")
    mf.status_note = "; ".join(dict.fromkeys(notes)) or None
