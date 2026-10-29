"""Bearbeitung einer einzelnen Datei (Remux oder Audiokonvertierung).

Ablauf: Spuren neu analysieren → Plan berechnen → in temporäre Datei schreiben →
Ergebnis prüfen (Spuren, Laufzeit, optional EBML-Strukturprüfung) → erst dann
abschließen (neue Datei anlegen bzw. Original ersetzen). Schlägt ein Schritt fehl,
werden die temporären Dateien gelöscht; das Original bleibt unverändert.
"""

from __future__ import annotations

import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import update

from ..database import session_scope
from ..models import Library, MediaFile, OptimizerItem
from ..scanners.ffprobe import run_ffprobe
from ..scanners.matroska import ContainerOptions, run_ebml_validator
from ..scanners.process import ResourceLimits, ScanControl, run_process
from ..scanners.tools import get_toolset
from ..services.library import mark_changed
from ..services.repairability import assess_file
from ..services.pathsec import PathSecurityError, resolve_media_file
from . import fileops
from .profiles import compute_audio_plan, expected_after_conversion, ffmpeg_encode_cmd, mkvmerge_merge_args
from .rules import FileOps, Rules, compute_plan, global_args, mkvmerge_args
from .service import store_analysis
from .tracks import Analysis, identify

log = logging.getLogger(__name__)

MAX_LOG = 20000


class Skip(Exception):
    pass


class Fail(Exception):
    pass


@dataclass
class ItemResult:
    state: str  # done | skipped | failed | cancelled
    message: str | None = None
    output_path: str | None = None
    backup_path: str | None = None
    output_size: int | None = None
    plan: dict = field(default_factory=dict)
    log: str = ""


class Ctx:
    def __init__(self, item_id: int, control: ScanControl, progress: Callable[[float, str], None]):
        self.item_id = item_id
        self.control = control
        self.progress = progress
        self.lines: list[str] = []
        self.resyncs = 0
        self.temps: list[str] = []
        self.limits = ResourceLimits(nice=10, io_class="best-effort-low", stall_timeout=1800)

    def log(self, msg: str) -> None:
        self.lines.append(msg)

    def register_temp(self, path: str) -> None:
        self.temps.append(path)
        with session_scope() as db:
            db.execute(update(OptimizerItem).where(OptimizerItem.id == self.item_id)
                       .values(temp_paths=list(self.temps)))

    def text(self) -> str:
        t = "\n".join(self.lines)
        return t if len(t) <= MAX_LOG else "…\n" + t[-MAX_LOG:]


async def _run(ctx: Ctx, cmd: list[str], phase: str, lo: float, hi: float, duration: float | None) -> int:
    ctx.log("$ " + shlex.join(cmd))
    tail: list[str] = []
    state = {"t": None}

    def on_line(line: str) -> None:
        if "Resyncing successful" in line:
            ctx.resyncs += 1
        if line.startswith("#GUI#progress"):
            try:
                ctx.progress(lo + (hi - lo) * float(line.split()[-1].rstrip("%")) / 100, phase)
            except ValueError:
                pass
            return
        if line.startswith("out_time_us="):
            try:
                state["t"] = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                pass
            return
        if line.startswith("progress=") and duration and state["t"] is not None:
            ctx.progress(lo + (hi - lo) * min(state["t"] / duration, 1.0), phase)
            return
        if "=" in line and not line.startswith("#") and not line.startswith("["):
            return  # weitere -progress-Schlüssel
        if line.startswith("#GUI#") or "[error]" in line or "[warning]" in line or "[fatal]" in line \
                or line.startswith(("Error", "Warning")):
            tail.append(line)

    res = await run_process(cmd, ctx.control, ctx.limits, on_stdout=on_line, on_stderr=on_line)
    for line in tail[-40:]:
        ctx.log("  " + line)
    if res.cancelled:
        raise Fail("abgebrochen")
    if res.spawn_error:
        raise Fail(res.spawn_error)
    if res.stalled:
        raise Fail(f"{phase}: Werkzeug reagiert nicht mehr")
    return res.returncode if res.returncode is not None else -1


def _signature(a: Analysis) -> list[tuple[str, str | None, str]]:
    return [(t.type, t.codec_family, t.language) for t in a.tracks]


async def _verify(ctx: Ctx, temp: str, expected: list[tuple], source: Analysis, ops: FileOps,
                  lang_check: bool = True) -> Analysis:
    ctx.progress(0.92, "Prüfung")
    out = await identify(temp, ctx.control, ctx.limits)
    if not out.ok:
        raise Fail(f"Ergebnis nicht lesbar: {out.error}")
    got = [(t.type, t.codec_family) + ((t.language,) if lang_check else ()) for t in out.tracks]
    exp = [tuple(e[:3] if lang_check else e[:2]) for e in expected]
    if got != exp:
        raise Fail(f"Spuren der neuen Datei entsprechen nicht dem Plan (erwartet {exp}, erhalten {got})")
    if source.duration and out.duration:
        tol = max(1.0, source.duration * 0.005)
        if abs(source.duration - out.duration) > tol:
            raise Fail(f"Laufzeit weicht ab: Original {source.duration:.2f} s, neu {out.duration:.2f} s")
    if ops.verify_output:
        summary, err, cancelled = await run_ebml_validator(temp, ctx.control, ctx.limits, ContainerOptions(), None)
        if cancelled:
            raise Fail("abgebrochen")
        if err:
            raise Fail(f"Strukturprüfung der neuen Datei fehlgeschlagen: {err}")
        bad = [f for f in summary.get("findings", []) if f["severity"] in ("error", "critical")]
        if bad:
            raise Fail("Strukturprüfung der neuen Datei meldet Fehler: " + "; ".join(f["description"] for f in bad[:3]))
        ctx.log(f"Strukturprüfung der neuen Datei: ohne Fehler ({summary.get('clusters')} Cluster)")
    return out


def _load(item_id: int) -> tuple[OptimizerItem, MediaFile, Library]:
    with session_scope() as db:
        item = db.get(OptimizerItem, item_id)
        if item is None:
            raise Skip("Eintrag nicht gefunden")
        mf = db.get(MediaFile, item.file_id) if item.file_id else None
        if mf is None or not mf.present:
            raise Skip("Datei nicht mehr vorhanden")
        lib = db.get(Library, mf.library_id)
        db.expunge_all()
        return item, mf, lib


async def process_item(item_id: int, kind: str, params: dict, control: ScanControl,
                       progress: Callable[[float, str], None]) -> ItemResult:
    ctx = Ctx(item_id, control, progress)
    ops = FileOps(**params["ops"])
    result = ItemResult(state="failed")
    started = time.monotonic()
    try:
        item, mf, lib = _load(item_id)
        if kind == "repair":
            with session_scope() as db:
                m2 = db.get(MediaFile, mf.id)
                rating = assess_file(db, m2) if m2 is not None else None
            if rating is not None:
                if rating["category"] == "ok" and not params.get("repair", {}).get("force"):
                    raise Skip("Kein Reparaturbedarf laut letzter Prüfung")
                if rating["category"] == "replace" and not params.get("repair", {}).get("salvage"):
                    raise Skip("Nicht mehr reparierbar (Datenverlust) – nur mit „Notreparatur“ zu bergen")
                result.plan = {"assessment": rating["category"], "reasons": rating["reasons"]}
        elif ops.skip_damaged and mf.status in ("critical", "error"):
            raise Skip("Datei hat Integritätsfehler (Status „" + mf.status + "“) – wird nicht bearbeitet")
        try:
            source = resolve_media_file(lib.path, mf.path)
        except PathSecurityError as exc:
            raise Fail(str(exc)) from exc
        try:
            st = os.stat(source)
        except FileNotFoundError as exc:
            raise Skip("Datei nicht mehr vorhanden") from exc
        if not os.access(source, os.R_OK):
            raise Fail("Keine Leseberechtigung")

        progress(0.01, "Analyse")
        analysis = await identify(source, control, ctx.limits)
        if mf.size == st.st_size and mf.mtime_ns == st.st_mtime_ns:
            with session_scope() as db:
                m2 = db.get(MediaFile, mf.id)
                if m2 is not None:
                    store_analysis(db, m2, analysis)
        if not analysis.ok:
            raise Fail(f"Datei nicht analysierbar: {analysis.error}")

        if kind == "repair":
            await _repair(ctx, result, mf, lib, source, st, analysis, params, ops)
        elif kind == "remux":
            await _remux(ctx, result, mf, lib, source, st, analysis, params, ops)
        else:
            await _audio(ctx, result, mf, lib, source, st, analysis, params, ops)

        if result.state == "done" and ops.output_mode in fileops.REPLACE_MODES:
            with session_scope() as db:
                m2 = db.get(MediaFile, mf.id)
                if m2 is not None:
                    nst = os.stat(source)
                    mark_changed(db, m2, nst.st_size, nst.st_mtime_ns, nst.st_ino)
                    what = {"remux": "neu geschrieben", "audio": "Audio konvertiert", "repair": "repariert"}[kind]
                    m2.status_note = f"Durch den MKV-Optimizer verändert ({what}) – erneute Prüfung empfohlen"
    except Skip as exc:
        result.state = "skipped"
        result.message = str(exc)
    except (Fail, fileops.FileOpError) as exc:
        result.state = "cancelled" if control.cancelled else "failed"
        result.message = "Vom Benutzer abgebrochen" if control.cancelled else str(exc)
    except OSError as exc:
        result.state = "failed"
        result.message = f"Dateifehler: {exc}"
    finally:
        fileops.remove_quiet(ctx.temps)
    ctx.log(f"Ergebnis: {result.state}{' – ' + result.message if result.message else ''} "
            f"({time.monotonic() - started:.1f} s)")
    result.log = ctx.text()
    return result


async def _remux(ctx: Ctx, result: ItemResult, mf: MediaFile, lib: Library, source: str, st: os.stat_result,
                 analysis: Analysis, params: dict, ops: FileOps) -> None:
    rules = Rules(**params["rules"])
    plan = compute_plan(analysis, rules, ops)
    result.plan = plan.to_dict()
    if plan.skip_reason:
        raise Skip(plan.skip_reason)
    target = fileops.plan_target(source, lib.path, mf.rel_path, ops)
    fileops.prepare_dir(target, int(st.st_size * 1.05) + 64 * 1024 * 1024)
    temp = fileops.temp_path(target, ctx.item_id)
    ctx.register_temp(temp)
    toolset = get_toolset()
    cmd = [toolset.mkvmerge.path or "mkvmerge", "--ui-language", "en_US", "--gui-mode", "-o", temp,
           *global_args(plan, ops), *mkvmerge_args(plan, analysis, ops), source]
    ctx.log("Entfernt: " + (", ".join(f"#{d['track_id']} ({d['type']})" for d in result.plan["decisions"]
                                       if not d["keep"]) or "keine Spur (nur Remux)"))
    rc = await _run(ctx, cmd, "Remux", 0.03, 0.9, None)
    if rc not in (0, 1):
        raise Fail(f"mkvmerge ist mit Exit-Code {rc} fehlgeschlagen")
    by_id = {t.track_id: t for t in analysis.tracks}
    order = plan.track_order or plan.kept
    expected = [(by_id[i].type, by_id[i].codec_family, by_id[i].language) for i in order]
    await _verify(ctx, temp, expected, analysis, ops)
    info = fileops.finalize(target, temp, source, st)
    ctx.temps.remove(temp)
    result.state = "done"
    result.output_path = info["final"]
    result.backup_path = info.get("backup")
    result.output_size = os.path.getsize(info["final"])
    saved = st.st_size - result.output_size
    if plan.removed:
        result.message = f"{len(plan.removed)} Spur(en) entfernt, {saved / 1e6:.1f} MB eingespart"
    else:
        result.message = f"Remux ohne Spurentfernung, {saved / 1e6:.1f} MB eingespart"
    if ops.remove_metadata:
        result.message += ", Metadaten entfernt"


async def _repair(ctx: Ctx, result: ItemResult, mf: MediaFile, lib: Library, source: str, st: os.stat_result,
                  analysis: Analysis, params: dict, ops: FileOps) -> None:
    """Remux aller Spuren mit mkvmerge: baut Index (Cues), Zeitstempel, Laufzeitangabe und Clusterstruktur neu auf.

    Zerstörte Bereiche überspringt mkvmerge per Resynchronisation – fehlende Daten kommen dadurch nicht zurück,
    die Datei ist danach aber wieder strukturell sauber und spulbar."""
    target = fileops.plan_target(source, lib.path, mf.rel_path, ops)
    fileops.prepare_dir(target, int(st.st_size * 1.05) + 64 * 1024 * 1024)
    temp = fileops.temp_path(target, ctx.item_id)
    ctx.register_temp(temp)
    toolset = get_toolset()
    meta = ["--no-global-tags", "--no-track-tags"] if ops.remove_metadata else []
    pre: list[str] = []
    src_args = meta
    expected = analysis.tracks
    plan = None
    if params.get("rules"):
        # Im selben Durchgang überflüssige Spuren nach den Optimizer-Regeln entfernen
        plan = compute_plan(analysis, Rules(**params["rules"]), ops)
        if plan.skip_reason:
            ctx.log(f"Spuren bleiben unverändert: {plan.skip_reason}")
            plan = None
        else:
            pre, src_args = global_args(plan, ops), mkvmerge_args(plan, analysis, ops)
            by_id = {t.track_id: t for t in analysis.tracks}
            expected = [by_id[i] for i in plan.track_order or plan.kept]
            result.plan = {**(result.plan or {}), **plan.to_dict()}
            ctx.log("Entfernt: " + ", ".join(f"#{i} ({by_id[i].type}, {by_id[i].language})" for i in plan.removed))
    cmd = [toolset.mkvmerge.path or "mkvmerge", "--ui-language", "en_US", "--gui-mode", "-o", temp,
           *pre, *src_args, source]
    rc = await _run(ctx, cmd, "Reparatur (Remux)", 0.03, 0.85, None)
    resyncs = ctx.resyncs
    if rc not in (0, 1):
        raise Fail(f"mkvmerge ist mit Exit-Code {rc} fehlgeschlagen")
    ctx.progress(0.88, "Prüfung")
    out = await identify(temp, ctx.control, ctx.limits)
    if not out.ok:
        raise Fail(f"Ergebnis nicht lesbar: {out.error}")
    # Reihenfolge nicht vergleichen: mkvmerge ordnet die Spuren beim Remux teils um (Video, Audio, Untertitel)
    got = sorted((t.type, t.codec_family or "", t.language) for t in out.tracks)
    exp = sorted((t.type, t.codec_family or "", t.language) for t in expected)
    if got != exp:
        raise Fail(f"Spuren der reparierten Datei stimmen nicht mit dem Original überein (erwartet {exp}, erhalten {got})")
    summary, err, cancelled = await run_ebml_validator(temp, ctx.control, ctx.limits, ContainerOptions(), None)
    if cancelled:
        raise Fail("abgebrochen")
    if err:
        raise Fail(f"Strukturprüfung der reparierten Datei fehlgeschlagen: {err}")
    bad = [f for f in summary.get("findings", []) if f["severity"] in ("error", "critical")]
    if bad:
        raise Fail("Auch die reparierte Datei hat Strukturfehler: " + "; ".join(f["description"] for f in bad[:3]))
    ctx.log(f"Strukturprüfung der reparierten Datei: ohne Fehler ({summary.get('clusters')} Cluster)")
    info = fileops.finalize(target, temp, source, st)
    ctx.temps.remove(temp)
    result.state = "done"
    result.output_path = info["final"]
    result.backup_path = info.get("backup")
    result.output_size = os.path.getsize(info["final"])
    parts = ["Index und Zeitstempel neu aufgebaut, Struktur geprüft"]
    if plan is not None:
        parts.append(f"{len(plan.removed)} Spur(en) entfernt, {(st.st_size - result.output_size) / 1e6:.1f} MB kleiner")
    if analysis.duration and out.duration and abs(analysis.duration - out.duration) > 1:
        parts.append(f"Laufzeit {analysis.duration / 60:.1f} → {out.duration / 60:.1f} min")
    if resyncs:
        parts.append(f"{resyncs} zerstörte(r) Bereich(e) übersprungen – dort fehlen weiterhin Bild/Ton")
    result.message = "; ".join(parts)


async def _audio(ctx: Ctx, result: ItemResult, mf: MediaFile, lib: Library, source: str, st: os.stat_result,
                 analysis: Analysis, params: dict, ops: FileOps) -> None:
    profile = params["profile"]
    plan = compute_audio_plan(analysis, profile)
    result.plan = plan.to_dict()
    if plan.skip_reason:
        raise Skip(plan.skip_reason)
    # FFmpeg-Streamindex muss der mkvmerge-Track-ID entsprechen
    probe, _, cancelled = await run_ffprobe(source, get_toolset(), ctx.control, ctx.limits)
    if cancelled:
        raise Fail("abgebrochen")
    type_map = {"video": "video", "audio": "audio", "subtitles": "subtitle"}
    for t in analysis.tracks:
        s = probe.stream(t.track_id)
        if s is None or (t.type in type_map and s.type != type_map[t.type]):
            raise Fail("Spurzuordnung zwischen mkvmerge und FFmpeg ist nicht eindeutig – Datei wird nicht bearbeitet")
    stem = os.path.splitext(os.path.basename(source))[0]
    filename = (profile.get("filename_pattern") or "{name}").replace("{name}", stem) \
        .replace("{codec}", profile["codec"].upper()).replace("{profile}", profile.get("name", "")) + ".mkv"
    filename = filename.replace("/", "_")
    target = fileops.plan_target(source, lib.path, mf.rel_path, ops, filename=filename)
    fileops.prepare_dir(target, int(st.st_size * 1.3) + 64 * 1024 * 1024)
    n = len(plan.conversions)
    tmp_tracks = []
    for i, conv in enumerate(plan.conversions):
        tmp = fileops.temp_path(target, ctx.item_id, f"-a{conv.track_id}", ".mka")
        ctx.register_temp(tmp)
        lo, hi = 0.02 + 0.6 * i / n, 0.02 + 0.6 * (i + 1) / n
        rc = await _run(ctx, ffmpeg_encode_cmd(source, conv.track_id, conv, profile, tmp),
                        f"Codierung Spur {conv.track_id}", lo, hi, analysis.duration)
        if rc != 0 or not os.path.exists(tmp):
            raise Fail(f"FFmpeg konnte Spur {conv.track_id} nicht codieren (Exit-Code {rc})")
        tmp_tracks.append(tmp)
    temp = fileops.temp_path(target, ctx.item_id)
    ctx.register_temp(temp)
    toolset = get_toolset()
    cmd = [toolset.mkvmerge.path or "mkvmerge", "--ui-language", "en_US", "--gui-mode", "-o", temp,
           *mkvmerge_merge_args(plan, analysis, source, tmp_tracks, ops.remove_metadata)]
    rc = await _run(ctx, cmd, "Zusammenführen", 0.62, 0.9, None)
    if rc not in (0, 1):
        raise Fail(f"mkvmerge ist mit Exit-Code {rc} fehlgeschlagen")
    expected = expected_after_conversion(plan, analysis, profile)
    await _verify(ctx, temp, expected, analysis, ops, lang_check=False)
    info = fileops.finalize(target, temp, source, st)
    ctx.temps.remove(temp)
    result.state = "done"
    result.output_path = info["final"]
    result.backup_path = info.get("backup")
    result.output_size = os.path.getsize(info["final"])
    result.message = f"{n} Spur(en) nach {profile['codec'].upper()} konvertiert ({'ersetzt' if plan.mode == 'replace' else 'hinzugefügt'})"
