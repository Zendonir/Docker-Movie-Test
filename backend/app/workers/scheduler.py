"""Zeitgesteuerte Prüfungen.

Doppelstarts werden auf zwei Ebenen verhindert:
1. Ein fälliger Zeitplan wird per bedingtem UPDATE (``next_run_at`` unverändert)
   genau einmal "beansprucht".
2. Aktive Aufträge mit identischen Parametern sind über einen eindeutigen
   Teilindex (``dedupe_key``) ausgeschlossen.

Nach einem Neustart werden verpasste Termine höchstens einmal nachgeholt.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from ..config import get_config
from ..database import session_scope
from ..models import ScanSchedule, utcnow
from ..services import jobs as jobsvc
from ..services.cron import Cron, CronError
from ..services.events import bus

log = logging.getLogger(__name__)

SCHEDULE_TASK_MAP = {"new": "new", "changed": "changed", "new_changed": "incremental", "full": "full",
                     "errors": "errors"}


def compute_next(schedule: ScanSchedule, after: datetime | None = None) -> datetime | None:
    try:
        nxt = Cron(schedule.cron).next_after(after or utcnow(), get_config().tz)
        return nxt.astimezone(timezone.utc)
    except CronError:
        return None


def run_due_schedules(now: datetime | None = None) -> list[int]:
    """Startet fällige Zeitpläne. Gibt die IDs der angelegten Aufträge zurück."""
    now = now or utcnow()
    created: list[int] = []
    with session_scope() as db:
        due = db.scalars(select(ScanSchedule).where(ScanSchedule.enabled.is_(True))).all()
        for s in due:
            if s.next_run_at is None:
                s.next_run_at = compute_next(s, now)
                continue
            if s.next_run_at > now:
                continue
            new_next = compute_next(s, now)
            claimed = db.execute(
                update(ScanSchedule)
                .where(ScanSchedule.id == s.id, ScanSchedule.next_run_at == s.next_run_at)
                .values(next_run_at=new_next, last_run_at=now)
            ).rowcount
            if not claimed:
                continue
            db.refresh(s)
            try:
                with db.begin_nested():
                    job = jobsvc.create_job(
                        db, task=SCHEDULE_TASK_MAP.get(s.task, "incremental"), methods=s.methods or ["quick"],
                        library_ids=s.library_ids or None, priority=s.priority, trigger="schedule",
                        schedule_id=s.id, name=f"Zeitplan: {s.name}")
                s.last_job_id = job.id
                s.last_message = f"Auftrag #{job.id} angelegt"
                created.append(job.id)
            except jobsvc.DuplicateJobError as exc:
                s.last_message = f"Übersprungen: identischer Auftrag #{exc.job_id} ist noch aktiv"
            except jobsvc.JobError as exc:
                s.last_message = f"Fehler: {exc}"
    if created:
        bus.stats_changed(force=True)
    return created


async def scheduler_loop(wake_worker, interval: float = 30.0) -> None:
    while True:
        try:
            created = await asyncio.to_thread(run_due_schedules)
            if created:
                log.info("Zeitplan hat Aufträge angelegt: %s", created)
                wake_worker()
        except Exception:  # pragma: no cover
            log.exception("Zeitplaner-Fehler")
        await asyncio.sleep(interval)
