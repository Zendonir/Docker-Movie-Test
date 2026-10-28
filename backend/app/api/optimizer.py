from __future__ import annotations

import asyncio
import shlex
import shutil
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..services.repairability import assess_candidates, failed_repairs, files_in_categories, repaired_files
from ..database import get_db
from ..models import Library, MediaFile, OptimizerItem, OptimizerJob, OptimizerProfile
from ..optimizer import fileops, service
from ..optimizer import worker as optworker
from ..optimizer.languages import language_label
from ..optimizer.profiles import (
    BUILTIN_PROFILES, CODECS, ProfileIn, compute_audio_plan, encoder_for, ffmpeg_encode_cmd, mkvmerge_merge_args,
)
from ..optimizer.rules import FileOps, Rules, TrackRule, collect_choices, compute_plan, global_args, mkvmerge_args
from ..optimizer.worker import add_event
from ..scanners.tools import get_toolset
from ..services.events import bus
from ..services.settings import load_settings, update_settings

router = APIRouter(prefix="/api/optimizer", tags=["MKV-Optimizer"])


def _worker() -> optworker.OptimizerWorker:
    if optworker.worker is None:
        raise HTTPException(503, "Optimizer nicht gestartet")
    return optworker.worker


def ensure_builtin_profiles(db: Session) -> None:
    existing = {p.name for p in db.scalars(select(OptimizerProfile))}
    for p in BUILTIN_PROFILES:
        if p["name"] not in existing:
            db.add(OptimizerProfile(builtin=True, filename_pattern="{name}", **p))


def profile_dict(p: OptimizerProfile) -> dict:
    return {"id": p.id, "name": p.name, "builtin": p.builtin, "codec": p.codec, "codec_label": CODECS[p.codec]["label"],
            "bitrate_kbps": p.bitrate_kbps, "max_channels": p.max_channels, "sample_rate": p.sample_rate,
            "source_codecs": p.source_codecs, "source_languages": p.source_languages, "mode": p.mode,
            "track_name": p.track_name, "filename_pattern": p.filename_pattern,
            "encoder_available": encoder_for(p.codec) is not None}


def _profile_params(p: OptimizerProfile) -> dict:
    d = profile_dict(p)
    d.pop("codec_label", None)
    d.pop("encoder_available", None)
    return d


# ---------------------------------------------------------------- Status
@router.get("/status")
def status(db: Session = Depends(get_db)):
    s = load_settings(db)
    libs = list(db.scalars(select(Library).order_by(Library.name)))
    ts = get_toolset()
    return {
        "tools": {"mkvmerge": ts.mkvmerge.version, "ffmpeg": ts.ffmpeg.version},
        "encoders": {c: encoder_for(c) for c in CODECS},
        "output_root": fileops.output_root(),
        "modes": {k: {"label": v, **fileops.mode_availability([lib.path for lib in libs])[k]}
                  for k, v in fileops.MODES.items()},
        "ops": FileOps.from_settings(s).to_dict(),
        "allow_modify_originals": s.optimizer_allow_modify_originals,
        # ohne gespeicherte Regeln: leer, damit nach dem Suchen die gefundenen Sprachen vorbelegt werden
        "last_rules": s.optimizer_last_rules or Rules(audio=TrackRule(), subtitles=TrackRule()).model_dump(),
        "libraries": [{"id": lib.id, "name": lib.name, "path": lib.path} for lib in libs],
    }


@router.get("/repair-candidates")
def repair_candidates(db: Session = Depends(get_db)):
    """Auswahlliste für Reparaturaufträge (alle reparierbaren Kategorien): offene Dateien und bereits
    reparierte (nicht mehr auswählbar)."""
    libs = {lib.id: lib.name for lib in db.scalars(select(Library))}
    cands = assess_candidates(db, {"repair", "optional", "replace"})
    done = repaired_files(db, [f.id for f, _ in cands])
    failed = failed_repairs(db, [f.id for f, _ in cands if f.id not in done])
    rows = [{"id": f.id, "filename": f.filename, "rel_path": f.rel_path, "library": libs.get(f.library_id),
             "size": f.size, "category": r["category"], "reasons": r["reason_text"], "repaired": done.get(f.id),
             "failed": failed.get(f.id)} for f, r in cands]
    s = load_settings(db)
    out_mode = FileOps.from_settings(s).output_mode
    free = None
    if out_mode == "output_dir":
        try:
            free = shutil.disk_usage(fileops.output_root()).free
        except OSError:
            free = None
    return {"items": rows, "output_mode": out_mode, "output_free": free}


# ---------------------------------------------------------------- Analyse
class AnalyzeIn(BaseModel):
    library_id: int
    directory: str = Field("", max_length=4096)
    recursive: bool = True
    force: bool = False


@router.post("/analyze", status_code=202)
async def start_analyze(body: AnalyzeIn, db: Session = Depends(get_db)):
    if db.get(Library, body.library_id) is None:
        raise HTTPException(404, "Bibliothek nicht gefunden")
    if service.STATE.running:
        raise HTTPException(409, "Es läuft bereits eine Analyse")
    asyncio.create_task(service.analyze(body.library_id, body.directory.strip("/"), body.recursive, body.force))
    await asyncio.sleep(0)
    return service.STATE.to_dict()


@router.get("/analyze")
def analyze_state():
    return service.STATE.to_dict()


@router.post("/analyze/cancel")
def cancel_analyze():
    service.STATE.cancel = True
    if service.STATE.control:
        service.STATE.control.cancel("user")
    return service.STATE.to_dict()


# ---------------------------------------------------------------- Vorschau
class PreviewIn(BaseModel):
    library_id: int
    directory: str = Field("", max_length=4096)
    recursive: bool = True
    only_multi_audio: bool = True
    kind: Literal["remux", "audio"] = "remux"
    rules: Rules | None = None
    profile_id: int | None = None
    only_changed: bool = False
    q: str | None = Field(None, max_length=300)
    page: int = Field(1, ge=1)
    page_size: int = Field(100, ge=1, le=1000)


def _track_dict(t) -> dict:
    return {"id": t.track_id, "type": t.type, "codec": t.codec, "family": t.codec_family, "language": t.language,
            "language_label": language_label(t.language), "name": t.name, "default": t.is_default,
            "forced": t.is_forced, "channels": t.channels}


@router.post("/preview")
def preview(body: PreviewIn, db: Session = Depends(get_db)):
    s = load_settings(db)
    ops = FileOps.from_settings(s)
    profile = None
    if body.kind == "audio":
        p = db.get(OptimizerProfile, body.profile_id) if body.profile_id else None
        if p is None:
            raise HTTPException(400, "Bitte ein Profil wählen")
        profile = _profile_params(p)
    rules = body.rules or Rules()
    if body.kind == "remux" and body.rules is not None and s.optimizer_last_rules != rules.model_dump():
        update_settings(db, {"optimizer_last_rules": rules.model_dump()})  # auch für „Reparieren + Spuren entfernen“
    files = service.mkv_files(db, body.library_id, body.directory, body.recursive)
    if body.q:
        ql = body.q.lower()
        files = [f for f in files if ql in f.name_lower]
    analyses = service.load_analyses(db, files)
    rows = []
    for f in files:
        a = analyses.get(f.id)
        if a is not None and body.only_multi_audio and a.ok and len(a.audio) < 2 and body.kind == "remux":
            continue
        plan = None
        if a is not None:
            plan = (compute_plan(a, rules, ops) if body.kind == "remux" else compute_audio_plan(a, profile)).to_dict()
            plan["changed"] = not plan.get("skip_reason")
        rows.append((f, a, plan))
    if body.only_changed:
        rows = [r for r in rows if r[2] and r[2]["changed"]]
    choices = collect_choices([a for _, a, _ in rows if a is not None and a.ok])
    changed_ids = [f.id for f, _, p in rows if p and p["changed"]]
    # Vorhersage je Datei (nur bekannte Größen); None = nicht vorhersagbar
    predictions = {f.id: p.get("predicted_saved") for f, _, p in rows if p and p["changed"]}
    prediction = {
        "saved": sum(v for v in predictions.values() if v),
        "files_with_unknown": sum(1 for f, _, p in rows if p and p["changed"] and (p.get("predicted_unknown") or 0) > 0),
        "exact": all(p.get("predicted_exact") for _, _, p in rows if p and p["changed"]),
    }
    unanalyzed = sum(1 for _, a, _ in rows if a is None)
    start = (body.page - 1) * body.page_size
    page_rows = rows[start:start + body.page_size]
    return {
        "total": len(rows), "changed_ids": changed_ids, "unanalyzed": unanalyzed, "choices": choices,
        "predictions": predictions, "prediction": prediction,
        "items": [{
            "file_id": f.id, "filename": f.filename, "directory": f.directory, "size": f.size, "status": f.status,
            "analyzed": a is not None, "ok": a.ok if a else None, "error": a.error if a else None,
            "duration": a.duration if a else None, "title": a.title if a else None,
            "tracks": [_track_dict(t) for t in a.tracks] if a else [], "plan": plan,
        } for f, a, plan in page_rows],
    }


class CommandIn(BaseModel):
    file_id: int
    kind: Literal["remux", "audio"] = "remux"
    rules: Rules | None = None
    profile_id: int | None = None


@router.post("/command")
def command_preview(body: CommandIn, db: Session = Depends(get_db)):
    """Zeigt die Befehle, die für eine Datei ausgeführt würden (anstelle der mkvmerge-GUI)."""
    mf = db.get(MediaFile, body.file_id)
    if mf is None:
        raise HTTPException(404, "Datei nicht gefunden")
    a = service.load_analyses(db, [mf]).get(mf.id)
    if a is None:
        raise HTTPException(400, "Datei noch nicht analysiert")
    ops = FileOps.from_settings(load_settings(db))
    ts = get_toolset()
    lib = db.get(Library, mf.library_id)
    try:
        target = fileops.plan_target(mf.path, lib.path, mf.rel_path, ops)
        dest = target.final_path
    except fileops.FileOpError as exc:
        dest = f"<{exc}>"
    if body.kind == "remux":
        plan = compute_plan(a, body.rules or Rules(), ops)
        cmds = [] if plan.skip_reason else [[ts.mkvmerge.path or "mkvmerge", "-o", dest, *global_args(plan, ops),
                                            *mkvmerge_args(plan, a, ops), mf.path]]
        pd = plan.to_dict()
    else:
        p = db.get(OptimizerProfile, body.profile_id) if body.profile_id else None
        if p is None:
            raise HTTPException(400, "Bitte ein Profil wählen")
        prof = _profile_params(p)
        plan = compute_audio_plan(a, prof)
        cmds = []
        if not plan.skip_reason and encoder_for(prof["codec"]):
            tmps = [f"<temp-spur-{c.track_id}.mka>" for c in plan.conversions]
            cmds = [ffmpeg_encode_cmd(mf.path, c.track_id, c, prof, t) for c, t in zip(plan.conversions, tmps)]
            cmds.append([ts.mkvmerge.path or "mkvmerge", "-o", dest,
                         *mkvmerge_merge_args(plan, a, mf.path, tmps, ops.remove_metadata)])
        pd = plan.to_dict()
    return {"plan": pd, "output_mode": ops.output_mode, "destination": dest,
            "commands": [shlex.join(c) for c in cmds], "tracks": [_track_dict(t) for t in a.tracks]}


# ---------------------------------------------------------------- Aufträge
class RepairOpts(BaseModel):
    salvage: bool = False  # auch nicht reparierbare Dateien bergen (Lücken bleiben)
    force: bool = False  # auch Dateien ohne Reparaturbedarf neu verpacken
    strip_tracks: bool = False  # zugleich überflüssige Spuren nach den Optimizer-Regeln entfernen


class JobIn(BaseModel):
    kind: Literal["remux", "audio", "repair"]
    file_ids: list[int] = Field(default_factory=list, max_length=100000)
    category: Literal["repair", "replace", "repair_and_replace"] | None = None  # Auswahl über die Reparaturliste
    repair: RepairOpts | None = None
    rules: Rules | None = None
    profile_id: int | None = None
    name: str | None = Field(None, max_length=300)


def job_dict(j: OptimizerJob) -> dict:
    return {"id": j.id, "kind": j.kind, "name": j.name, "state": j.state, "pause_requested": j.pause_requested,
            "cancel_requested": j.cancel_requested, "created_at": j.created_at, "started_at": j.started_at,
            "finished_at": j.finished_at, "total_files": j.total_files, "done_files": j.done_files,
            "skipped_files": j.skipped_files, "failed_files": j.failed_files, "total_bytes": j.total_bytes,
            "done_bytes": j.done_bytes, "saved_bytes": j.saved_bytes, "resume_count": j.resume_count,
            "output_mode": (j.params.get("ops") or {}).get("output_mode"), "message": j.message,
            "predicted_saved": j.params.get("predicted_saved"),
            "predicted_unknown_files": j.params.get("predicted_unknown_files", 0)}


@router.post("/jobs", status_code=201)
def create_job(body: JobIn, db: Session = Depends(get_db)):
    s = load_settings(db)
    ops = FileOps.from_settings(s)
    libs = list(db.scalars(select(Library)))
    avail = fileops.mode_availability([lib.path for lib in libs])[ops.output_mode]
    if not avail["available"]:
        raise HTTPException(400, f"Ausgabemodus „{fileops.MODES[ops.output_mode]}“ nicht möglich: {avail['reason']}")
    if ops.output_mode in fileops.REPLACE_MODES and not s.optimizer_allow_modify_originals:
        raise HTTPException(400, "Das Ersetzen von Originaldateien ist nicht freigegeben "
                                 "(Einstellungen → MKV-Optimizer → „Veränderung von Originalen erlauben“).")
    tools = get_toolset()
    if not tools.mkvmerge.available:
        raise HTTPException(400, "MKVToolNix (mkvmerge) ist nicht installiert")
    params: dict = {"ops": ops.to_dict()}
    if body.kind == "repair":
        opts = body.repair or RepairOpts()
        if body.category == "replace" or body.category == "repair_and_replace":
            opts.salvage = True
        params["repair"] = opts.model_dump()
        if opts.strip_tracks:
            rules = body.rules or (Rules(**s.optimizer_last_rules) if s.optimizer_last_rules else None)
            if rules is None:
                raise HTTPException(400, "Noch keine Spurregeln festgelegt (MKV-Optimizer → Spuren entfernen)")
            params["rules"] = rules.model_dump()
        name = body.name or ("Notreparatur" if opts.salvage else "Reparatur")
        if body.category:
            wanted = {"repair": {"repair"}, "replace": {"replace"},
                      "repair_and_replace": {"repair", "replace"}}[body.category]
            ids = files_in_categories(db, wanted)
            if not ids:
                raise HTTPException(400, "Keine Dateien in dieser Reparaturliste")
            done = repaired_files(db, ids)  # bereits reparierte, unveränderte Originale nicht erneut reparieren
            body.file_ids = [i for i in ids if i not in done]
            if not body.file_ids:
                raise HTTPException(400, f"Alle {len(ids)} Datei(en) dieser Liste wurden bereits repariert – "
                                         "die reparierten Fassungen liegen im Ausgabeverzeichnis")
    elif body.kind == "remux":
        rules = body.rules or Rules()
        params["rules"] = rules.model_dump()
        update_settings(db, {"optimizer_last_rules": rules.model_dump()})
        name = body.name or "Spuren optimieren"
    else:
        p = db.get(OptimizerProfile, body.profile_id) if body.profile_id else None
        if p is None:
            raise HTTPException(400, "Bitte ein Profil wählen")
        if not encoder_for(p.codec):
            raise HTTPException(400, f"FFmpeg hat keinen Encoder für {p.codec}")
        params["profile"] = _profile_params(p)
        name = body.name or f"Audiokonvertierung: {p.name}"
    if not body.file_ids:
        raise HTTPException(400, "Keine Dateien ausgewählt")
    ids = sorted(set(body.file_ids))
    files = list(db.scalars(select(MediaFile).where(MediaFile.id.in_(ids), MediaFile.present.is_(True),
                                                   MediaFile.extension == "mkv").order_by(MediaFile.rel_path)))
    if not files:
        raise HTTPException(400, "Keine gültigen MKV-Dateien ausgewählt")
    busy = set(db.scalars(select(OptimizerItem.file_id).join(OptimizerJob).where(
        OptimizerJob.state.in_(optworker.ACTIVE), OptimizerItem.state.in_(("pending", "running")))))
    conflict = [f.filename for f in files if f.id in busy]
    if conflict:
        raise HTTPException(409, f"{len(conflict)} Datei(en) sind bereits in einem aktiven Optimizer-Auftrag, "
                                 f"z. B. {conflict[0]}")
    # Vorhersage aus den zwischengespeicherten Analysen (wird bei der Ausführung neu berechnet)
    analyses = service.load_analyses(db, files) if body.kind != "repair" else {}
    pred_total, pred_unknown = 0, 0
    for f in files if body.kind != "repair" else []:
        a = analyses.get(f.id)
        if a is None:
            pred_unknown += 1
            continue
        pl = compute_plan(a, Rules(**params["rules"]), ops) if body.kind == "remux" else \
            compute_audio_plan(a, params["profile"])
        if pl.skip_reason:
            continue
        pred_total += pl.predicted_saved or 0
        if pl.predicted_unknown or pl.predicted_saved is None:
            pred_unknown += 1
    params["predicted_saved"] = pred_total
    params["predicted_unknown_files"] = pred_unknown
    job = OptimizerJob(kind=body.kind, name=name[:300], state="queued", params=params, total_files=len(files),
                       total_bytes=sum(f.size for f in files), events=[])
    db.add(job)
    db.flush()
    for i, f in enumerate(files):
        db.add(OptimizerItem(job_id=job.id, file_id=f.id, seq=i, state="pending", source_path=f.path, size=f.size))
    add_event(job, f"Auftrag angelegt: {len(files)} Dateien, Ausgabe: {fileops.MODES[ops.output_mode]}")
    db.commit()
    _worker().wake()
    bus.publish("optimizer", {"job": job.id})
    return job_dict(job)


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200)):
    total = db.scalar(select(func.count()).select_from(OptimizerJob))
    rows = db.scalars(select(OptimizerJob).order_by(OptimizerJob.id.desc()).offset((page - 1) * page_size)
                      .limit(page_size)).all()
    return {"total": total, "items": [job_dict(j) for j in rows]}


def _job(db: Session, job_id: int) -> OptimizerJob:
    j = db.get(OptimizerJob, job_id)
    if j is None:
        raise HTTPException(404, "Auftrag nicht gefunden")
    return j


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    j = _job(db, job_id)
    counts = dict(db.execute(select(OptimizerItem.state, func.count()).where(OptimizerItem.job_id == j.id)
                             .group_by(OptimizerItem.state)).all())
    return {**job_dict(j), "params": j.params, "item_counts": counts, "events": list(reversed(j.events or []))}


@router.get("/jobs/{job_id}/items")
def job_items(job_id: int, db: Session = Depends(get_db), state: str | None = None, page: int = Query(1, ge=1),
              page_size: int = Query(50, ge=1, le=500)):
    _job(db, job_id)
    q = select(OptimizerItem).where(OptimizerItem.job_id == job_id)
    if state:
        q = q.where(OptimizerItem.state == state)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(OptimizerItem.seq).offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "items": [{
        "id": it.id, "file_id": it.file_id, "name": it.source_path.rsplit("/", 1)[-1], "source_path": it.source_path,
        "state": it.state, "size": it.size, "output_path": it.output_path, "output_size": it.output_size,
        "backup_path": it.backup_path, "message": it.message, "duration_s": it.duration_s, "attempts": it.attempts,
        "predicted_saved": (it.plan or {}).get("predicted_saved"),
        "predicted_exact": (it.plan or {}).get("predicted_exact", False),
        "finished_at": it.finished_at} for it in rows]}


@router.get("/items/{item_id}")
def item_detail(item_id: int, db: Session = Depends(get_db)):
    it = db.get(OptimizerItem, item_id)
    if it is None:
        raise HTTPException(404, "Eintrag nicht gefunden")
    return {"id": it.id, "job_id": it.job_id, "file_id": it.file_id, "state": it.state, "source_path": it.source_path,
            "output_path": it.output_path, "backup_path": it.backup_path, "size": it.size, "output_size": it.output_size,
            "plan": it.plan, "message": it.message, "log": it.log, "duration_s": it.duration_s}


@router.post("/jobs/{job_id}/{action}")
def job_action(job_id: int, action: Literal["pause", "resume", "cancel"], db: Session = Depends(get_db)):
    j = _job(db, job_id)
    if action == "pause":
        if j.state not in ("queued", "running"):
            raise HTTPException(400, "Auftrag läuft nicht")
        j.pause_requested = True
        j.state = "paused"
        add_event(j, "Pausiert")
    elif action == "resume":
        if j.state not in ("paused",):
            raise HTTPException(400, "Auftrag ist nicht pausiert")
        j.pause_requested = False
        j.state = "queued"
        add_event(j, "Fortgesetzt")
    else:
        if j.state not in optworker.ACTIVE:
            raise HTTPException(400, "Auftrag ist bereits beendet")
        j.cancel_requested = True
        add_event(j, "Abbruch angefordert")
    db.commit()
    _worker().wake()
    bus.publish("optimizer", {"job": j.id})
    return job_dict(j)


@router.get("/live")
async def live():
    return await asyncio.to_thread(_worker().snapshot)


# ---------------------------------------------------------------- Profile
@router.get("/profiles")
def list_profiles(db: Session = Depends(get_db)):
    return [profile_dict(p) for p in db.scalars(select(OptimizerProfile).order_by(OptimizerProfile.builtin.desc(),
                                                                                    OptimizerProfile.name))]


@router.post("/profiles", status_code=201)
def create_profile(body: ProfileIn, db: Session = Depends(get_db)):
    p = OptimizerProfile(builtin=False, **body.model_dump())
    db.add(p)
    try:
        db.flush()
    except IntegrityError as exc:
        raise HTTPException(409, "Ein Profil mit diesem Namen existiert bereits") from exc
    return profile_dict(p)


@router.put("/profiles/{pid}")
def update_profile(pid: int, body: ProfileIn, db: Session = Depends(get_db)):
    p = db.get(OptimizerProfile, pid)
    if p is None:
        raise HTTPException(404, "Profil nicht gefunden")
    if p.builtin:
        raise HTTPException(400, "Vordefinierte Profile können nicht geändert werden – bitte kopieren")
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    try:
        db.flush()
    except IntegrityError as exc:
        raise HTTPException(409, "Ein Profil mit diesem Namen existiert bereits") from exc
    return profile_dict(p)


@router.delete("/profiles/{pid}")
def delete_profile(pid: int, db: Session = Depends(get_db)):
    p = db.get(OptimizerProfile, pid)
    if p is None:
        raise HTTPException(404, "Profil nicht gefunden")
    if p.builtin:
        raise HTTPException(400, "Vordefinierte Profile können nicht gelöscht werden")
    db.delete(p)
    return {"deleted": pid}
