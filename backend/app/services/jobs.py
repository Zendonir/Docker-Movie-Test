"""Scanaufträge: Anlegen (mit Duplikatschutz), Vorbereiten, Steuern, Wiederaufnahme."""

from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import JOB_ACTIVE_STATES, JobEvent, Library, MediaFile, ScanJob, ScanJobItem, ScanResult, utcnow
from ..scanners.base import SCANNER_VERSIONS, Method
from .library import inventory_library
from .settings import AppSettings

log = logging.getLogger(__name__)

TASKS = {
    "incremental": "Neue, geänderte und unvollständig geprüfte Dateien",
    "new": "Nur neue Dateien",
    "changed": "Nur geänderte Dateien",
    "full": "Vollständige Bibliothek erneut prüfen",
    "errors": "Fehlerhafte Dateien erneut prüfen",
    "files": "Ausgewählte Dateien prüfen",
}
METHOD_SET = ("quick", "container", "full")


class DuplicateJobError(Exception):
    def __init__(self, job_id: int):
        super().__init__(f"Ein identischer Auftrag ist bereits aktiv (#{job_id})")
        self.job_id = job_id


class JobError(ValueError):
    pass


def add_event(db: Session, job_id: int, message: str, level: str = "info") -> None:
    db.add(JobEvent(job_id=job_id, message=message, level=level))


def dedupe_key(task: str, methods: list[str], library_ids: list[int], file_ids: list[int] | None, force: bool) -> str:
    payload = json.dumps({"t": task, "m": sorted(methods), "l": sorted(library_ids), "f": sorted(file_ids or []),
                          "x": force}, sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()


def create_job(db: Session, *, task: str, methods: list[str], library_ids: list[int] | None = None,
               file_ids: list[int] | None = None, priority: int = 0, name: str | None = None,
               trigger: str = "manual", schedule_id: int | None = None, force: bool | None = None) -> ScanJob:
    if task not in TASKS:
        raise JobError(f"Unbekannte Aufgabe: {task}")
    methods = [m for m in METHOD_SET if m in set(methods)]
    if not methods:
        raise JobError("Mindestens eine Prüfmethode wählen")
    if task == "files":
        if not file_ids:
            raise JobError("Keine Dateien ausgewählt")
        file_ids = sorted(set(int(i) for i in file_ids))
        if len(file_ids) > 100000:
            raise JobError("Zu viele Dateien in einem Auftrag")
        found = db.scalar(select(func.count()).select_from(MediaFile).where(MediaFile.id.in_(file_ids)))
        if found != len(file_ids):
            raise JobError("Unbekannte Datei-IDs")
        library_ids = []
    else:
        all_ids = [lib.id for lib in db.scalars(select(Library).where(Library.enabled.is_(True)))]
        library_ids = sorted(set(library_ids or all_ids))
        unknown = set(library_ids) - set(all_ids)
        if unknown:
            raise JobError(f"Unbekannte oder deaktivierte Bibliotheken: {sorted(unknown)}")
        if not library_ids:
            raise JobError("Keine aktive Bibliothek vorhanden")
        file_ids = None
    if force is None:
        force = task in ("full", "errors", "files")
    key = dedupe_key(task, methods, library_ids, file_ids, force)
    existing = db.scalar(select(ScanJob.id).where(ScanJob.dedupe_key == key, ScanJob.state.in_(JOB_ACTIVE_STATES)))
    if existing:
        raise DuplicateJobError(existing)
    if not name:
        name = TASKS[task]
        if task == "files" and file_ids and len(file_ids) == 1:
            fname = db.scalar(select(MediaFile.filename).where(MediaFile.id == file_ids[0]))
            name = f"Erneute Prüfung: {fname}"
    job = ScanJob(name=name[:300], task=task, methods=methods, library_ids=library_ids, file_ids=file_ids,
                  force=bool(force), priority=int(priority), state="queued", phase="prepare", dedupe_key=key,
                  trigger=trigger, schedule_id=schedule_id)
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError as exc:  # gleichzeitig angelegter identischer Auftrag
        existing = db.scalar(select(ScanJob.id).where(ScanJob.dedupe_key == key, ScanJob.state.in_(JOB_ACTIVE_STATES)))
        raise DuplicateJobError(existing or 0) from exc
    add_event(db, job.id, f"Auftrag angelegt ({TASKS[task]}; Methoden: {', '.join(methods)}; Auslöser: {trigger})")
    return job


def _needs_any_method(methods: list[str]):
    """SQL-Bedingung: Datei benötigt mindestens eine der Methoden (kein vollständiges aktuelles Ergebnis)."""
    conds = []
    for m in methods:
        done = exists().where(and_(
            ScanResult.file_id == MediaFile.id,
            ScanResult.method == m,
            ScanResult.is_current.is_(True),
            ScanResult.outcome.in_(("completed", "partial")),
            ScanResult.fingerprint_version == MediaFile.fingerprint_version,
            ScanResult.scanner_version == SCANNER_VERSIONS[Method(m)],
        ))
        conds.append(~done)
    return or_(*conds)


def prepare_job(db: Session, job: ScanJob, settings: AppSettings, cancel_check=None) -> None:
    """Bestandsaufnahme der Bibliotheken und Ermittlung der zu prüfenden Dateien."""
    if job.task != "files":
        for lib_id in job.library_ids:
            lib = db.get(Library, lib_id)
            if lib is None or not lib.enabled:
                continue
            try:
                stats = inventory_library(db, lib, settings, cancel_check)
                add_event(db, job.id, (
                    f"Bestandsaufnahme '{lib.name}': {stats.files_found} Dateien, {stats.new} neu, "
                    f"{stats.changed} geändert, {stats.moved} verschoben, {stats.missing} nicht mehr vorhanden"
                    f" ({stats.duration_s:.1f} s)"))
                for w in stats.warnings[:20]:
                    add_event(db, job.id, w, "warning")
                if stats.unreadable_dirs:
                    add_event(db, job.id, f"{len(stats.unreadable_dirs)} Verzeichnisse nicht lesbar, z. B. "
                              f"{stats.unreadable_dirs[0]}", "warning")
            except FileNotFoundError as exc:
                add_event(db, job.id, str(exc), "error")

    q = select(MediaFile.id, MediaFile.size).where(MediaFile.present.is_(True))
    if job.task == "files":
        q = select(MediaFile.id, MediaFile.size).where(MediaFile.id.in_(job.file_ids or []))
    else:
        q = q.where(MediaFile.library_id.in_(job.library_ids))
        if job.task == "new":
            q = q.where(~exists().where(ScanResult.file_id == MediaFile.id), MediaFile.fingerprint_version == 1)
        elif job.task == "changed":
            q = q.where(MediaFile.fingerprint_version > 1, _needs_any_method(job.methods))
        elif job.task == "errors":
            q = q.where(MediaFile.status.in_(("critical", "error", "warning")))
        elif job.task == "incremental" and not job.force:
            q = q.where(_needs_any_method(job.methods))
    candidates_total = None
    if job.task in ("incremental", "new", "changed") and not job.force:
        candidates_total = db.scalar(select(func.count()).select_from(MediaFile).where(
            MediaFile.present.is_(True), MediaFile.library_id.in_(job.library_ids)))
    rows = db.execute(q.order_by(MediaFile.library_id, MediaFile.rel_path)).all()
    existing = set(db.scalars(select(ScanJobItem.file_id).where(ScanJobItem.job_id == job.id)))
    seq = 0
    for fid, size in rows:
        seq += 1
        if fid in existing:
            continue
        db.add(ScanJobItem(job_id=job.id, file_id=fid, seq=seq, state="pending", size=size or 0, methods=job.methods))
    job.total_files = len(rows)
    job.total_bytes = int(sum(size or 0 for _, size in rows))
    if candidates_total is not None:
        unchanged = max(candidates_total - len(rows), 0)
    if candidates_total is not None and unchanged > 0:
        job.message = f"{unchanged} unveränderte, bereits geprüfte Dateien übersprungen"
        add_event(db, job.id, job.message)
    job.phase = "scan"
    add_event(db, job.id, f"{len(rows)} Dateien zur Prüfung vorgemerkt ({job.total_bytes / 1e9:.2f} GB)")


def request_pause(db: Session, job: ScanJob, pause_mode: str) -> None:
    if job.state not in ("queued", "running"):
        raise JobError("Nur laufende oder wartende Aufträge können pausiert werden")
    job.pause_requested = True
    job.state = "paused"
    add_event(db, job.id, "Pause angefordert" + (" (sofort)" if pause_mode == "immediate" else
                                                  " (nach Abschluss der aktuellen Datei)"))


def request_resume(db: Session, job: ScanJob) -> None:
    if job.state not in ("paused", "interrupted", "failed"):
        raise JobError("Auftrag ist nicht pausiert oder unterbrochen")
    if job.state == "interrupted":
        job.resume_count += 1
    job.pause_requested = False
    job.state = "queued"
    job.finished_at = None
    db.execute(update(ScanJobItem).where(ScanJobItem.job_id == job.id, ScanJobItem.state == "running")
               .values(state="pending"))
    add_event(db, job.id, "Auftrag fortgesetzt")


def request_cancel(db: Session, job: ScanJob) -> None:
    if job.state not in JOB_ACTIVE_STATES and job.state != "interrupted":
        raise JobError("Auftrag ist bereits beendet")
    job.cancel_requested = True
    if job.state in ("queued", "paused", "interrupted"):
        finalize_cancel(db, job)
    add_event(db, job.id, "Abbruch durch Benutzer angefordert")


def finalize_cancel(db: Session, job: ScanJob) -> None:
    db.execute(update(ScanJobItem).where(ScanJobItem.job_id == job.id, ScanJobItem.state.in_(("pending", "running")))
               .values(state="cancelled"))
    job.state = "cancelled"
    job.finished_at = utcnow()
    job.pause_requested = False


def recover_after_restart(db: Session, settings: AppSettings) -> list[int]:
    """Unvollständige Aufträge nach einem Neustart nachvollziehbar wieder aufnehmen."""
    resumed: list[int] = []
    for job in db.scalars(select(ScanJob).where(ScanJob.state.in_(JOB_ACTIVE_STATES + ("interrupted",)))):
        running_items = db.scalars(select(ScanJobItem).where(ScanJobItem.job_id == job.id,
                                                             ScanJobItem.state == "running")).all()
        for item in running_items:
            item.state = "pending"
            fname = db.scalar(select(MediaFile.filename).where(MediaFile.id == item.file_id))
            add_event(db, job.id, f"Prüfung von '{fname}' wurde durch einen Neustart unterbrochen und wird wiederholt.",
                      "warning")
        if job.cancel_requested:
            finalize_cancel(db, job)
            add_event(db, job.id, "Abbruch nach Neustart abgeschlossen")
            continue
        if job.state == "running" or (job.state == "queued" and job.started_at is not None):
            pending = db.scalar(select(func.count()).select_from(ScanJobItem).where(
                ScanJobItem.job_id == job.id, ScanJobItem.state == "pending"))
            if settings.auto_resume_after_restart:
                job.state = "queued"
                job.resume_count += 1
                add_event(db, job.id, f"Neustart erkannt – Auftrag wird automatisch fortgesetzt "
                          f"({pending} Dateien ausstehend, Phase: {job.phase}).", "warning")
                resumed.append(job.id)
            else:
                job.state = "interrupted"
                add_event(db, job.id, f"Neustart erkannt – Auftrag unterbrochen ({pending} Dateien ausstehend). "
                          "Manuell fortsetzen.", "warning")
    return resumed
