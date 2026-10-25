"""Zuordnung der Mediendateien zu IMDb-Titeln und Pflege der Bewertungen."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from sqlalchemy import select

from ..database import session_scope
from ..models import Library, MediaFile, MovieInfo, utcnow
from ..services.events import bus
from . import imdb
from .parse import imdb_id_from_names, imdb_id_from_nfo, normalize_title, parse_file

log = logging.getLogger(__name__)

ARTICLES = re.compile(r"^(der|die|das|ein|eine|the|a|an|le|la|les|el|los|il)\s+", re.I)


def sort_key(title: str | None) -> str | None:
    if not title:
        return None
    return ARTICLES.sub("", normalize_title(title)) or normalize_title(title)


@dataclass
class MatchState:
    running: bool = False
    total: int = 0
    done: int = 0
    matched: int = 0
    message: str | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


STATE = MatchState()


def _apply(info: MovieInfo, cand: imdb.Candidate | None) -> None:
    if cand is None:
        info.title = info.original_title = info.genres = info.title_type = None
        info.year = info.runtime_min = info.votes = None
        info.rating = None
        info.title_sort = sort_key(info.parsed_title)
        return
    info.imdb_id = cand.imdb_id
    info.title = cand.title_de or cand.title  # deutscher Titel, falls vorhanden
    original = cand.original_title or cand.title
    info.original_title = original if original != info.title else None
    info.year = cand.year
    info.runtime_min = cand.runtime
    info.genres = cand.genres
    info.title_type = cand.type
    info.rating = cand.rating
    info.votes = cand.votes
    info.title_sort = sort_key(info.parsed_title or cand.title)


def match_file(mf: MediaFile, lib: Library, info: MovieInfo, lookup: imdb.Lookup | None) -> None:
    title, year = parse_file(mf.rel_path)
    info.parsed_title, info.parsed_year = title, year
    info.fingerprint_version = mf.fingerprint_version
    if info.manual_none:
        info.imdb_id, info.match_source, info.match_confidence = None, "manual", "none"
        _apply(info, None)
        return
    if info.manual and info.imdb_id:
        cand = lookup.get(info.imdb_id) if lookup else None
        info.match_source, info.match_confidence = "manual", "exact"
        if cand:
            _apply(info, cand)
        return
    nfo_id, nfo_path = imdb_id_from_nfo(mf.path, lib.path)
    info.nfo_path = nfo_path
    explicit = nfo_id or imdb_id_from_names(mf.rel_path)
    if explicit:
        info.imdb_id = explicit
        info.match_source = "nfo" if nfo_id else "filename"
        info.match_confidence = "exact"
        cand = lookup.get(explicit) if lookup else None
        if cand:
            _apply(info, cand)
        else:
            info.title_sort = sort_key(title)
        return
    if lookup is None:
        info.imdb_id, info.match_source, info.match_confidence = None, None, None
        _apply(info, None)
        return
    best, conf = imdb.best_match(lookup.search(title, year), year)
    info.match_source = "title_year"
    info.match_confidence = conf
    if best is None:
        info.imdb_id = None
        _apply(info, None)
    else:
        _apply(info, best)


def refresh(file_ids: list[int] | None = None, only_missing: bool = False) -> dict:
    """Ordnet Dateien zu (alle, bestimmte oder nur noch nicht zugeordnete). Läuft im Thread."""
    if STATE.running:
        return STATE.to_dict()
    STATE.__init__()  # type: ignore[misc]
    STATE.running = True
    started = time.monotonic()
    lookup = imdb.Lookup() if imdb.status()["available"] else None
    try:
        with session_scope() as db:
            libs = {lib.id: lib for lib in db.scalars(select(Library))}
            q = select(MediaFile).where(MediaFile.present.is_(True))
            if file_ids:
                q = q.where(MediaFile.id.in_(file_ids))
            files = list(db.scalars(q))
            existing = {m.file_id: m for m in db.scalars(select(MovieInfo))}
            if only_missing:
                files = [f for f in files if f.id not in existing
                         or existing[f.id].fingerprint_version != f.fingerprint_version]
            STATE.total = len(files)
            last = 0.0
            for mf in files:
                info = existing.get(mf.id)
                if info is None:
                    info = MovieInfo(file_id=mf.id)
                    db.add(info)
                lib = libs.get(mf.library_id)
                if lib is not None:
                    try:
                        match_file(mf, lib, info, lookup)
                    except Exception:  # pragma: no cover - einzelne Datei darf den Lauf nicht stoppen
                        log.exception("Zuordnung fehlgeschlagen: %s", mf.path)
                info.updated_at = utcnow()
                STATE.done += 1
                if info.rating is not None or info.imdb_id:
                    STATE.matched += 1
                if STATE.done % 500 == 0:
                    db.flush()
                if time.monotonic() - last > 0.5:
                    last = time.monotonic()
                    bus.publish("movies", STATE.to_dict())
        STATE.message = (f"{STATE.done} Dateien zugeordnet, davon {STATE.matched} mit IMDb-Titel "
                         f"({time.monotonic() - started:.1f} s)")
        if lookup is None:
            STATE.message += " – IMDb-Daten fehlen, nur Titel/Jahr aus Dateinamen"
    finally:
        if lookup:
            lookup.close()
        STATE.running = False
        bus.publish("movies", STATE.to_dict())
    return STATE.to_dict()


def set_manual(file_id: int, imdb_id: str | None) -> MovieInfo:
    """Manuelle Zuordnung (imdb_id=None: ausdrücklich keine Zuordnung). Bleibt bei späteren Läufen erhalten."""
    lookup = imdb.Lookup() if imdb.status()["available"] else None
    try:
        with session_scope() as db:
            mf = db.get(MediaFile, file_id)
            if mf is None:
                raise LookupError("Datei nicht gefunden")
            info = db.get(MovieInfo, file_id) or MovieInfo(file_id=file_id)
            db.add(info)
            info.manual = imdb_id is not None
            info.manual_none = imdb_id is None
            info.imdb_id = imdb_id
            lib = db.get(Library, mf.library_id)
            match_file(mf, lib, info, lookup)
            db.flush()
            db.expunge(info)
            return info
    finally:
        if lookup:
            lookup.close()


def reset_manual(file_id: int) -> None:
    with session_scope() as db:
        info = db.get(MovieInfo, file_id)
        if info:
            info.manual = info.manual_none = False
            info.imdb_id = None
    refresh([file_id])
