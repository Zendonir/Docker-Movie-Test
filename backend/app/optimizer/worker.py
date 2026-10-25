"""Hintergrund-Worker des MKV-Optimizers (bearbeitet genau eine Datei gleichzeitig)."""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import func, select, update

from ..database import session_scope
from ..models import Library, MediaFile, OptimizerItem, OptimizerJob, utcnow
from ..scanners.process import ScanControl
from ..services import filelocks
from ..services.events import bus
from ..services.library import inventory_library
from ..services.settings import load_settings
from . import fileops
from .runner import process_item

log = logging.getLogger(__name__)
ACTIVE = ("queued", "running", "paused")


def add_event(job: OptimizerJob, message: str, level: str = "info") -> None:
    ev = list(job.events or [])
    ev.append({"ts": utcnow().isoformat(), "level": level, "message": message})
    job.events = ev[-300:]


def recover_after_restart() -> None:
    """Unterbrochene Aufträge nach einem Neustart aufräumen und fortsetzen."""
    with session_scope() as db:
        for item in db.scalars(select(OptimizerItem).where(OptimizerItem.state == "running")):
            fileops.remove_quiet(list(item.temp_paths or []))
            item.temp_paths = []
            item.state = "pending"
            job = db.get(OptimizerJob, item.job_id)
            if job:
                add_event(job, f"Bearbeitung von „{item.source_path.rsplit('/', 1)[-1]}“ wurde durch einen Neustart "
                               "unterbrochen; temporäre Dateien entfernt, das Original ist unverändert. Wird wiederholt.",
                          "warning")
        for job in db.scalars(select(OptimizerJob).where(OptimizerJob.state.in_(ACTIVE))):
            if job.cancel_requested:
                _finalize_cancel(db, job)
            elif job.state == "running":
                job.state = "queued"
                job.resume_count += 1
                add_event(job, "Neustart erkannt – Auftrag wird fortgesetzt", "warning")


def _finalize_cancel(db, job: OptimizerJob) -> None:
    db.execute(update(OptimizerItem).where(OptimizerItem.job_id == job.id,
                                           OptimizerItem.state.in_(("pending", "running")))
               .values(state="cancelled"))
    job.state = "cancelled"
    job.finished_at = utcnow()
    add_event(job, "Auftrag abgebrochen")


class OptimizerWorker:
    def __init__(self, scan_worker=None) -> None:
        self.scan_worker = scan_worker
        self.current: dict | None = None
        self.control: ScanControl | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task | None = None
        self._progress_task: asyncio.Task | None = None
        self._item_task: asyncio.Task | None = None
        self._samples: dict[int, list[tuple[float, int]]] = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="optimizer-worker")
        self._progress_task = asyncio.create_task(self._progress_loop(), name="optimizer-progress")

    async def stop(self) -> None:
        self._stopping = True
        if self.control:
            self.control.cancel("shutdown")
        if self._item_task:
            await asyncio.wait([self._item_task], timeout=15)
        for t in (self._task, self._progress_task):
            if t:
                t.cancel()

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except Exception:  # pragma: no cover
                log.exception("Fehler im Optimizer-Worker")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def _jobs(self) -> list[dict]:
        with session_scope() as db:
            return [{"id": j.id, "state": j.state, "pause": j.pause_requested, "cancel": j.cancel_requested}
                    for j in db.scalars(select(OptimizerJob).where(OptimizerJob.state.in_(ACTIVE))
                                        .order_by(OptimizerJob.id))]

    async def _tick(self) -> None:
        jobs = await asyncio.to_thread(self._jobs)
        settings = await asyncio.to_thread(lambda: _settings())
        cur = self.current
        if cur and self.control:
            j = next((x for x in jobs if x["id"] == cur["job_id"]), None)
            if j is None or j["cancel"]:
                self.control.cancel("user")
            elif j["pause"] or settings.scanner_paused:
                self.control.pause()
            elif self.control.paused:
                self.control.resume()
        for j in jobs:
            if j["cancel"] and not (cur and cur["job_id"] == j["id"]):
                def fin(jid=j["id"]):
                    with session_scope() as db:
                        job = db.get(OptimizerJob, jid)
                        if job and job.state in ACTIVE:
                            _finalize_cancel(db, job)
                await asyncio.to_thread(fin)
                bus.publish("optimizer", {"job": j["id"]})
        if cur is not None or settings.scanner_paused or self._stopping:
            return
        scan_locked = set(self.scan_worker.locked_files) if self.scan_worker else set()
        for j in jobs:
            if j["pause"] or j["cancel"] or j["state"] not in ("queued", "running"):
                continue
            item = await asyncio.to_thread(self._claim, j["id"], scan_locked)
            if item:
                self._start_item(item)
                return
        await asyncio.to_thread(self._finish_jobs)

    def _claim(self, job_id: int, locked: set[int]) -> dict | None:
        with session_scope() as db:
            job = db.get(OptimizerJob, job_id)
            for item in db.scalars(select(OptimizerItem).where(OptimizerItem.job_id == job_id,
                                                               OptimizerItem.state == "pending")
                                   .order_by(OptimizerItem.seq).limit(50)):
                if item.file_id in locked:
                    continue
                item.state = "running"
                item.attempts += 1
                item.started_at = utcnow()
                if job.state != "running":
                    job.state = "running"
                    if job.started_at is None:
                        job.started_at = utcnow()
                    add_event(job, "Bearbeitung gestartet")
                return {"job_id": job_id, "item_id": item.id, "file_id": item.file_id, "size": item.size,
                        "name": item.source_path.rsplit("/", 1)[-1], "kind": job.kind, "params": job.params}
        return None

    def _start_item(self, item: dict) -> None:
        self.control = ScanControl()
        self.current = dict(item, fraction=0.0, phase="Start", started=time.monotonic())
        if item["file_id"]:
            filelocks.optimizer_busy.add(item["file_id"])

        def progress(frac: float, phase: str) -> None:
            if self.current is not None:
                self.current["fraction"] = max(self.current["fraction"], min(frac, 1.0))
                self.current["phase"] = phase

        async def run() -> None:
            control = self.control
            try:
                res = await process_item(item["item_id"], item["kind"], item["params"], control, progress)
            except Exception as exc:  # pragma: no cover
                log.exception("Optimizer-Eintrag %s fehlgeschlagen", item["item_id"])
                from .runner import ItemResult
                res = ItemResult(state="failed", message=f"Interner Fehler: {exc}")
            await asyncio.to_thread(self._store, item, res, control)
            filelocks.optimizer_busy.discard(item["file_id"])
            self.current = None
            self.control = None
            bus.publish("optimizer", {"job": item["job_id"], "item": item["item_id"]})
            bus.stats_changed()
            self.wake()

        self._item_task = asyncio.create_task(run())

    def _store(self, item: dict, res, control: ScanControl) -> None:
        with session_scope() as db:
            it = db.get(OptimizerItem, item["item_id"])
            job = db.get(OptimizerJob, item["job_id"])
            if it is None or job is None:
                return
            if control.cancelled and control.cancel_reason == "shutdown":
                it.state = "pending"
                it.message = "Durch Herunterfahren unterbrochen – wird wiederholt"
                it.temp_paths = []
                return
            it.state = res.state
            it.message = res.message
            it.log = res.log
            it.plan = res.plan or {}
            it.output_path = res.output_path
            it.backup_path = res.backup_path
            it.output_size = res.output_size
            it.finished_at = utcnow()
            it.temp_paths = []
            if it.started_at:
                it.duration_s = (it.finished_at - it.started_at).total_seconds()
            inc: dict[str, int] = {}
            if res.state == "done":
                inc["done_files"] = 1
                if res.output_size is not None and job.kind == "remux":
                    inc["saved_bytes"] = max((it.size or 0) - res.output_size, 0)
            elif res.state == "skipped":
                inc["skipped_files"] = 1
            elif res.state == "failed":
                inc["failed_files"] = 1
                add_event(job, f"Fehlgeschlagen: {it.source_path.rsplit('/', 1)[-1]} – {res.message}", "error")
            if res.state in ("done", "skipped", "failed"):
                inc["done_bytes"] = it.size or 0
            if inc:
                db.execute(update(OptimizerJob).where(OptimizerJob.id == job.id)
                           .values(**{k: getattr(OptimizerJob, k) + v for k, v in inc.items()}))

    def _finish_jobs(self) -> None:
        libs_to_refresh: set[int] = set()
        with session_scope() as db:
            for job in db.scalars(select(OptimizerJob).where(OptimizerJob.state.in_(("queued", "running")),
                                                             OptimizerJob.pause_requested.is_(False),
                                                             OptimizerJob.cancel_requested.is_(False))):
                open_items = db.scalar(select(func.count()).select_from(OptimizerItem).where(
                    OptimizerItem.job_id == job.id, OptimizerItem.state.in_(("pending", "running"))))
                if open_items:
                    continue
                job.state = "completed"
                job.finished_at = utcnow()
                add_event(job, f"Auftrag abgeschlossen: {job.done_files} bearbeitet, {job.skipped_files} übersprungen, "
                               f"{job.failed_files} fehlgeschlagen")
                if job.params.get("ops", {}).get("output_mode") != "output_dir":
                    for (lib_id,) in db.execute(select(MediaFile.library_id).join(
                            OptimizerItem, OptimizerItem.file_id == MediaFile.id).where(
                            OptimizerItem.job_id == job.id).distinct()):
                        libs_to_refresh.add(lib_id)
                bus.publish("optimizer", {"job": job.id, "state": "completed"})
        for lib_id in libs_to_refresh:
            try:
                with session_scope() as db:
                    lib = db.get(Library, lib_id)
                    if lib:
                        inventory_library(db, lib, load_settings(db))
            except Exception:  # pragma: no cover
                log.exception("Bestandsaufnahme nach Optimierung fehlgeschlagen")
        if libs_to_refresh:
            bus.stats_changed(force=True)

    def snapshot(self) -> dict:
        with session_scope() as db:
            jobs = db.scalars(select(OptimizerJob).where(OptimizerJob.state.in_(ACTIVE)).order_by(OptimizerJob.id)).all()
            out = []
            now = time.monotonic()
            for j in jobs:
                cur = self.current if self.current and self.current["job_id"] == j.id else None
                processed = (j.done_bytes or 0) + (int(cur["fraction"] * cur["size"]) if cur else 0)
                samples = self._samples.setdefault(j.id, [])
                if cur and not (self.control and self.control.paused):
                    samples.append((now, processed))
                    del samples[:-60]
                speed = None
                if len(samples) >= 2 and samples[-1][0] - samples[0][0] >= 2:
                    speed = (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0]) or None
                remaining = max((j.total_bytes or 0) - processed, 0)
                file_eta = None
                if cur and cur["fraction"] > 0.02:
                    el = now - cur["started"]
                    file_eta = el / cur["fraction"] * (1 - cur["fraction"])
                out.append({
                    "id": j.id, "kind": j.kind, "name": j.name, "state": j.state, "pause_requested": j.pause_requested,
                    "total_files": j.total_files, "done_files": j.done_files, "skipped_files": j.skipped_files,
                    "failed_files": j.failed_files, "total_bytes": j.total_bytes, "processed_bytes": processed,
                    "saved_bytes": j.saved_bytes, "speed_bps": speed, "eta_seconds": remaining / speed if speed else None,
                    "current": None if not cur else {
                        "item_id": cur["item_id"], "file_id": cur["file_id"], "name": cur["name"], "size": cur["size"],
                        "phase": cur["phase"], "fraction": round(cur["fraction"], 4), "eta_seconds": file_eta,
                        "elapsed": now - cur["started"], "paused": bool(self.control and self.control.paused)},
                })
        return {"jobs": out}

    async def _progress_loop(self) -> None:
        while not self._stopping:
            try:
                if self.current is not None:
                    bus.publish("optimizer_progress", await asyncio.to_thread(self.snapshot))
            except Exception:  # pragma: no cover
                log.exception("Optimizer-Fortschritt")
            await asyncio.sleep(1.0)


def _settings():
    with session_scope() as db:
        return load_settings(db)


worker: OptimizerWorker | None = None
