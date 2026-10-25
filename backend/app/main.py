"""Einstiegspunkt der Webanwendung."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from . import __version__
from .api import auth_routes, events, exports_routes, files, jobs, libraries, optimizer, schedules, settings_routes, stats, system
from .api.deps import require_auth
from .config import get_config
from .database import dispose_engine, get_engine, init_engine, session_scope
from .database.migrate import upgrade
from .models import Library
from .scanners.tools import get_toolset
from .services import auth
from .services.events import bus
from .services.jobs import recover_after_restart
from .services.pathsec import real
from .services.settings import invalidate_cache, load_settings, update_settings
from .workers.scheduler import scheduler_loop
from .optimizer import worker as optworker
from .workers.worker import ScanWorker, set_worker

log = logging.getLogger("media_integrity")


def setup_logging() -> None:
    cfg = get_config()
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(cfg.log_level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        fh = logging.handlers.RotatingFileHandler(cfg.log_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5,
                                                  encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def bootstrap() -> None:
    """Synchroner Teil des Starts: Verzeichnisse, Datenbank, Migrationen, Wiederaufnahme."""
    cfg = get_config()
    for d in (cfg.config_dir, cfg.log_dir, cfg.scan_log_dir, cfg.tmp_dir):
        d.mkdir(parents=True, exist_ok=True)
    setup_logging()
    auth.init_secret(cfg.config_dir)
    upgrade(cfg.db_path)
    init_engine(cfg.db_path)
    invalidate_cache()
    with session_scope() as db:
        settings = load_settings(db, refresh=True)
        if cfg.auth_enabled and cfg.auth_username and cfg.auth_password:
            # Anmeldedaten aus der Umgebung haben Vorrang
            if settings.auth_username != cfg.auth_username or not auth.verify_password(cfg.auth_password,
                                                                                        settings.auth_password_hash):
                update_settings(db, {"auth_enabled": True, "auth_username": cfg.auth_username,
                                     "auth_password_hash": auth.hash_password(cfg.auth_password)})
            elif not settings.auth_enabled:
                update_settings(db, {"auth_enabled": True})
        if cfg.auto_create_library and db.scalar(select(Library.id).limit(1)) is None:
            for root in cfg.media_roots:
                if root.is_dir():
                    db.add(Library(name="Filme" if len(cfg.media_roots) == 1 else root.name or "Medien",
                                   path=real(root), exclude_patterns=[]))
                    log.info("Bibliothek automatisch angelegt: %s", root)
        optimizer.ensure_builtin_profiles(db)
        resumed = recover_after_restart(db, load_settings(db))
        if resumed:
            log.warning("Nach Neustart fortgesetzte Aufträge: %s", resumed)
    optworker.recover_after_restart()
    tools = get_toolset()
    log.info("Media Integrity Scanner %s – Werkzeuge: %s, %d decodierbare Codecs, UID %s/GID %s",
             __version__, tools.versions(), len(tools.decodable_codecs), os.getuid(), os.getgid())
    for root in cfg.media_roots:
        if os.access(root, os.W_OK):
            log.warning("Medienverzeichnis %s ist beschreibbar eingebunden – empfohlen ist ':ro'.", root)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(bootstrap)
    bus.bind_loop(asyncio.get_running_loop())
    w = ScanWorker()
    set_worker(w)
    app.state.worker = w
    w.start()
    ow = optworker.OptimizerWorker(scan_worker=w)
    optworker.worker = ow
    ow.start()
    sched = asyncio.create_task(scheduler_loop(w.wake))
    try:
        yield
    finally:
        sched.cancel()
        await ow.stop()
        optworker.worker = None
        await w.stop("shutdown")
        set_worker(None)
        dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="Media Integrity Scanner", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)

    @app.get("/health", include_in_schema=False)
    def health():
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover
            return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)
        w = getattr(app.state, "worker", None)
        alive = bool(w and w._task and not w._task.done())
        if not alive:
            return JSONResponse({"status": "error", "detail": "Worker läuft nicht"}, status_code=503)
        return {"status": "ok", "version": __version__}

    app.include_router(auth_routes.router)
    protected = [Depends(require_auth)]
    for r in (libraries.router, files.router, jobs.router, schedules.router, settings_routes.router, stats.router,
              exports_routes.router, events.router, system.router, optimizer.router):
        app.include_router(r, dependencies=protected)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    static_dir = get_config().static_dir
    index = static_dir / "index.html"
    if (static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Nicht gefunden"}, status_code=404)
        candidate = (static_dir / full_path).resolve()
        if full_path and candidate.is_file() and Path(os.path.commonpath([candidate, static_dir.resolve()])) == static_dir.resolve():
            return FileResponse(candidate)
        if index.exists():
            return FileResponse(index)
        return JSONResponse({"detail": "Frontend nicht gebaut. API unter /api/docs."}, status_code=404)

    return app


app = create_app()
