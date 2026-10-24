"""Bestandsaufnahme der Medienbibliotheken.

* rekursive Suche (os.scandir, ohne Dateiinhalte zu lesen)
* Identifikation über Pfad, Größe und Änderungszeit (optional Schnell-Hash)
* Erkennung verschobener/umbenannter Dateien
* gelöschte Dateien werden als "nicht mehr vorhanden" markiert (nie gelöscht)
* Symlinks werden standardmäßig ignoriert, sonst nur innerhalb der Bibliothek verfolgt
"""

from __future__ import annotations

import fnmatch
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import Library, MediaFile, ScanResult, utcnow
from .fingerprint import quick_hash
from .pathsec import is_within, real
from .settings import AppSettings

log = logging.getLogger(__name__)


@dataclass
class FoundFile:
    rel_path: str
    abs_path: str
    size: int
    mtime_ns: int
    inode: int


@dataclass
class InventoryStats:
    library_id: int
    directories: int = 0
    files_found: int = 0
    new: int = 0
    changed: int = 0
    moved: int = 0
    missing: int = 0
    reappeared: int = 0
    unchanged: int = 0
    excluded: int = 0
    symlinks_skipped: int = 0
    unreadable_dirs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    aborted: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["unreadable_dirs"] = d["unreadable_dirs"][:50]
        return d


def _excluded(name: str, rel_path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel_path, pat):
            return True
    return False


def walk_library(root: str, settings: AppSettings, extra_excludes: list[str],
                 cancel_check: Callable[[], bool] | None = None) -> tuple[list[FoundFile], InventoryStats]:
    stats = InventoryStats(library_id=0)
    root_real = real(root)
    patterns = list(settings.exclude_patterns) + list(extra_excludes or [])
    exts = {"." + e.lower() for e in settings.extensions}
    found: list[FoundFile] = []
    visited: set[tuple[int, int]] = set()
    try:
        st = os.stat(root_real)
        visited.add((st.st_dev, st.st_ino))
    except OSError as exc:
        raise FileNotFoundError(f"Bibliothekspfad nicht erreichbar: {root} ({exc})") from exc

    stack: list[tuple[str, str]] = [(root_real, "")]
    while stack:
        if cancel_check and cancel_check():
            stats.aborted = True
            break
        abs_dir, rel_dir = stack.pop()
        stats.directories += 1
        try:
            entries = list(os.scandir(abs_dir))
        except OSError as exc:
            stats.unreadable_dirs.append(rel_dir or "/")
            log.warning("Verzeichnis nicht lesbar: %s (%s)", abs_dir, exc)
            continue
        entries.sort(key=lambda e: e.name)
        for entry in entries:
            name = entry.name
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if _excluded(name, rel, patterns):
                stats.excluded += 1
                continue
            try:
                is_link = entry.is_symlink()
                if is_link:
                    if not settings.follow_symlinks:
                        stats.symlinks_skipped += 1
                        continue
                    target = real(entry.path)
                    if not is_within(target, root_real):
                        stats.symlinks_skipped += 1
                        stats.warnings.append(f"Symlink außerhalb der Bibliothek ignoriert: {rel}")
                        continue
                if entry.is_dir(follow_symlinks=is_link):
                    dst = entry.stat(follow_symlinks=is_link)
                    key = (dst.st_dev, dst.st_ino)
                    if key in visited:
                        continue  # Schleife oder doppelter Pfad
                    visited.add(key)
                    stack.append((entry.path, rel))
                    continue
                if not entry.is_file(follow_symlinks=is_link):
                    continue
                if os.path.splitext(name)[1].lower() not in exts:
                    continue
                fst = entry.stat(follow_symlinks=is_link)
                found.append(FoundFile(rel, entry.path, fst.st_size, fst.st_mtime_ns, fst.st_ino))
            except OSError as exc:
                stats.warnings.append(f"Eintrag nicht lesbar: {rel} ({exc.strerror})")
    stats.files_found = len(found)
    return found, stats


def _apply_file_fields(mf: MediaFile, library: Library, ff: FoundFile) -> None:
    mf.library_id = library.id
    mf.rel_path = ff.rel_path
    mf.path = os.path.join(library.path, ff.rel_path)
    mf.filename = os.path.basename(ff.rel_path)
    mf.name_lower = ff.rel_path.lower()
    mf.directory = os.path.dirname(ff.rel_path)
    mf.extension = os.path.splitext(ff.rel_path)[1].lower().lstrip(".")


def mark_changed(db: Session, mf: MediaFile, size: int, mtime_ns: int, inode: int | None) -> None:
    """Neue Dateiversion: alte Ergebnisse gelten nicht mehr für den aktuellen Stand."""
    mf.size = size
    mf.mtime_ns = mtime_ns
    mf.inode = inode
    mf.fingerprint_version = (mf.fingerprint_version or 1) + 1
    mf.changed_at = utcnow()
    mf.status = "unchecked"
    mf.check_level = "none"
    mf.methods_done = []
    mf.error_count = 0
    mf.warning_count = 0
    mf.status_note = "Datei wurde seit der letzten Prüfung verändert"
    mf.quick_hash = None
    mf.full_hash = None
    db.execute(update(ScanResult).where(ScanResult.file_id == mf.id).values(is_current=False))


def _hash_or_none(path: str, size: int, settings: AppSettings) -> str | None:
    if settings.hash_mode == "none":
        return None
    try:
        return quick_hash(path, size)
    except OSError:
        return None


def inventory_library(db: Session, library: Library, settings: AppSettings,
                      cancel_check: Callable[[], bool] | None = None) -> InventoryStats:
    started = time.monotonic()
    found, stats = walk_library(library.path, settings, library.exclude_patterns or [], cancel_check)
    stats.library_id = library.id
    now = utcnow()

    existing: dict[str, MediaFile] = {
        mf.rel_path: mf for mf in db.scalars(select(MediaFile).where(MediaFile.library_id == library.id))
    }
    seen: set[str] = set()
    new_files: list[FoundFile] = []

    for ff in found:
        seen.add(ff.rel_path)
        mf = existing.get(ff.rel_path)
        if mf is None:
            new_files.append(ff)
            continue
        mf.last_seen_at = now
        if not mf.present:
            mf.present = True
            mf.missing_since = None
            stats.reappeared += 1
        if mf.size != ff.size or mf.mtime_ns != ff.mtime_ns:
            mark_changed(db, mf, ff.size, ff.mtime_ns, ff.inode)
            mf.quick_hash = _hash_or_none(ff.abs_path, ff.size, settings)
            stats.changed += 1
        else:
            if mf.quick_hash is None and settings.hash_mode != "none":
                mf.quick_hash = _hash_or_none(ff.abs_path, ff.size, settings)
            stats.unchanged += 1

    # Nicht mehr gefundene Dateien – außer in unlesbaren Verzeichnissen oder bei abgebrochenem Lauf
    vanished: list[MediaFile] = []
    if not stats.aborted:
        unreadable = [d.rstrip("/") for d in stats.unreadable_dirs]
        for rel, mf in existing.items():
            if rel in seen or not mf.present:
                continue
            if any(d in ("", "/") or rel.startswith(d + "/") for d in unreadable):
                continue
            vanished.append(mf)
        present_before = sum(1 for mf in existing.values() if mf.present)
        if found == [] and present_before > 0:
            stats.warnings.append(
                "Keine Dateien gefunden, obwohl die Bibliothek bekannte Dateien enthält – möglicherweise ist das "
                "Dataset nicht eingehängt. Dateien wurden NICHT als fehlend markiert.")
            vanished = []

    # Verschobene/umbenannte Dateien erkennen (gleiche Größe + Änderungszeit [+ Schnell-Hash])
    candidates: dict[tuple[int, int], list[MediaFile]] = {}
    for mf in vanished:
        candidates.setdefault((mf.size, mf.mtime_ns), []).append(mf)
    if new_files:
        sizes = {ff.size for ff in new_files}
        # auch bereits als fehlend markierte Dateien aus allen Bibliotheken berücksichtigen
        for chunk_start in range(0, len(sizes), 500):
            chunk = list(sizes)[chunk_start:chunk_start + 500]
            for mf in db.scalars(select(MediaFile).where(MediaFile.present.is_(False), MediaFile.size.in_(chunk))):
                lst = candidates.setdefault((mf.size, mf.mtime_ns), [])
                if mf not in lst:
                    lst.append(mf)

    moved_ids: set[int] = set()
    for ff in new_files:
        lst = candidates.get((ff.size, ff.mtime_ns)) or []
        match = None
        if lst:
            qh = _hash_or_none(ff.abs_path, ff.size, settings)
            for cand in lst:
                if cand.id in moved_ids:
                    continue
                if settings.hash_mode != "none" and cand.quick_hash and qh and cand.quick_hash != qh:
                    continue
                match = cand
                break
            if match is not None:
                moved_ids.add(match.id)
                old_path = match.path
                _apply_file_fields(match, library, ff)
                match.moved_from = old_path
                match.present = True
                match.missing_since = None
                match.last_seen_at = now
                match.inode = ff.inode
                if qh:
                    match.quick_hash = qh
                stats.moved += 1
                continue
        mf = MediaFile(size=ff.size, mtime_ns=ff.mtime_ns, inode=ff.inode, first_seen_at=now, last_seen_at=now,
                       status="unchecked", check_level="none", methods_done=[], fingerprint_version=1, present=True)
        _apply_file_fields(mf, library, ff)
        mf.quick_hash = _hash_or_none(ff.abs_path, ff.size, settings)
        db.add(mf)
        stats.new += 1

    for mf in vanished:
        if mf.id in moved_ids:
            continue
        mf.present = False
        mf.missing_since = now
        stats.missing += 1

    stats.duration_s = round(time.monotonic() - started, 3)
    library.last_inventory_at = now
    library.last_inventory_stats = stats.to_dict()
    db.flush()
    return stats


def storage_info(path: str) -> dict | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return {"total": total, "free": free, "used": total - st.f_bfree * st.f_frsize}
