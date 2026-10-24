from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db, session_scope
from ..models import Library, MediaFile
from ..services.events import bus
from ..services.library import inventory_library, storage_info
from ..services.pathsec import PathSecurityError, allowed_roots, real, validate_library_path
from ..services.settings import load_settings
from .schemas import LibraryCreate, LibraryUpdate

router = APIRouter(prefix="/api/libraries", tags=["Bibliotheken"])


def _lib_dict(db: Session, lib: Library) -> dict:
    counts = db.execute(select(func.count(), func.coalesce(func.sum(MediaFile.size), 0))
                        .where(MediaFile.library_id == lib.id, MediaFile.present.is_(True))).one()
    return {
        "id": lib.id, "name": lib.name, "path": lib.path, "enabled": lib.enabled,
        "exclude_patterns": lib.exclude_patterns or [], "created_at": lib.created_at,
        "last_inventory_at": lib.last_inventory_at, "last_inventory_stats": lib.last_inventory_stats,
        "file_count": counts[0], "total_size": int(counts[1]), "storage": storage_info(lib.path),
        "reachable": os.path.isdir(lib.path),
    }


@router.get("")
def list_libraries(db: Session = Depends(get_db)):
    return [_lib_dict(db, lib) for lib in db.scalars(select(Library).order_by(Library.name))]


@router.get("/roots")
def media_roots():
    return [{"path": r, "storage": storage_info(r), "exists": os.path.isdir(r)} for r in allowed_roots()]


@router.get("/browse")
def browse(path: str = Query(..., max_length=4096)):
    """Listet Unterverzeichnisse (keine Dateien) innerhalb der Medienwurzeln – zur Auswahl eines Bibliothekspfads."""
    try:
        resolved = validate_library_path(path)
    except PathSecurityError as exc:
        raise HTTPException(400, str(exc)) from exc
    dirs = []
    try:
        for e in sorted(os.scandir(resolved), key=lambda x: x.name.lower()):
            if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                dirs.append({"name": e.name, "path": os.path.join(resolved, e.name)})
    except OSError as exc:
        raise HTTPException(400, f"Verzeichnis nicht lesbar: {exc.strerror}") from exc
    parent = os.path.dirname(resolved)
    roots = allowed_roots()
    can_up = any(os.path.commonpath([parent, r]) == r for r in roots) and resolved not in roots
    return {"path": resolved, "parent": parent if can_up else None, "directories": dirs[:2000]}


@router.post("", status_code=201)
def create_library(body: LibraryCreate, db: Session = Depends(get_db)):
    try:
        path = validate_library_path(body.path)
    except PathSecurityError as exc:
        raise HTTPException(400, str(exc)) from exc
    for other in db.scalars(select(Library)):
        o = real(other.path)
        if os.path.commonpath([o, path]) in (o, path):
            raise HTTPException(409, f"Überschneidet sich mit der Bibliothek '{other.name}' ({other.path})")
    lib = Library(name=body.name.strip(), path=path, exclude_patterns=[p for p in body.exclude_patterns if p.strip()],
                  enabled=body.enabled)
    db.add(lib)
    try:
        db.flush()
    except IntegrityError as exc:
        raise HTTPException(409, "Bibliothek existiert bereits") from exc
    bus.stats_changed(force=True)
    return _lib_dict(db, lib)


@router.patch("/{lib_id}")
def update_library(lib_id: int, body: LibraryUpdate, db: Session = Depends(get_db)):
    lib = db.get(Library, lib_id)
    if lib is None:
        raise HTTPException(404, "Bibliothek nicht gefunden")
    if body.name is not None:
        lib.name = body.name.strip()
    if body.exclude_patterns is not None:
        lib.exclude_patterns = [p.strip() for p in body.exclude_patterns if p.strip()]
    if body.enabled is not None:
        lib.enabled = body.enabled
    return _lib_dict(db, lib)


@router.delete("/{lib_id}")
def delete_library(lib_id: int, db: Session = Depends(get_db)):
    """Entfernt die Bibliothek und ihre Prüfergebnisse aus der Datenbank. Mediendateien werden NICHT angetastet."""
    lib = db.get(Library, lib_id)
    if lib is None:
        raise HTTPException(404, "Bibliothek nicht gefunden")
    db.delete(lib)
    bus.stats_changed(force=True)
    return {"deleted": lib_id}


@router.post("/{lib_id}/inventory")
async def run_inventory(lib_id: int):
    """Bestandsaufnahme ohne Prüfung (liest nur Verzeichnisse und Dateiattribute)."""
    def work():
        with session_scope() as db:
            lib = db.get(Library, lib_id)
            if lib is None:
                raise HTTPException(404, "Bibliothek nicht gefunden")
            settings = load_settings(db)
            return inventory_library(db, lib, settings).to_dict()

    try:
        stats = await asyncio.to_thread(work)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from exc
    bus.stats_changed(force=True)
    return stats
