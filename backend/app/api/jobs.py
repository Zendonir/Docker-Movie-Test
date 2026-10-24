from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import JobEvent, MediaFile, ScanJob, ScanJobItem
from ..services import jobs as jobsvc
from ..services.events import bus
from ..services.labels import STATUS_LABELS
from ..services.settings import load_settings, update_settings
from ..workers.worker import get_worker
from .schemas import JobCreate, JobUpdate

router = APIRouter(prefix="/api", tags=["Scans"])


def job_dict(j: ScanJob) -> dict:
    return {
        "id": j.id, "name": j.name, "task": j.task, "task_label": jobsvc.TASKS.get(j.task, j.task),
        "methods": j.methods, "library_ids": j.library_ids, "file_count_selected": len(j.file_ids or []),
        "force": j.force, "priority": j.priority, "state": j.state, "phase": j.phase,
        "pause_requested": j.pause_requested, "cancel_requested": j.cancel_requested, "trigger": j.trigger,
        "schedule_id": j.schedule_id, "created_at": j.created_at, "started_at": j.started_at,
        "finished_at": j.finished_at, "total_files": j.total_files, "done_files": j.done_files,
        "skipped_files": j.skipped_files, "failed_files": j.failed_files, "total_bytes": j.total_bytes,
        "done_bytes": j.done_bytes, "files_with_errors": j.files_with_errors,
        "files_with_warnings": j.files_with_warnings, "findings_count": j.findings_count,
        "resume_count": j.resume_count, "active_seconds": j.active_seconds, "message": j.message,
    }


def _get(db: Session, job_id: int) -> ScanJob:
    j = db.get(ScanJob, job_id)
    if j is None:
        raise HTTPException(404, "Auftrag nicht gefunden")
    return j


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200),
              state: list[str] | None = Query(None)):
    q = select(ScanJob)
    if state:
        q = q.where(ScanJob.state.in_(state))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(ScanJob.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "items": [job_dict(j) for j in rows]}


@router.post("/jobs", status_code=201)
def create_job(body: JobCreate, db: Session = Depends(get_db)):
    try:
        job = jobsvc.create_job(db, task=body.task, methods=list(body.methods), library_ids=body.library_ids,
                                file_ids=body.file_ids, priority=body.priority, name=body.name, force=body.force)
    except jobsvc.DuplicateJobError as exc:
        raise HTTPException(409, {"message": str(exc), "job_id": exc.job_id}) from exc
    except jobsvc.JobError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    get_worker().wake()
    bus.publish("job", {"id": job.id, "state": job.state})
    return job_dict(job)


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    j = _get(db, job_id)
    counts = dict(db.execute(select(ScanJobItem.state, func.count()).where(ScanJobItem.job_id == j.id)
                             .group_by(ScanJobItem.state)).all())
    events = db.scalars(select(JobEvent).where(JobEvent.job_id == j.id).order_by(JobEvent.id.desc()).limit(300)).all()
    return {**job_dict(j), "item_counts": counts,
            "events": [{"ts": e.ts, "level": e.level, "message": e.message} for e in events]}


@router.get("/jobs/{job_id}/items")
def job_items(job_id: int, db: Session = Depends(get_db), state: list[str] | None = Query(None),
              status: list[str] | None = Query(None), page: int = Query(1, ge=1),
              page_size: int = Query(50, ge=1, le=500)):
    _get(db, job_id)
    q = select(ScanJobItem, MediaFile).join(MediaFile, MediaFile.id == ScanJobItem.file_id).where(
        ScanJobItem.job_id == job_id)
    if state:
        q = q.where(ScanJobItem.state.in_(state))
    if status:
        q = q.where(ScanJobItem.status.in_(status))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.execute(q.order_by(ScanJobItem.seq).offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "items": [{
        "id": it.id, "file_id": f.id, "filename": f.filename, "directory": f.directory, "size": it.size,
        "state": it.state, "status": it.status, "status_label": STATUS_LABELS.get(it.status or "", it.status),
        "started_at": it.started_at, "finished_at": it.finished_at, "attempts": it.attempts, "message": it.message,
    } for it, f in rows]}


@router.patch("/jobs/{job_id}")
def update_job(job_id: int, body: JobUpdate, db: Session = Depends(get_db)):
    j = _get(db, job_id)
    j.priority = body.priority
    jobsvc.add_event(db, j.id, f"Priorität auf {body.priority} gesetzt")
    get_worker().wake()
    return job_dict(j)


@router.post("/jobs/{job_id}/pause")
def pause_job(job_id: int, db: Session = Depends(get_db)):
    j = _get(db, job_id)
    try:
        jobsvc.request_pause(db, j, load_settings(db).pause_mode)
    except jobsvc.JobError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    get_worker().wake()
    bus.publish("job", {"id": j.id, "state": j.state})
    return job_dict(j)


@router.post("/jobs/{job_id}/resume")
def resume_job(job_id: int, db: Session = Depends(get_db)):
    j = _get(db, job_id)
    try:
        jobsvc.request_resume(db, j)
    except jobsvc.JobError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    get_worker().wake()
    bus.publish("job", {"id": j.id, "state": j.state})
    return job_dict(j)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    j = _get(db, job_id)
    try:
        jobsvc.request_cancel(db, j)
    except jobsvc.JobError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    get_worker().wake()
    bus.publish("job", {"id": j.id, "state": j.state})
    return job_dict(j)


@router.get("/scan/live")
async def live():
    import asyncio
    return await asyncio.to_thread(get_worker().snapshot)


@router.post("/scanner/pause")
def pause_scanner(db: Session = Depends(get_db)):
    update_settings(db, {"scanner_paused": True})
    db.commit()
    get_worker().wake()
    bus.publish("scanner", {"paused": True})
    return {"scanner_paused": True}


@router.post("/scanner/resume")
def resume_scanner(db: Session = Depends(get_db)):
    update_settings(db, {"scanner_paused": False})
    db.commit()
    get_worker().wake()
    bus.publish("scanner", {"paused": False})
    return {"scanner_paused": False}
