"""Prüfung einer einzelnen Datei mit den gewählten Methoden.

Ablauf:
1. Pfad validieren (innerhalb der Bibliothek, kein Symlink-Ausbruch) und
   Zugriffsrechte prüfen.
2. Dateikennung (Größe, mtime, Inode) vor der Prüfung festhalten.
3. FFprobe (immer, liefert Streams und Laufzeit), dann die Methoden.
4. Dateikennung nach der Prüfung vergleichen – geänderte Dateien werden als
   "nicht vollständig geprüft" markiert.
5. Ergebnisse fortlaufend in SQLite speichern und den Dateistatus neu berechnen.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from sqlalchemy import select, update

from ..config import get_config
from ..database import session_scope
from ..models import DetectedError, Library, MediaFile, MediaStream, ScanResult, utcnow
from ..scanners.base import SCANNER_VERSIONS, Finding, Method, MethodResult, Outcome, Severity, TimePrecision
from ..scanners.ffmpeg import DecodeOptions, run_ffmpeg_check
from ..scanners.ffprobe import (
    ProbeInfo, analyze_mkvmerge_identify, analyze_probe, run_ffprobe, run_mkvmerge_identify,
)
from ..scanners.matroska import ContainerOptions, run_container_check, time_range_for_offset
from ..scanners.process import ResourceLimits, ScanControl
from ..scanners.tools import get_toolset
from .fingerprint import full_hash
from .library import mark_changed
from .pathsec import PathSecurityError, resolve_media_file
from .settings import AppSettings
from .status import recompute_file_status

log = logging.getLogger(__name__)

METHOD_WEIGHTS = {"quick": 0.02, "container": 1.0, "full": 1.0}


@dataclass
class FileProgress:
    file_id: int
    filename: str
    size: int
    method: str | None = None
    fraction: float = 0.0
    started: float = field(default_factory=time.monotonic)
    findings: int = 0


@dataclass
class FileScanOutcome:
    file_id: int
    status: str
    results: list[int]
    findings: int
    errors: int
    warnings: int
    skipped: bool = False
    message: str | None = None
    cancelled: bool = False


def method_needed(db, mf: MediaFile, method: str) -> bool:
    """True, wenn die Methode für die aktuelle Dateiversion noch nicht vollständig gelaufen ist."""
    r = db.scalars(
        select(ScanResult).where(
            ScanResult.file_id == mf.id, ScanResult.method == method, ScanResult.is_current.is_(True),
            ScanResult.fingerprint_version == mf.fingerprint_version,
        ).order_by(ScanResult.id.desc()).limit(1)
    ).first()
    if r is None:
        return True
    if r.scanner_version != SCANNER_VERSIONS[Method(method)]:
        return True
    # "partial" = alle prüfbaren Streams geprüft, Rest nicht unterstützt: eine Wiederholung ändert nichts
    return r.outcome not in ("completed", "partial")


def _limits(s: AppSettings) -> ResourceLimits:
    return ResourceLimits(nice=s.nice_level, io_class=s.io_priority, stall_timeout=s.stall_timeout_s)


def _decode_opts(s: AppSettings, log_path: str | None) -> DecodeOptions:
    return DecodeOptions(threads=s.ffmpeg_threads, hwaccel=s.hwaccel, strict=s.strict_decode,
                         readrate=s.read_rate_limit, max_findings=s.max_findings_per_result, log_path=log_path,
                         duration_tolerance_s=s.duration_tolerance_s, duration_tolerance_pct=s.duration_tolerance_pct)


def _container_opts(s: AppSettings, also_full: bool) -> ContainerOptions:
    return ContainerOptions(
        run_ebml=s.container_ebml, run_mkvmerge=s.container_mkvmerge,
        # Bei gleichzeitiger Decodierprüfung durchläuft FFmpeg den Demuxer ohnehin vollständig.
        run_ffmpeg_demux=s.container_ffmpeg_demux and not also_full,
        remux_check=s.container_remux_check, tmp_dir=str(get_config().tmp_dir),
        ts_jump_warning=s.ts_jump_warning_s, ts_jump_error=s.ts_jump_error_s, track_gap_warning=s.track_gap_warning_s,
        duration_tolerance_s=s.duration_tolerance_s, duration_tolerance_pct=s.duration_tolerance_pct,
        max_findings=s.max_findings_per_result,
    )


def _store_probe(db, mf: MediaFile, probe: ProbeInfo) -> None:
    mf.container_format = probe.format_long_name or probe.format_name
    mf.duration = probe.duration
    mf.bit_rate = probe.bit_rate
    mf.probed_at = utcnow()
    video = next((s for s in probe.streams if s.type == "video" and not s.attached_pic), None)
    mf.video_codec = video.codec_name if video else None
    mf.video_resolution = f"{video.width}x{video.height}" if video and video.width and video.height else None
    audio = [s.codec_name or "?" for s in probe.streams if s.type == "audio"]
    mf.audio_codecs = ", ".join(dict.fromkeys(audio))[:250] or None
    for old in list(mf.streams):
        db.delete(old)
    db.flush()
    for s in probe.streams:
        db.add(MediaStream(
            file_id=mf.id, index=s.index, type=s.type, codec_name=s.codec_name, codec_long_name=s.codec_long_name,
            profile=s.profile, width=s.width, height=s.height, fps=s.fps, pix_fmt=s.pix_fmt, bit_depth=s.bit_depth,
            channels=s.channels, channel_layout=s.channel_layout, sample_rate=s.sample_rate, language=s.language,
            title=s.title, bit_rate=s.bit_rate, duration=s.duration or s.tag_duration, is_default=s.is_default,
            forced=s.forced, attached_pic=s.attached_pic, decodable=s.decodable,
        ))


def _save_result(job_id: int | None, file_id: int, fp_version: int, size: int, mtime_ns: int,
                 mres: MethodResult, started_at: datetime, duration_s: float) -> tuple[int, int, int, int]:
    """Speichert ein Methodenergebnis. Rückgabe (result_id, findings, errors, warnings)."""
    status = mres.status()
    with session_scope() as db:
        mf = db.get(MediaFile, file_id)
        if mf is None:
            return 0, 0, 0, 0
        prev_completed = db.scalars(
            select(ScanResult).where(ScanResult.file_id == file_id, ScanResult.method == mres.method.value,
                                     ScanResult.is_current.is_(True), ScanResult.outcome == "completed",
                                     ScanResult.fingerprint_version == fp_version)).first()
        # Ein abgebrochener Lauf ohne Befund verdrängt kein vollständiges früheres Ergebnis.
        becomes_current = fp_version == mf.fingerprint_version and not (
            mres.outcome in (Outcome.ABORTED, Outcome.INTERRUPTED) and prev_completed is not None
            and status == "incomplete")
        if becomes_current:
            db.execute(update(ScanResult).where(ScanResult.file_id == file_id, ScanResult.method == mres.method.value)
                       .values(is_current=False))
        counts = {"critical": 0, "error": 0, "warning": 0}
        for f in mres.findings:
            if f.severity.value in counts:
                counts[f.severity.value] += 1
        r = ScanResult(
            file_id=file_id, job_id=job_id, method=mres.method.value, scanner_version=SCANNER_VERSIONS[mres.method],
            outcome=mres.outcome.value, status=status, is_current=becomes_current, fingerprint_version=fp_version,
            file_size=size, file_mtime_ns=mtime_ns, started_at=started_at, finished_at=utcnow(),
            duration_s=round(duration_s, 3), critical_count=counts["critical"], error_count=counts["error"],
            warning_count=counts["warning"], summary=mres.summary or None, tools=mres.tools, details=mres.details,
            decoded_duration=mres.decoded_duration, container_duration=mres.container_duration,
            log_path=mres.log_path, raw_message_count=mres.raw_message_count,
        )
        db.add(r)
        db.flush()
        for f in mres.findings:
            db.add(DetectedError(
                result_id=r.id, file_id=file_id, method=mres.method.value, error_type=f.error_type,
                severity=f.severity.value, heuristic=f.heuristic, stream_index=f.stream_index,
                stream_type=f.stream_type, description=f.description[:4000],
                time_start=f.time_start, time_end=f.time_end, time_precision=f.time_precision.value,
                time_source=f.time_source, byte_offset=f.byte_offset, byte_end=f.byte_end, tool=f.tool,
                raw_message=(f.raw_message or "")[:20000] or None, occurrences=f.occurrences, extra=f.extra or {},
            ))
        db.flush()
        recompute_file_status(db, mf)
        return r.id, len(mres.findings), counts["critical"] + counts["error"], counts["warning"]


def _simple_result(method: Method, outcome: Outcome, finding: Finding) -> MethodResult:
    return MethodResult(method=method, outcome=outcome, findings=[finding])


async def scan_file(
    file_id: int,
    methods: list[str],
    settings: AppSettings,
    control: ScanControl,
    job_id: int | None = None,
    force: bool = False,
    progress: Callable[[FileProgress], None] | None = None,
    test_hooks: dict | None = None,
) -> FileScanOutcome:
    toolset = get_toolset()
    cfg = get_config()
    hooks = test_hooks or {}

    with session_scope() as db:
        mf = db.get(MediaFile, file_id)
        if mf is None:
            return FileScanOutcome(file_id, "unchecked", [], 0, 0, 0, skipped=True, message="Datei nicht in der Datenbank")
        lib = db.get(Library, mf.library_id)
        lib_path = lib.path if lib else ""
        path = mf.path
        filename = mf.filename
        todo = [m for m in ("quick", "container", "full") if m in methods and (force or method_needed(db, mf, m))]
        fp_version = mf.fingerprint_version
        known_size, known_mtime = mf.size, mf.mtime_ns

    fp = FileProgress(file_id=file_id, filename=filename, size=known_size)
    if not todo:
        return FileScanOutcome(file_id, "skipped", [], 0, 0, 0, skipped=True,
                               message="Bereits mit denselben Methoden vollständig geprüft")

    started_at = utcnow()
    t0 = time.monotonic()
    result_ids: list[int] = []
    total_findings = total_errors = total_warnings = 0

    def store(mres: MethodResult, size: int, mtime: int, t_start: float) -> None:
        nonlocal total_findings, total_errors, total_warnings
        rid, nf, ne, nw = _save_result(job_id, file_id, fp_version, size, mtime, mres, started_at,
                                       time.monotonic() - t_start - control.paused_seconds)
        result_ids.append(rid)
        total_findings += nf
        total_errors += ne
        total_warnings += nw

    def fail_all(outcome: Outcome, finding_factory) -> FileScanOutcome:
        for m in todo:
            store(_simple_result(Method(m), outcome, finding_factory()), known_size, known_mtime, t0)
        with session_scope() as db:
            mf2 = db.get(MediaFile, file_id)
            st = mf2.status if mf2 else "incomplete"
        return FileScanOutcome(file_id, st, result_ids, total_findings, total_errors, total_warnings)

    # 1. Pfad und Zugriff
    try:
        real_path = resolve_media_file(lib_path, path)
    except PathSecurityError as exc:
        why = str(exc)
        return fail_all(Outcome.PERMISSION_DENIED, lambda: Finding(
            "permission_denied", Severity.INCOMPLETE, f"Pfad abgelehnt: {why}", "scanner"))
    try:
        st_before = os.stat(real_path)
    except FileNotFoundError:
        with session_scope() as db:
            mf = db.get(MediaFile, file_id)
            if mf:
                mf.present = False
                mf.missing_since = utcnow()
        return FileScanOutcome(file_id, "missing", [], 0, 0, 0, skipped=True, message="Datei nicht mehr vorhanden")
    except PermissionError:
        return fail_all(Outcome.PERMISSION_DENIED, lambda: Finding(
            "permission_denied", Severity.INCOMPLETE, "Keine Berechtigung, die Datei zu lesen.", "scanner"))

    # Datei seit der Bestandsaufnahme verändert? -> neue Version anlegen
    if st_before.st_size != known_size or st_before.st_mtime_ns != known_mtime:
        with session_scope() as db:
            mf = db.get(MediaFile, file_id)
            mark_changed(db, mf, st_before.st_size, st_before.st_mtime_ns, st_before.st_ino)
            fp_version = mf.fingerprint_version
            known_size, known_mtime = mf.size, mf.mtime_ns
        fp.size = known_size

    try:
        fd = os.open(real_path, os.O_RDONLY)
        os.close(fd)
    except PermissionError:
        return fail_all(Outcome.PERMISSION_DENIED, lambda: Finding(
            "permission_denied", Severity.INCOMPLETE,
            "Keine Leseberechtigung für die Datei (Container-Benutzer prüfen: PUID/PGID).", "scanner"))
    except OSError as exc:
        reason = exc.strerror
        return fail_all(Outcome.COMPLETED, lambda: Finding(
            "file_unreadable", Severity.CRITICAL, f"Die Datei kann nicht geöffnet werden: {reason}", "scanner"))

    limits = _limits(settings)
    weights = {m: METHOD_WEIGHTS[m] for m in todo}
    wsum = sum(weights.values())
    done_weight = 0.0

    def make_progress(method: str) -> Callable[[float], None]:
        def cb(frac: float) -> None:
            fp.method = method
            fp.fraction = min((done_weight + weights[method] * frac) / wsum, 1.0)
            if progress:
                progress(fp)
        return cb

    # 2. FFprobe (immer)
    fp.method = "probe"
    if progress:
        progress(fp)
    t_probe = time.monotonic()
    probe, _, cancelled = await run_ffprobe(real_path, toolset, control, limits)
    if cancelled:
        return FileScanOutcome(file_id, "cancelled", [], 0, 0, 0, cancelled=True)
    if probe.ok:
        with session_scope() as db:
            mf = db.get(MediaFile, file_id)
            _store_probe(db, mf, probe)

    method_results: list[tuple[MethodResult, float]] = []

    if "quick" in todo:
        findings = analyze_probe(probe, toolset, st_before.st_size)
        mk = None
        if probe.ok and (probe.is_matroska or real_path.lower().endswith(".mkv")):
            mk = await run_mkvmerge_identify(real_path, toolset, control, limits)
            findings += analyze_mkvmerge_identify(mk, probe)
        outcome = Outcome.COMPLETED
        summary = "Schnellprüfung (Header/Index) – keine vollständige Integritätsprüfung"
        if any(f.error_type == "unsupported_codec" for f in findings):
            outcome = Outcome.PARTIAL
            summary = "Nicht alle Streams prüfbar (Codec nicht unterstützt – kein Defektnachweis)"
        mres = MethodResult(Method.QUICK, outcome, findings,
                            summary=summary,
                            container_duration=probe.duration,
                            tools={"ffprobe": {"version": toolset.ffprobe.version},
                                   **({"mkvmerge -J": {"version": toolset.mkvmerge.version}} if mk is not None else {})},
                            details={"format": probe.format_name, "streams": len(probe.streams)})
        if control.cancelled:
            mres.outcome = Outcome.ABORTED
        method_results.append((mres, t_probe))
        done_weight += weights["quick"]
        make_progress("quick")(0.0)

    if hooks.get("after_probe"):
        await hooks["after_probe"](real_path)

    # Optional: vollständiger SHA-256 (erkennt Inhaltsänderungen bei gleicher Größe/mtime, z. B. Bitrot)
    hash_finding: Finding | None = None
    if settings.hash_mode == "full" and ("container" in todo or "full" in todo) and not control.cancelled:
        def cancel_check() -> bool:
            while control.paused and not control.cancelled:
                time.sleep(0.5)
            return control.cancelled
        try:
            digest = await asyncio.to_thread(full_hash, real_path, cancel_check)
        except InterruptedError:
            digest = None
        except OSError as exc:
            digest = None
            hash_finding = Finding("read_error", Severity.CRITICAL,
                                   f"Lesefehler beim Berechnen der Prüfsumme: {exc.strerror}", "sha256")
        if digest:
            with session_scope() as db:
                mf = db.get(MediaFile, file_id)
                previous = mf.full_hash
                mf.full_hash = digest
            if previous and previous != digest:
                hash_finding = Finding(
                    "file_modified", Severity.ERROR,
                    "Der Dateiinhalt hat sich seit der letzten Prüfung verändert, obwohl Größe und Änderungszeit "
                    "gleich geblieben sind (stille Datenveränderung, z. B. Bitrot oder Speicherfehler).", "sha256",
                    raw_message=f"vorher {previous}\njetzt  {digest}")

    if not probe.ok and ("container" in todo or "full" in todo):
        # Ohne lesbaren Container sind Strukturprüfung (außer EBML) und Decodierung nicht sinnvoll.
        unreadable = Finding("file_unreadable", Severity.CRITICAL,
                             "Die Datei kann von FFprobe nicht geöffnet werden – Container nicht lesbar.", "ffprobe",
                             raw_message=probe.error)
        if "container" in todo:
            t_c = time.monotonic()
            if real_path.lower().endswith((".mkv", ".mka", ".webm")) and not control.cancelled:
                cres = await run_container_check(real_path, probe, toolset, control, limits,
                                                 _container_opts(settings, False), _decode_opts(settings, None),
                                                 make_progress("container"))
                cres.findings.append(unreadable)
            else:
                cres = MethodResult(Method.CONTAINER, Outcome.COMPLETED, [unreadable])
            method_results.append((cres, t_c))
        if "full" in todo:
            method_results.append((MethodResult(Method.FULL, Outcome.COMPLETED, [Finding(
                "file_unreadable", Severity.CRITICAL,
                "Decodierung nicht möglich: Die Datei kann nicht geöffnet werden.", "ffmpeg (Decodierung)",
                raw_message=probe.error)]), time.monotonic()))
    else:
        if "container" in todo and not control.cancelled:
            t_c = time.monotonic()
            cres = await run_container_check(real_path, probe, toolset, control, limits,
                                             _container_opts(settings, "full" in todo),
                                             _decode_opts(settings, None), make_progress("container"))
            method_results.append((cres, t_c))
            done_weight += weights["container"]
        if "full" in todo and not control.cancelled:
            t_f = time.monotonic()
            log_path = None
            if settings.keep_scan_logs:
                log_path = str(cfg.scan_log_dir / f"{file_id}" / f"{int(time.time())}-full.log.gz")
            fres = await run_ffmpeg_check(real_path, probe, toolset, control, limits,
                                          _decode_opts(settings, log_path), mode="decode",
                                          on_progress=make_progress("full"))
            method_results.append((fres, t_f))
            done_weight += weights["full"]

    if hash_finding is not None:
        for r, _ in method_results:
            if r.method == Method.CONTAINER or (r.method == Method.FULL and "container" not in todo):
                r.findings.append(hash_finding)
                break

    # Nicht gestartete Methoden bei Abbruch als abgebrochen festhalten
    ran = {r.method.value for r, _ in method_results}
    if control.cancelled:
        for m in todo:
            if m not in ran:
                method_results.append((MethodResult(Method(m), Outcome.ABORTED, []), time.monotonic()))

    # Strukturbefunde des Decoders über den Clusterindex zeitlich einordnen
    ebml_ci = None
    for r, _ in method_results:
        if r.method == Method.CONTAINER:
            ebml_ci = r.details.get("_cluster_index")
    if ebml_ci:
        for r, _ in method_results:
            if r.method != Method.FULL:
                continue
            for f in r.findings:
                if f.byte_offset is not None and f.time_precision in (TimePrecision.APPROX, TimePrecision.UNKNOWN):
                    s, e = time_range_for_offset(ebml_ci, f.byte_offset)
                    if s is not None:
                        f.time_start, f.time_end, f.time_precision = s, e, TimePrecision.RANGE
                        f.time_source = "Clusterindex (Zeitstempel der umgebenden Cluster)"
    for r, _ in method_results:
        r.details.pop("_cluster_index", None)

    # 3. Datei während der Prüfung verändert?
    if hooks.get("before_final_stat"):
        await hooks["before_final_stat"](real_path)
    try:
        st_after = os.stat(real_path)
        modified = (st_after.st_size != st_before.st_size or st_after.st_mtime_ns != st_before.st_mtime_ns
                    or st_after.st_ino != st_before.st_ino)
    except FileNotFoundError:
        modified = True
        st_after = None
    if modified:
        for r, _ in method_results:
            r.outcome = Outcome.MODIFIED
            r.summary = "Datei wurde während der Prüfung verändert – Ergebnis nicht verwertbar"
            r.findings.append(Finding("file_modified", Severity.INCOMPLETE,
                                      "Die Datei wurde während der Prüfung verändert (Größe/Änderungszeit). "
                                      "Bitte nach Abschluss des Schreibvorgangs erneut prüfen.", "scanner"))
            # Befunde aus einer sich ändernden Datei sind kein belastbarer Defektnachweis
            for f in r.findings:
                if f.severity in (Severity.ERROR, Severity.CRITICAL, Severity.WARNING) and f.error_type != "file_modified":
                    f.severity = Severity.INFO
                    f.description = "[Datei während der Prüfung verändert] " + f.description

    if control.cancelled and control.cancel_reason in ("user", None):
        for r, _ in method_results:
            if r.outcome not in (Outcome.MODIFIED,):
                r.outcome = Outcome.ABORTED
                r.summary = "Prüfung vom Benutzer abgebrochen"
                r.findings.append(Finding("scan_aborted", Severity.INCOMPLETE, "Prüfung vom Benutzer abgebrochen.", "scanner"))
    elif control.cancelled:
        for r, _ in method_results:
            r.outcome = Outcome.INTERRUPTED
            r.summary = f"Prüfung unterbrochen ({control.cancel_reason})"

    for r, t_start in method_results:
        store(r, st_before.st_size, st_before.st_mtime_ns, t_start)

    if modified:
        with session_scope() as db:
            mf = db.get(MediaFile, file_id)
            if mf is not None:
                if st_after is not None:
                    mark_changed(db, mf, st_after.st_size, st_after.st_mtime_ns, st_after.st_ino)
                mf.status = "incomplete"
                mf.status_note = "Datei wurde während der Prüfung verändert – erneute Prüfung erforderlich"

    with session_scope() as db:
        mf = db.get(MediaFile, file_id)
        status = mf.status if mf else "unchecked"
    fp.fraction = 1.0
    if progress:
        progress(fp)
    log.info("Geprüft: %s -> %s (%d Befunde, %.1fs)", filename, status, total_findings, time.monotonic() - t0)
    return FileScanOutcome(file_id, status, result_ids, total_findings, total_errors, total_warnings,
                           cancelled=control.cancelled)


async def scan_file_standalone(file_id: int, methods: list[str], settings: AppSettings, force: bool = True):
    """Hilfsfunktion (CLI/Tests): Datei ohne Auftrag prüfen."""
    control = ScanControl()
    return await scan_file(file_id, methods, settings, control, force=force)


__all__ = ["scan_file", "scan_file_standalone", "method_needed", "FileProgress", "FileScanOutcome"]
