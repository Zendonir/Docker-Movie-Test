"""Hintergrund-Worker für Scanaufträge.

* läuft als asyncio-Task im selben Prozess wie die API, die eigentliche Arbeit
  erledigen Unterprozesse (FFmpeg, mkvmerge, EBML-Prüfer) – die API blockiert nie
* globale Parallelität = ``max_concurrent_scans`` (über alle Aufträge)
* dieselbe Datei wird nie gleichzeitig von zwei Aufträgen geprüft
* Pause sofort (SIGSTOP) oder nach der aktuellen Datei, Abbruch per SIGTERM
* alle Zustände werden laufend in SQLite festgehalten
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from sqlalchemy import func, select, update

from ..database import session_scope
from ..models import ScanJob, ScanJobItem, utcnow
from ..scanners.process import ScanControl
from ..services import jobs as jobsvc
from ..services import filelocks
from ..services.events import bus
from ..services.scan_service import FileProgress, scan_file
from ..services.settings import AppSettings, load_settings

log = logging.getLogger(__name__)


@dataclass
class RunningItem:
    job_id: int
    item_id: int
    file_id: int
    size: int
    control: ScanControl
    task: asyncio.Task | None = None
    progress: FileProgress | None = None


@dataclass
class JobLive:
    samples: deque = field(default_factory=lambda: deque(maxlen=60))  # (monotonic, processed_bytes)
    last_active: float | None = None
    active_seconds: float = 0.0


class ScanWorker:
    def __init__(self) -> None:
        self.running: dict[int, RunningItem] = {}
        self.locked_files: set[int] = set()
        self.live: dict[int, JobLive] = {}
        self._wakeup = asyncio.Event()
        self._stopping = False
        self._preparing: set[int] = set()
        self._task: asyncio.Task | None = None
        self._progress_task: asyncio.Task | None = None
        self.test_hooks: dict | None = None
        self._paused_globally = False

    # -- Lebenszyklus --
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="scan-worker")
        self._progress_task = asyncio.create_task(self._progress_loop(), name="scan-progress")

    async def stop(self, reason: str = "shutdown") -> None:
        """Beendet laufende Prüfungen; die Aufträge bleiben zur Wiederaufnahme 'running'."""
        self._stopping = True
        for r in list(self.running.values()):
            r.control.cancel(reason)
        tasks = [r.task for r in self.running.values() if r.task]
        if tasks:
            await asyncio.wait(tasks, timeout=15)
        for t in (self._task, self._progress_task):
            if t:
                t.cancel()
        self._wakeup.set()

    def wake(self) -> None:
        self._wakeup.set()

    # -- Hauptschleife --
    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except Exception:  # pragma: no cover - Schutz der Schleife
                log.exception("Fehler in der Worker-Schleife")
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self._wakeup.clear()

    async def _tick(self) -> None:
        settings = await asyncio.to_thread(self._load_settings)
        state = await asyncio.to_thread(self._read_jobs)
        self._apply_controls(state, settings)
        if settings.scanner_paused or self._stopping:
            return
        free = settings.max_concurrent_scans - len(self.running)
        for job in state:
            if job["cancel_requested"] or job["pause_requested"] or job["state"] not in ("queued", "running"):
                continue
            if job["phase"] == "prepare":
                if job["id"] not in self._preparing and not self._preparing:
                    self._preparing.add(job["id"])
                    asyncio.create_task(self._prepare(job["id"], settings))
                continue
            if free <= 0:
                continue
            locked = set(self.locked_files) | set(filelocks.optimizer_busy)
            picked = await asyncio.to_thread(self._claim_items, job["id"], free, locked)
            for item in picked:
                self._spawn(item, settings)
                free -= 1
        await asyncio.to_thread(self._finish_jobs)

    def _load_settings(self) -> AppSettings:
        with session_scope() as db:
            return load_settings(db)

    def _read_jobs(self) -> list[dict]:
        with session_scope() as db:
            rows = db.scalars(select(ScanJob).where(ScanJob.state.in_(("queued", "running", "paused")))
                              .order_by(ScanJob.priority.desc(), ScanJob.id.asc())).all()
            return [{"id": j.id, "state": j.state, "phase": j.phase, "pause_requested": j.pause_requested,
                     "cancel_requested": j.cancel_requested} for j in rows]

    def _apply_controls(self, jobs: list[dict], settings: AppSettings) -> None:
        by_id = {j["id"]: j for j in jobs}
        for r in list(self.running.values()):
            j = by_id.get(r.job_id)
            if j is None or j["cancel_requested"]:
                r.control.cancel("user")
                continue
            should_pause = settings.scanner_paused or j["pause_requested"]
            if should_pause and settings.pause_mode == "immediate":
                r.control.pause()
            elif not should_pause and r.control.paused:
                r.control.resume()
        # Abgebrochene Aufträge ohne laufende Dateien abschließen
        for j in jobs:
            if j["cancel_requested"] and not any(r.job_id == j["id"] for r in self.running.values()):
                with session_scope() as db:
                    job = db.get(ScanJob, j["id"])
                    if job and job.state in ("queued", "running", "paused"):
                        jobsvc.finalize_cancel(db, job)
                        jobsvc.add_event(db, job.id, "Auftrag abgebrochen")
                bus.publish("job", {"id": j["id"], "state": "cancelled"})
                bus.stats_changed(force=True)

    async def _prepare(self, job_id: int, settings: AppSettings) -> None:
        try:
            await asyncio.to_thread(self._prepare_sync, job_id, settings)
        except Exception as exc:
            log.exception("Vorbereitung von Auftrag %s fehlgeschlagen", job_id)
            with session_scope() as db:
                job = db.get(ScanJob, job_id)
                if job:
                    job.state = "failed"
                    job.finished_at = utcnow()
                    job.message = f"Vorbereitung fehlgeschlagen: {exc}"
                    jobsvc.add_event(db, job_id, job.message, "error")
        finally:
            self._preparing.discard(job_id)
            bus.publish("job", {"id": job_id})
            bus.stats_changed(force=True)
            self.wake()

    def _prepare_sync(self, job_id: int, settings: AppSettings) -> None:
        def cancel_check() -> bool:
            with session_scope() as db2:
                j = db2.get(ScanJob, job_id)
                return bool(j is None or j.cancel_requested)

        with session_scope() as db:
            job = db.get(ScanJob, job_id)
            if job is None or job.phase != "prepare":
                return
            if job.started_at is None:
                job.started_at = utcnow()
            job.state = "running"
            jobsvc.prepare_job(db, job, settings, cancel_check)

    def _claim_items(self, job_id: int, limit: int, locked: set[int]) -> list[dict]:
        out = []
        with session_scope() as db:
            job = db.get(ScanJob, job_id)
            if job is None or job.state not in ("queued", "running") or job.pause_requested or job.cancel_requested:
                return []
            rows = db.scalars(select(ScanJobItem).where(ScanJobItem.job_id == job_id, ScanJobItem.state == "pending")
                              .order_by(ScanJobItem.seq).limit(limit + len(locked) + 5)).all()
            for item in rows:
                if len(out) >= limit:
                    break
                if item.file_id in locked:
                    continue
                item.state = "running"
                item.started_at = utcnow()
                item.attempts += 1
                locked.add(item.file_id)
                out.append({"job_id": job_id, "item_id": item.id, "file_id": item.file_id, "size": item.size,
                            "methods": list(item.methods or job.methods), "force": job.force})
            if out:
                if job.state != "running":
                    job.state = "running"
                    jobsvc.add_event(db, job_id, "Prüfung gestartet")
                if job.started_at is None:
                    job.started_at = utcnow()
        return out

    def _spawn(self, item: dict, settings: AppSettings) -> None:
        control = ScanControl()
        r = RunningItem(job_id=item["job_id"], item_id=item["item_id"], file_id=item["file_id"], size=item["size"],
                        control=control)
        self.running[item["item_id"]] = r
        self.locked_files.add(item["file_id"])
        self.live.setdefault(item["job_id"], JobLive())
        r.task = asyncio.create_task(self._run_item(r, item, settings))

    async def _run_item(self, r: RunningItem, item: dict, settings: AppSettings) -> None:
        def on_progress(p: FileProgress) -> None:
            r.progress = p

        outcome = None
        error: str | None = None
        try:
            outcome = await scan_file(item["file_id"], item["methods"], settings, r.control, job_id=item["job_id"],
                                      force=item["force"], progress=on_progress, test_hooks=self.test_hooks)
        except asyncio.CancelledError:
            r.control.cancel("shutdown")
            raise
        except Exception as exc:  # Werkzeug- oder Programmfehler: Datei als fehlgeschlagen markieren, Job läuft weiter
            log.exception("Prüfung von Datei %s fehlgeschlagen", item["file_id"])
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self.running.pop(r.item_id, None)
            self.locked_files.discard(r.file_id)
        await asyncio.to_thread(self._finish_item, r, outcome, error)
        bus.publish("file", {"id": r.file_id})
        bus.stats_changed()
        self.wake()

    def _finish_item(self, r: RunningItem, outcome, error: str | None) -> None:
        inc: dict[str, int] = {}
        with session_scope() as db:
            item = db.get(ScanJobItem, r.item_id)
            if item is None or db.get(ScanJob, r.job_id) is None:
                return
            item.finished_at = utcnow()
            if r.control.cancelled and r.control.cancel_reason != "user":
                # Herunterfahren: Datei bleibt ausstehend und wird nach dem Neustart wiederholt
                item.state = "pending"
                item.message = "Durch Neustart/Herunterfahren unterbrochen"
                return
            if error:
                item.state = "failed"
                item.message = error[:2000]
                inc["failed_files"] = 1
                jobsvc.add_event(db, r.job_id, f"Datei-ID {r.file_id}: interner Fehler – {error[:300]}", "error")
            elif outcome is None:
                item.state = "failed"
                inc["failed_files"] = 1
            elif outcome.cancelled:
                item.state = "cancelled"
                item.status = outcome.status
            elif outcome.skipped:
                item.state = "skipped"
                item.message = outcome.message
                inc["skipped_files"] = 1
            else:
                item.state = "done"
                item.status = outcome.status
                inc["done_files"] = 1
                inc["findings_count"] = outcome.findings
                if outcome.status in ("critical", "error"):
                    inc["files_with_errors"] = 1
                elif outcome.status == "warning":
                    inc["files_with_warnings"] = 1
            if item.state in ("done", "skipped", "failed"):
                inc["done_bytes"] = item.size or 0
            # Atomare Erhöhung – mehrere parallele Prüfungen schreiben gleichzeitig
            values = {k: getattr(ScanJob, k) + v for k, v in inc.items()}
            live = self.live.get(r.job_id)
            if live:
                values["active_seconds"] = live.active_seconds
            if values:
                db.execute(update(ScanJob).where(ScanJob.id == r.job_id).values(**values))

    def _finish_jobs(self) -> None:
        with session_scope() as db:
            for job in db.scalars(select(ScanJob).where(ScanJob.state.in_(("running", "queued")), ScanJob.phase == "scan",
                                                   ScanJob.pause_requested.is_(False), ScanJob.cancel_requested.is_(False))):
                open_items = db.scalar(select(func.count()).select_from(ScanJobItem).where(
                    ScanJobItem.job_id == job.id, ScanJobItem.state.in_(("pending", "running"))))
                if open_items == 0 and not any(r.job_id == job.id for r in self.running.values()):
                    job.state = "completed"
                    job.phase = "done"
                    job.finished_at = utcnow()
                    live = self.live.pop(job.id, None)
                    if live:
                        job.active_seconds = live.active_seconds
                    jobsvc.add_event(db, job.id, (
                        f"Auftrag abgeschlossen: {job.done_files} geprüft, {job.skipped_files} übersprungen, "
                        f"{job.failed_files} fehlgeschlagen, {job.files_with_errors} Dateien mit Fehlern, "
                        f"{job.files_with_warnings} mit Warnungen"))
                    bus.publish("job", {"id": job.id, "state": "completed"})
                    bus.stats_changed(force=True)

    # -- Fortschritt --
    def snapshot(self) -> dict:
        """Live-Zustand aller aktiven Aufträge (für SSE und /api/scan/live)."""
        with session_scope() as db:
            jobs = db.scalars(select(ScanJob).where(ScanJob.state.in_(("queued", "running", "paused")))
                              .order_by(ScanJob.priority.desc(), ScanJob.id)).all()
            out = []
            now = time.monotonic()
            for j in jobs:
                running = [r for r in self.running.values() if r.job_id == j.id]
                partial = sum(int((r.progress.fraction if r.progress else 0) * r.size) for r in running)
                processed = (j.done_bytes or 0) + partial
                live = self.live.setdefault(j.id, JobLive())
                is_active = bool(running) and not all(r.control.paused for r in running)
                if is_active:
                    if live.last_active is not None:
                        live.active_seconds += now - live.last_active
                    live.last_active = now
                    live.samples.append((now, processed))
                else:
                    live.last_active = None
                speed = None
                if len(live.samples) >= 2:
                    t0, b0 = live.samples[0]
                    t1, b1 = live.samples[-1]
                    if t1 - t0 >= 2 and b1 >= b0:
                        speed = (b1 - b0) / (t1 - t0)
                if (speed is None or speed == 0) and live.active_seconds > 5 and processed > 0:
                    speed = processed / live.active_seconds
                remaining = max((j.total_bytes or 0) - processed, 0)
                eta = remaining / speed if speed else None
                out.append({
                    "id": j.id, "name": j.name, "state": j.state, "phase": j.phase, "priority": j.priority,
                    "methods": j.methods, "total_files": j.total_files, "done_files": j.done_files,
                    "skipped_files": j.skipped_files, "failed_files": j.failed_files,
                    "total_bytes": j.total_bytes, "processed_bytes": processed,
                    "files_with_errors": j.files_with_errors, "files_with_warnings": j.files_with_warnings,
                    "findings_count": j.findings_count, "speed_bps": speed, "eta_seconds": eta,
                    "pause_requested": j.pause_requested, "parallel": len(running),
                    "pausing_after_file": j.pause_requested and any(not r.control.paused for r in running),
                    "current": [{
                        "file_id": r.file_id,
                        "filename": r.progress.filename if r.progress else None,
                        "size": r.size,
                        "method": r.progress.method if r.progress else None,
                        "fraction": round(r.progress.fraction, 4) if r.progress else 0.0,
                        "paused": r.control.paused,
                    } for r in running],
                })
        return {"jobs": out, "running": len(self.running), "paused_globally": self._paused_globally}

    async def _progress_loop(self) -> None:
        while not self._stopping:
            try:
                snap = await asyncio.to_thread(self.snapshot)
                if snap["jobs"] or self.running:
                    bus.publish("progress", snap)
            except Exception:  # pragma: no cover
                log.exception("Fortschrittsermittlung fehlgeschlagen")
            await asyncio.sleep(1.0)


worker: ScanWorker | None = None


def get_worker() -> ScanWorker:
    if worker is None:
        raise RuntimeError("Worker nicht gestartet")
    return worker


def set_worker(w: ScanWorker | None) -> None:
    global worker
    worker = w
