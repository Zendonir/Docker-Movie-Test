"""Berichte und Export (CSV, JSON, HTML) – Unicode-sicher, gestreamt für große Bibliotheken."""

from __future__ import annotations

import csv
import html
import io
import json
from datetime import datetime
from typing import Iterator
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..models import DetectedError, Library, MediaFile, ScanJob, ScanJobItem, ScanResult
from .labels import (
    CHECK_LEVEL_LABELS, METHOD_LABELS, OUTCOME_LABELS, PRECISION_LABELS, SEVERITY_LABELS, STATUS_LABELS,
    error_type_label, fmt_position, fmt_size, fmt_time,
)

SCOPES = ("library", "damaged", "warnings", "job", "file", "filter")
BATCH = 500


class ExportError(ValueError):
    pass


def _local(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(ZoneInfo(get_config().tz)).strftime("%d.%m.%Y %H:%M:%S")


def _file_query(scope: str, job_id: int | None, file_id: int | None, library_id: int | None,
                filters: dict | None = None):
    q = select(MediaFile)
    if scope == "filter":
        from .filters import apply_filters
        f = {"search": None, "status": None, "ext": None, "library_id": None, "directory": None, "only_new": False,
             "only_damaged": False, "present": "yes", "check_level": None} | (filters or {})
        q = apply_filters(q, **f)
    elif scope == "library":
        pass
    elif scope == "damaged":
        q = q.where(MediaFile.status.in_(("critical", "error")))
    elif scope == "warnings":
        q = q.where(MediaFile.status == "warning")
    elif scope == "job":
        if job_id is None:
            raise ExportError("job_id fehlt")
        q = q.where(MediaFile.id.in_(select(ScanJobItem.file_id).where(ScanJobItem.job_id == job_id)))
    elif scope == "file":
        if file_id is None:
            raise ExportError("file_id fehlt")
        q = q.where(MediaFile.id == file_id)
    else:
        raise ExportError(f"Unbekannter Exportumfang: {scope}")
    if library_id is not None:
        q = q.where(MediaFile.library_id == library_id)
    return q.order_by(MediaFile.library_id, MediaFile.rel_path)


def _results_for(db: Session, file_ids: list[int], scope: str, job_id: int | None) -> dict[int, list[ScanResult]]:
    if not file_ids:
        return {}
    q = select(ScanResult).where(ScanResult.file_id.in_(file_ids))
    if scope == "job":
        q = q.where(ScanResult.job_id == job_id)
    else:
        q = q.where(ScanResult.is_current.is_(True))
    out: dict[int, list[ScanResult]] = {}
    for r in db.scalars(q.order_by(ScanResult.id)):
        out.setdefault(r.file_id, []).append(r)
    return out


def _errors_for(db: Session, result_ids: list[int]) -> dict[int, list[DetectedError]]:
    if not result_ids:
        return {}
    out: dict[int, list[DetectedError]] = {}
    q = select(DetectedError).where(DetectedError.result_id.in_(result_ids)).order_by(
        DetectedError.time_start.is_(None), DetectedError.time_start, DetectedError.id)
    for e in db.scalars(q):
        out.setdefault(e.result_id, []).append(e)
    return out


def iter_records(db: Session, scope: str, job_id: int | None = None, file_id: int | None = None,
                 library_id: int | None = None, include_info: bool = False,
                 filters: dict | None = None) -> Iterator[dict]:
    libs = {lib.id: lib for lib in db.scalars(select(Library))}
    offset = 0
    base = _file_query(scope, job_id, file_id, library_id, filters)
    while True:
        files = db.scalars(base.offset(offset).limit(BATCH)).all()
        if not files:
            break
        offset += len(files)
        results = _results_for(db, [f.id for f in files], scope, job_id)
        errors = _errors_for(db, [r.id for rs in results.values() for r in rs])
        for f in files:
            rs = results.get(f.id, [])
            findings = []
            for r in rs:
                for e in errors.get(r.id, []):
                    if e.severity == "info" and not include_info:
                        continue
                    findings.append({
                        "method": r.method,
                        "method_label": METHOD_LABELS.get(r.method, r.method),
                        "severity": e.severity,
                        "severity_label": SEVERITY_LABELS.get(e.severity, e.severity),
                        "error_type": e.error_type,
                        "error_type_label": error_type_label(e.error_type),
                        "heuristic": e.heuristic,
                        "stream_index": e.stream_index,
                        "stream_type": e.stream_type,
                        "description": e.description,
                        "time_start": e.time_start,
                        "time_end": e.time_end,
                        "time_precision": e.time_precision,
                        "time_precision_label": PRECISION_LABELS.get(e.time_precision, e.time_precision),
                        "position": fmt_position(e.time_start, e.time_end, e.time_precision),
                        "time_source": e.time_source,
                        "byte_offset": e.byte_offset,
                        "byte_end": e.byte_end,
                        "tool": e.tool,
                        "occurrences": e.occurrences,
                        "raw_message": e.raw_message,
                    })
            yield {
                "id": f.id,
                "filename": f.filename,
                "path": f.path,
                "directory": f.directory,
                "library": libs[f.library_id].name if f.library_id in libs else None,
                "present": f.present,
                "status": f.status,
                "status_label": STATUS_LABELS.get(f.status, f.status),
                "check_level": f.check_level,
                "check_level_label": CHECK_LEVEL_LABELS.get(f.check_level, f.check_level),
                "status_note": f.status_note,
                "size": f.size,
                "size_label": fmt_size(f.size),
                "duration": f.duration,
                "duration_label": fmt_time(f.duration) if f.duration else "",
                "container": f.container_format,
                "video_codec": f.video_codec,
                "resolution": f.video_resolution,
                "audio_codecs": f.audio_codecs,
                "last_checked_at": f.last_checked_at.isoformat() if f.last_checked_at else None,
                "last_checked_label": _local(f.last_checked_at),
                "error_count": f.error_count,
                "warning_count": f.warning_count,
                "results": [{
                    "method": r.method, "method_label": METHOD_LABELS.get(r.method, r.method),
                    "outcome": r.outcome, "outcome_label": OUTCOME_LABELS.get(r.outcome, r.outcome),
                    "status": r.status, "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "scanner_version": r.scanner_version, "job_id": r.job_id,
                    "decoded_duration": r.decoded_duration, "container_duration": r.container_duration,
                } for r in rs],
                "findings": findings,
            }


CSV_FILE_COLUMNS = [
    ("Status", "status_label"), ("Prüftiefe", "check_level_label"), ("Film", "filename"), ("Pfad", "path"),
    ("Verzeichnis", "directory"), ("Bibliothek", "library"), ("Größe (Byte)", "size"), ("Größe", "size_label"),
    ("Dauer", "duration_label"), ("Container", "container"), ("Videoformat", "video_codec"),
    ("Auflösung", "resolution"), ("Audio", "audio_codecs"), ("Letzte Prüfung", "last_checked_label"),
    ("Fehler", "error_count"), ("Warnungen", "warning_count"), ("Vorhanden", "present"), ("Hinweis", "status_note"),
]
CSV_FINDING_COLUMNS = [
    ("Schweregrad", "severity_label"), ("Fehlertyp", "error_type_label"), ("Heuristisch", "heuristic"),
    ("Methode", "method_label"), ("Stream", "stream_index"), ("Streamtyp", "stream_type"), ("Zeitpunkt", "position"),
    ("Zeitgenauigkeit", "time_precision_label"), ("Zeit von (s)", "time_start"), ("Zeit bis (s)", "time_end"),
    ("Byte-Offset", "byte_offset"), ("Werkzeug", "tool"), ("Anzahl", "occurrences"), ("Beschreibung", "description"),
    ("Originalmeldung", "raw_message"),
]


def _csv_val(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "ja" if v else "nein"
    s = str(v)
    # Schutz vor Formel-Injektion in Tabellenkalkulationen
    if s and s[0] in "=+-@" and not s.lstrip("-").replace(".", "", 1).isdigit():
        s = "'" + s
    return s


def export_csv(records: Iterator[dict], details: bool, delimiter: str = ";") -> Iterator[bytes]:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    header = [c for c, _ in CSV_FILE_COLUMNS] + ([c for c, _ in CSV_FINDING_COLUMNS] if details else [])
    w.writerow(header)
    yield ("﻿" + buf.getvalue()).encode("utf-8")  # BOM: Excel erkennt UTF-8/Umlaute
    buf.seek(0)
    buf.truncate()
    for rec in records:
        base = [_csv_val(rec.get(k)) for _, k in CSV_FILE_COLUMNS]
        if details and rec["findings"]:
            for f in rec["findings"]:
                w.writerow(base + [_csv_val(f.get(k)) for _, k in CSV_FINDING_COLUMNS])
        else:
            w.writerow(base + ([""] * len(CSV_FINDING_COLUMNS) if details else []))
        if buf.tell() > 64 * 1024:
            yield buf.getvalue().encode("utf-8")
            buf.seek(0)
            buf.truncate()
    yield buf.getvalue().encode("utf-8")


def export_json(records: Iterator[dict], meta: dict) -> Iterator[bytes]:
    yield ('{"meta": ' + json.dumps(meta, ensure_ascii=False, default=str) + ', "files": [').encode("utf-8")
    first = True
    for rec in records:
        yield (("" if first else ",") + "\n" + json.dumps(rec, ensure_ascii=False, default=str)).encode("utf-8")
        first = False
    yield b"\n]}\n"


HTML_STYLE = """
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:24px;color:#111827;background:#fff}
h1{font-size:22px;margin:0 0 4px} .meta{color:#4b5563;font-size:13px;margin-bottom:16px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:18px}
th,td{border:1px solid #e5e7eb;padding:5px 7px;text-align:left;vertical-align:top}
th{background:#f3f4f6} .s-critical{color:#b91c1c;font-weight:700} .s-error{color:#c2410c;font-weight:600}
.s-warning{color:#a16207} .s-ok{color:#15803d} .s-incomplete{color:#6b7280} .s-unchecked{color:#6b7280}
.file{margin:18px 0 6px;font-size:15px} .path{color:#6b7280;font-size:12px;word-break:break-all}
.raw{font-family:ui-monospace,monospace;font-size:11px;white-space:pre-wrap;color:#374151}
.summary td{font-size:14px} .h{font-style:italic;color:#6b7280}
"""


def export_html(records: Iterator[dict], meta: dict, details: bool) -> Iterator[bytes]:
    e = html.escape
    yield (f"<!doctype html><html lang='de'><head><meta charset='utf-8'><title>{e(meta['title'])}</title>"
           f"<style>{HTML_STYLE}</style></head><body><h1>{e(meta['title'])}</h1>"
           f"<div class='meta'>Erstellt am {e(meta['created'])} · Media Integrity Scanner · Umfang: {e(meta['scope_label'])}"
           f"</div>").encode("utf-8")
    counts: dict[str, int] = {}
    rows: list[str] = []
    detail_parts: list[str] = []

    def flush_rows() -> Iterator[bytes]:
        if rows:
            yield "".join(rows).encode("utf-8")
            rows.clear()

    yield ("<table><thead><tr><th>Status</th><th>Film</th><th>Größe</th><th>Dauer</th><th>Video</th>"
           "<th>Letzte Prüfung</th><th>Prüftiefe</th><th>Fehler</th><th>Warnungen</th><th>Verzeichnis</th></tr></thead>"
           "<tbody>").encode("utf-8")
    n = 0
    for rec in records:
        n += 1
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
        rows.append(
            f"<tr><td class='s-{e(rec['status'])}'>{e(rec['status_label'])}</td><td>{e(rec['filename'])}</td>"
            f"<td>{e(rec['size_label'])}</td><td>{e(rec['duration_label'])}</td><td>{e(rec['video_codec'] or '')}"
            f" {e(rec['resolution'] or '')}</td><td>{e(rec['last_checked_label'])}</td>"
            f"<td>{e(rec['check_level_label'])}</td><td>{rec['error_count']}</td><td>{rec['warning_count']}</td>"
            f"<td>{e(rec['directory'] or '')}</td></tr>")
        if details and rec["findings"]:
            part = [f"<div class='file'><b>{e(rec['filename'])}</b> – <span class='s-{e(rec['status'])}'>"
                    f"{e(rec['status_label'])}</span></div><div class='path'>{e(rec['path'])}</div>"
                    "<table><thead><tr><th>Zeitpunkt</th><th>Schweregrad</th><th>Fehler</th><th>Stream</th>"
                    "<th>Beschreibung</th><th>Byte-Offset</th><th>Werkzeug</th><th>Originalmeldung</th></tr></thead><tbody>"]
            for f in rec["findings"]:
                h = " <span class='h'>(Hinweis, kein sicherer Defektnachweis)</span>" if f["heuristic"] else ""
                part.append(
                    f"<tr><td>{e(f['position'])}<br><span class='h'>{e(f['time_precision_label'])}</span></td>"
                    f"<td class='s-{e(f['severity'])}'>{e(f['severity_label'])}</td><td>{e(f['error_type_label'])}{h}</td>"
                    f"<td>{'' if f['stream_index'] is None else f['stream_index']} {e(f['stream_type'] or '')}</td>"
                    f"<td>{e(f['description'])}" + (f" (×{f['occurrences']})" if f["occurrences"] > 1 else "") + "</td>"
                    f"<td>{'' if f['byte_offset'] is None else f['byte_offset']}</td><td>{e(f['tool'])}</td>"
                    f"<td class='raw'>{e((f['raw_message'] or '')[:3000])}</td></tr>")
            part.append("</tbody></table>")
            detail_parts.append("".join(part))
        if len(rows) >= 200:
            yield from flush_rows()
    yield from flush_rows()
    yield b"</tbody></table>"
    summary = "".join(f"<tr><td class='s-{e(k)}'>{e(STATUS_LABELS.get(k, k))}</td><td>{v}</td></tr>"
                      for k, v in sorted(counts.items()))
    yield (f"<h2>Zusammenfassung</h2><table class='summary' style='width:auto'><tr><th>Status</th><th>Dateien</th></tr>"
           f"{summary}<tr><td><b>Gesamt</b></td><td><b>{n}</b></td></tr></table>").encode("utf-8")
    if detail_parts:
        yield "<h2>Fehlerdetails</h2>".encode("utf-8")
        for p in detail_parts:
            yield p.encode("utf-8")
    yield ("<p class='meta'>Zeitangaben: „exakt“ stammt aus Container-/Paketzeitstempeln, „Bereich“ aus den "
           "umgebenden Clusterzeitstempeln, „ungefähr“ aus dem Fortschritt des Decoders. Heuristische Hinweise sind "
           "kein zweifelsfreier Defektnachweis.</p></body></html>").encode("utf-8")


def scope_label(scope: str, db: Session, job_id: int | None, file_id: int | None) -> str:
    if scope == "job" and job_id:
        job = db.get(ScanJob, job_id)
        return f"Scanlauf #{job_id}" + (f" ({job.name})" if job else "")
    if scope == "file" and file_id:
        f = db.get(MediaFile, file_id)
        return f"Datei: {f.filename}" if f else "Datei"
    return {"library": "Gesamte Bibliothek", "damaged": "Beschädigte Dateien (Kritisch/Fehler)",
            "warnings": "Dateien mit Warnungen", "filter": "Gefilterte Dateiliste"}.get(scope, scope)


def build_export(db: Session, fmt: str, scope: str, job_id: int | None = None, file_id: int | None = None,
                 library_id: int | None = None, details: bool | None = None,
                 delimiter: str = ";", filters: dict | None = None) -> tuple[Iterator[bytes], str, str]:
    if scope not in SCOPES:
        raise ExportError("Unbekannter Umfang")
    if details is None:
        details = scope in ("damaged", "warnings", "file", "job", "filter")
    label = scope_label(scope, db, job_id, file_id)
    created = datetime.now(ZoneInfo(get_config().tz))
    meta = {"title": f"Media Integrity Scanner – {label}", "scope": scope, "scope_label": label,
            "created": created.strftime("%d.%m.%Y %H:%M"), "created_iso": created.isoformat()}
    records = iter_records(db, scope, job_id, file_id, library_id, filters=filters)
    stamp = created.strftime("%Y%m%d-%H%M")
    base = f"media-integrity-{scope}{'-' + str(job_id or file_id) if (job_id or file_id) else ''}-{stamp}"
    if fmt == "csv":
        return export_csv(records, details, delimiter), "text/csv; charset=utf-8", base + ".csv"
    if fmt == "json":
        return export_json(records, meta), "application/json; charset=utf-8", base + ".json"
    if fmt == "html":
        return export_html(records, meta, details), "text/html; charset=utf-8", base + ".html"
    raise ExportError("Format muss csv, json oder html sein")
