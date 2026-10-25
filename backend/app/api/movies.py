from __future__ import annotations

import asyncio
import csv
import io
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Library, MediaFile, MediaStream, MovieInfo
from ..movies import imdb, service
from ..optimizer.languages import _ALIASES, language_label, normalize_language
from ..services.exports import _csv_val
from ..services.labels import STATUS_LABELS

router = APIRouter(prefix="/api/movies", tags=["Filme"])

GENRES_DE = {
    "Action": "Action", "Adventure": "Abenteuer", "Animation": "Animation", "Biography": "Biografie",
    "Comedy": "Komödie", "Crime": "Krimi", "Documentary": "Dokumentation", "Drama": "Drama", "Family": "Familie",
    "Fantasy": "Fantasy", "Film-Noir": "Film noir", "History": "Historie", "Horror": "Horror", "Music": "Musik",
    "Musical": "Musical", "Mystery": "Mystery", "Romance": "Romantik", "Sci-Fi": "Science-Fiction", "Sport": "Sport",
    "Thriller": "Thriller", "War": "Krieg", "Western": "Western", "News": "Nachrichten", "Short": "Kurzfilm",
    "Adult": "Erwachsene", "Reality-TV": "Reality-TV", "Talk-Show": "Talkshow", "Game-Show": "Spielshow",
}


def _aliases(code: str) -> list[str]:
    c = normalize_language(code)
    return sorted({c} | {k for k, v in _ALIASES.items() if v == c and k})


def _height():
    return (select(func.max(MediaStream.height))
            .where(MediaStream.file_id == MediaFile.id, MediaStream.type == "video",
                   MediaStream.attached_pic.is_(False))
            .correlate(MediaFile).scalar_subquery())


def _audio_count():
    return (select(func.count()).where(MediaStream.file_id == MediaFile.id, MediaStream.type == "audio")
            .correlate(MediaFile).scalar_subquery())


SORTS = {
    "title": lambda: func.coalesce(MovieInfo.title_sort, MediaFile.name_lower),
    "year": lambda: func.coalesce(MovieInfo.year, MovieInfo.parsed_year),
    "rating": lambda: MovieInfo.rating,
    "votes": lambda: MovieInfo.votes,
    "runtime": lambda: MediaFile.duration,
    "size": lambda: MediaFile.size,
    "resolution": _height,
    "video": lambda: MediaFile.video_codec,
    "audio": _audio_count,
    "added": lambda: MediaFile.first_seen_at,
    "status": lambda: case({"critical": 5, "error": 4, "warning": 3, "incomplete": 2, "unchecked": 1, "ok": 0},
                           value=MediaFile.status, else_=0),
    "genres": lambda: MovieInfo.genres,
}


def _filtered(q, *, search, genre, audio_lang, sub_lang, min_rating, library_id, matched, resolution):
    q = q.where(MediaFile.present.is_(True))
    if library_id is not None:
        q = q.where(MediaFile.library_id == library_id)
    if search:
        for t in [t for t in search.lower().split() if t][:8]:
            like = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            q = q.where(or_(MediaFile.name_lower.like(like, escape="\\"),
                            func.lower(MovieInfo.title).like(like, escape="\\"),
                            func.lower(MovieInfo.original_title).like(like, escape="\\")))
    if genre:
        q = q.where(MovieInfo.genres.like(f"%{genre.replace('%', '')}%"))
    if min_rating is not None:
        q = q.where(MovieInfo.rating >= min_rating)
    if audio_lang:
        q = q.where(exists().where(MediaStream.file_id == MediaFile.id, MediaStream.type == "audio",
                                   func.lower(func.coalesce(MediaStream.language, "und")).in_(_aliases(audio_lang))))
    if sub_lang:
        q = q.where(exists().where(MediaStream.file_id == MediaFile.id, MediaStream.type == "subtitle",
                                   func.lower(func.coalesce(MediaStream.language, "und")).in_(_aliases(sub_lang))))
    if matched == "yes":
        q = q.where(MovieInfo.imdb_id.is_not(None))
    elif matched == "no":
        q = q.where(MovieInfo.imdb_id.is_(None))
    if resolution:
        h = _height()
        q = q.where({"2160": h >= 1700, "1080": and_(h >= 900, h < 1700), "720": and_(h >= 600, h < 900),
                     "sd": h < 600}[resolution])
    return q


def _filters(q: str | None = Query(None, max_length=300), genre: str | None = Query(None, max_length=40),
             audio_lang: str | None = Query(None, max_length=12), sub_lang: str | None = Query(None, max_length=12),
             min_rating: float | None = Query(None, ge=0, le=10), library_id: int | None = None,
             matched: str = Query("all", pattern="^(all|yes|no)$"),
             resolution: str | None = Query(None, pattern="^(2160|1080|720|sd)$")) -> dict:
    return dict(search=q, genre=genre, audio_lang=audio_lang, sub_lang=sub_lang, min_rating=min_rating,
                library_id=library_id, matched=matched, resolution=resolution)


def _streams(db: Session, ids: list[int]) -> dict[int, list[MediaStream]]:
    out: dict[int, list[MediaStream]] = {}
    if ids:
        for s in db.scalars(select(MediaStream).where(MediaStream.file_id.in_(ids)).order_by(MediaStream.index)):
            out.setdefault(s.file_id, []).append(s)
    return out


def _movie_dict(f: MediaFile, m: MovieInfo | None, streams: list[MediaStream], libs: dict[int, str]) -> dict:
    video = next((s for s in streams if s.type == "video" and not s.attached_pic), None)
    audio = [s for s in streams if s.type == "audio"]
    subs = [s for s in streams if s.type == "subtitle"]
    genres = (m.genres or "").split(",") if m and m.genres else []
    return {
        "file_id": f.id, "filename": f.filename, "directory": f.directory, "library": libs.get(f.library_id),
        "path": f.path, "size": f.size, "duration": f.duration, "bit_rate": f.bit_rate, "container": f.container_format,
        "status": f.status, "status_label": STATUS_LABELS.get(f.status, f.status), "added": f.first_seen_at,
        "probed": f.probed_at is not None,
        "title": (m.title if m and m.title else None) or (m.parsed_title if m else None) or f.filename,
        "original_title": m.original_title if m else None,
        "parsed_title": m.parsed_title if m else None,
        "year": (m.year or m.parsed_year) if m else None,
        "imdb_id": m.imdb_id if m else None,
        "imdb_url": f"https://www.imdb.com/title/{m.imdb_id}/" if m and m.imdb_id else None,
        "rating": m.rating if m else None, "votes": m.votes if m else None,
        "runtime_min": m.runtime_min if m else None,
        "genres": [{"value": g, "label": GENRES_DE.get(g, g)} for g in genres if g],
        "match_source": m.match_source if m else None, "match_confidence": m.match_confidence if m else None,
        "manual": bool(m and (m.manual or m.manual_none)),
        "video": None if video is None else {
            "codec": video.codec_name, "profile": video.profile, "width": video.width, "height": video.height,
            "fps": video.fps, "bit_depth": video.bit_depth, "pix_fmt": video.pix_fmt},
        "audio": [{"index": s.index, "language": normalize_language(s.language), "language_label":
                   language_label(normalize_language(s.language)), "codec": s.codec_name, "profile": s.profile,
                   "channels": s.channels, "channel_layout": s.channel_layout, "title": s.title,
                   "default": s.is_default, "bit_rate": s.bit_rate} for s in audio],
        "subtitles": [{"index": s.index, "language": normalize_language(s.language), "codec": s.codec_name,
                       "forced": s.forced, "title": s.title, "default": s.is_default} for s in subs],
    }


_auto_refresh_running = False


async def _auto_refresh_missing() -> None:
    global _auto_refresh_running
    if _auto_refresh_running or service.STATE.running:
        return
    _auto_refresh_running = True
    try:
        await asyncio.to_thread(service.refresh, None, True)
    finally:
        _auto_refresh_running = False


@router.get("")
async def list_movies(db: Session = Depends(get_db), filters: dict = Depends(_filters),
                      sort: str = Query("title"), order: str = Query("asc", pattern="^(asc|desc)$"),
                      page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500)):
    base = _filtered(select(MediaFile, MovieInfo).outerjoin(MovieInfo, MovieInfo.file_id == MediaFile.id), **filters)
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    col = SORTS.get(sort, SORTS["title"])()
    ordering = col.desc().nulls_last() if order == "desc" else col.asc().nulls_last()
    rows = db.execute(base.order_by(ordering, MediaFile.name_lower).offset((page - 1) * page_size)
                      .limit(page_size)).all()
    streams = _streams(db, [f.id for f, _ in rows])
    libs = {lib.id: lib.name for lib in db.scalars(select(Library))}
    unmatched = db.scalar(select(func.count()).select_from(MediaFile).outerjoin(
        MovieInfo, MovieInfo.file_id == MediaFile.id).where(
        MediaFile.present.is_(True), or_(MovieInfo.file_id.is_(None),
                                         MovieInfo.fingerprint_version != MediaFile.fingerprint_version)))
    if unmatched:
        asyncio.create_task(_auto_refresh_missing())
    return {"total": total, "page": page, "page_size": page_size, "pending_matches": unmatched,
            "items": [_movie_dict(f, m, streams.get(f.id, []), libs) for f, m in rows]}


@router.get("/facets")
def facets(db: Session = Depends(get_db)):
    genres: dict[str, int] = {}
    for (g,) in db.execute(select(MovieInfo.genres).join(MediaFile).where(MediaFile.present.is_(True),
                                                                          MovieInfo.genres.is_not(None))):
        for x in g.split(","):
            genres[x] = genres.get(x, 0) + 1
    langs: dict[str, int] = {}
    for lang, n in db.execute(select(MediaStream.language, func.count(func.distinct(MediaStream.file_id)))
                              .join(MediaFile).where(MediaFile.present.is_(True), MediaStream.type == "audio")
                              .group_by(MediaStream.language)):
        k = normalize_language(lang)
        langs[k] = langs.get(k, 0) + n
    sublangs: dict[str, int] = {}
    for lang, n in db.execute(select(MediaStream.language, func.count(func.distinct(MediaStream.file_id)))
                              .join(MediaFile).where(MediaFile.present.is_(True), MediaStream.type == "subtitle")
                              .group_by(MediaStream.language)):
        k = normalize_language(lang)
        sublangs[k] = sublangs.get(k, 0) + n
    total = db.scalar(select(func.count()).select_from(MediaFile).where(MediaFile.present.is_(True)))
    matched = db.scalar(select(func.count()).select_from(MovieInfo).join(MediaFile).where(
        MediaFile.present.is_(True), MovieInfo.imdb_id.is_not(None)))
    rated = db.scalar(select(func.count()).select_from(MovieInfo).join(MediaFile).where(
        MediaFile.present.is_(True), MovieInfo.rating.is_not(None)))
    unprobed = db.scalar(select(func.count()).select_from(MediaFile).where(MediaFile.present.is_(True),
                                                                          MediaFile.probed_at.is_(None)))
    avg = db.scalar(select(func.avg(MovieInfo.rating)).join(MediaFile).where(MediaFile.present.is_(True)))
    return {
        "genres": [{"value": g, "label": GENRES_DE.get(g, g), "count": c} for g, c in sorted(genres.items(),
                                                                                        key=lambda x: -x[1])],
        "audio_languages": [{"value": k, "label": language_label(k), "count": c}
                            for k, c in sorted(langs.items(), key=lambda x: -x[1])],
        "subtitle_languages": [{"value": k, "label": language_label(k), "count": c}
                               for k, c in sorted(sublangs.items(), key=lambda x: -x[1])],
        "total": total, "matched": matched, "rated": rated, "unprobed": unprobed,
        "average_rating": round(avg, 2) if avg else None,
    }


# ------------------------------------------------------------------ IMDb-Daten
@router.get("/imdb")
def imdb_status():
    return {**imdb.status(), "match": service.STATE.to_dict()}


class ImdbUpdateIn(BaseModel):
    download: bool = True
    include_akas: bool = True


@router.post("/imdb/update", status_code=202)
async def imdb_update(body: ImdbUpdateIn):
    if imdb.STATE.running:
        raise HTTPException(409, "Die IMDb-Aktualisierung läuft bereits")

    async def run():
        try:
            await asyncio.to_thread(imdb.run_update, body.include_akas, body.download)
        except Exception:
            return
        await asyncio.to_thread(service.refresh)

    asyncio.create_task(run())
    await asyncio.sleep(0.05)
    return imdb.status()


@router.post("/imdb/cancel")
def imdb_cancel():
    imdb.STATE.cancel = True
    return imdb.status()


@router.post("/match", status_code=202)
async def match_all(only_missing: bool = False):
    if service.STATE.running:
        raise HTTPException(409, "Die Zuordnung läuft bereits")
    asyncio.create_task(asyncio.to_thread(service.refresh, None, only_missing))
    await asyncio.sleep(0.05)
    return service.STATE.to_dict()


@router.get("/{file_id}/candidates")
def candidates(file_id: int, db: Session = Depends(get_db), q: str | None = Query(None, max_length=300),
               year: int | None = Query(None, ge=1870, le=2100)):
    mf = db.get(MediaFile, file_id)
    if mf is None:
        raise HTTPException(404, "Datei nicht gefunden")
    if not imdb.status()["available"]:
        raise HTTPException(400, "Keine IMDb-Daten vorhanden – bitte zuerst herunterladen bzw. importieren")
    info = db.get(MovieInfo, file_id)
    title = q or (info.parsed_title if info else None) or mf.filename
    y = year if year is not None else (info.parsed_year if info and not q else None)
    lookup = imdb.Lookup()
    try:
        cands = lookup.search(title, y, limit=25)
        if not cands and y is not None:
            cands = lookup.search(title, None, limit=25)
        m = re.search(r"tt\d{7,9}", title)
        if m:
            c = lookup.get(m.group(0))
            cands = ([c] if c else []) + cands
    finally:
        lookup.close()
    return {"query": title, "year": y, "items": [c.to_dict() for c in cands]}


class ManualIn(BaseModel):
    imdb_id: str | None = Field(None, pattern=r"^tt\d{7,9}$")


@router.put("/{file_id}/imdb")
def set_imdb(file_id: int, body: ManualIn):
    try:
        info = service.set_manual(file_id, body.imdb_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"imdb_id": info.imdb_id, "title": info.title, "rating": info.rating, "manual": True}


@router.delete("/{file_id}/imdb")
def reset_imdb(file_id: int):
    service.reset_manual(file_id)
    return {"reset": True}


@router.get("/export.csv")
def export_csv(db: Session = Depends(get_db), filters: dict = Depends(_filters), sort: str = Query("title"),
               order: str = Query("asc", pattern="^(asc|desc)$")):
    base = _filtered(select(MediaFile, MovieInfo).outerjoin(MovieInfo, MovieInfo.file_id == MediaFile.id), **filters)
    col = SORTS.get(sort, SORTS["title"])()
    rows = db.execute(base.order_by(col.desc().nulls_last() if order == "desc" else col.asc().nulls_last(),
                                    MediaFile.name_lower)).all()
    streams = _streams(db, [f.id for f, _ in rows])
    libs = {lib.id: lib.name for lib in db.scalars(select(Library))}

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        w.writerow(["Titel", "Originaltitel", "Jahr", "IMDb-Bewertung", "Stimmen", "IMDb-ID", "Genres", "Laufzeit (min)",
                    "Auflösung", "Videocodec", "Tonspuren", "Untertitel", "Größe (Byte)", "Status", "Datei", "Pfad"])
        yield ("﻿" + buf.getvalue()).encode("utf-8")
        for f, m in rows:
            d = _movie_dict(f, m, streams.get(f.id, []), libs)
            buf.seek(0)
            buf.truncate()
            v = d["video"] or {}
            w.writerow([_csv_val(x) for x in [
                d["title"], d["original_title"] or "", d["year"] or "",
                str(d["rating"]).replace(".", ",") if d["rating"] is not None else "", d["votes"] or "",
                d["imdb_id"] or "", ", ".join(g["label"] for g in d["genres"]),
                round(f.duration / 60) if f.duration else "", f"{v.get('width')}x{v.get('height')}" if v else "",
                v.get("codec", ""), " | ".join(f"{a['language'].upper()} {a['codec']} {a['channels'] or ''}ch".strip()
                                               for a in d["audio"]),
                ", ".join(s["language"].upper() for s in d["subtitles"]), f.size, d["status_label"], f.filename, f.path,
            ]])
            yield buf.getvalue().encode("utf-8")

    return StreamingResponse(gen(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename*=UTF-8''filmliste.csv"})
