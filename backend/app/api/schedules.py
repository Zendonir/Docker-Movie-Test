from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Library, ScanSchedule, utcnow
from ..services import jobs as jobsvc
from ..services.cron import CronError, cron_for
from ..workers.scheduler import SCHEDULE_TASK_MAP, compute_next
from ..workers.worker import get_worker
from .schemas import ScheduleIn

router = APIRouter(prefix="/api/schedules", tags=["Zeitpläne"])

TASK_LABELS = {
    "new": "Neue Dateien prüfen",
    "changed": "Geänderte Dateien prüfen",
    "new_changed": "Neue und geänderte Dateien prüfen",
    "full": "Vollständige Bibliothek erneut prüfen",
    "errors": "Bereits fehlerhafte Dateien nachprüfen",
}


def sched_dict(s: ScanSchedule) -> dict:
    return {
        "id": s.id, "name": s.name, "enabled": s.enabled, "kind": s.kind, "cron": s.cron, "task": s.task,
        "task_label": TASK_LABELS.get(s.task, s.task), "methods": s.methods, "library_ids": s.library_ids,
        "priority": s.priority, "created_at": s.created_at, "last_run_at": s.last_run_at,
        "next_run_at": s.next_run_at, "last_job_id": s.last_job_id, "last_message": s.last_message,
    }


def _apply(s: ScanSchedule, body: ScheduleIn, db: Session) -> None:
    try:
        cron = cron_for(body.kind, body.time, body.weekday, body.day, body.cron)
    except CronError as exc:
        raise HTTPException(400, str(exc)) from exc
    known = set(db.scalars(select(Library.id)))
    if set(body.library_ids) - known:
        raise HTTPException(400, "Unbekannte Bibliothek im Zeitplan")
    s.name, s.enabled, s.kind, s.cron, s.task = body.name.strip(), body.enabled, body.kind, cron, body.task
    s.methods = list(body.methods)
    s.library_ids = sorted(set(body.library_ids))
    s.priority = body.priority
    s.next_run_at = compute_next(s, utcnow())


@router.get("")
def list_schedules(db: Session = Depends(get_db)):
    return [sched_dict(s) for s in db.scalars(select(ScanSchedule).order_by(ScanSchedule.id))]


@router.post("", status_code=201)
def create_schedule(body: ScheduleIn, db: Session = Depends(get_db)):
    s = ScanSchedule()
    _apply(s, body, db)
    db.add(s)
    db.flush()
    return sched_dict(s)


@router.put("/{sid}")
def update_schedule(sid: int, body: ScheduleIn, db: Session = Depends(get_db)):
    s = db.get(ScanSchedule, sid)
    if s is None:
        raise HTTPException(404, "Zeitplan nicht gefunden")
    _apply(s, body, db)
    return sched_dict(s)


@router.delete("/{sid}")
def delete_schedule(sid: int, db: Session = Depends(get_db)):
    s = db.get(ScanSchedule, sid)
    if s is None:
        raise HTTPException(404, "Zeitplan nicht gefunden")
    db.delete(s)
    return {"deleted": sid}


@router.post("/{sid}/run")
def run_now(sid: int, db: Session = Depends(get_db)):
    s = db.get(ScanSchedule, sid)
    if s is None:
        raise HTTPException(404, "Zeitplan nicht gefunden")
    try:
        job = jobsvc.create_job(db, task=SCHEDULE_TASK_MAP.get(s.task, "incremental"), methods=s.methods,
                                library_ids=s.library_ids or None, priority=s.priority, trigger="schedule",
                                schedule_id=s.id, name=f"Zeitplan: {s.name} (manuell)")
    except jobsvc.DuplicateJobError as exc:
        raise HTTPException(409, {"message": str(exc), "job_id": exc.job_id}) from exc
    except jobsvc.JobError as exc:
        raise HTTPException(400, str(exc)) from exc
    s.last_job_id = job.id
    db.commit()
    get_worker().wake()
    return {"job_id": job.id}
