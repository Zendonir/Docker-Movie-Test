"""Modus B – erweiterte Containerprüfung.

Für Matroska-Dateien werden drei voneinander unabhängige Verfahren kombiniert:

1. **EBML-Strukturprüfer** (``ebml.py``): eigener, streamender Parser, der die
   gesamte Datei liest und jedes Element/jeden Block validiert (inkl. CRC-32,
   Zeitstempel, Cues, SeekHead, abgeschnittene Dateien).
2. **mkvmerge-Volldurchlauf** (``mkvmerge -o /dev/null``): MKVToolNix liest und
   multiplext alle Blöcke, ohne etwas zu schreiben. Meldet z. B.
   "Error in the Matroska file structure at position …" inkl. Resync-Zeitstempeln.
   ``mkvmerge -J`` (reine Identifikation) wird dafür ausdrücklich nicht genutzt.
3. **FFmpeg-Demux-Durchlauf** (``ffmpeg -c copy -f null``): liest alle Pakete
   über den FFmpeg-Demuxer. Wird übersprungen, wenn im selben Lauf ohnehin die
   vollständige Decodierung erfolgt (die den Demuxer genauso durchläuft).

Optional: Diagnose-Remux mit mkvmerge in eine temporäre Datei (nie über das
Original), anschließend Vergleich der Laufzeit. Die temporäre Datei wird
immer gelöscht.

Für andere Container (MP4, AVI, TS, …) besteht Modus B aus dem
FFmpeg-Demux-Durchlauf.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .base import Finding, Method, MethodResult, Outcome, Severity, TimePrecision
from .ebml import TOOL as EBML_TOOL
from .ffmpeg import DecodeOptions, run_ffmpeg_check
from .ffprobe import ProbeInfo
from .messages import (
    MKV_FIRST_TS, MKV_IGNORE, MKV_LAST_TS, MKV_PERMISSION, MKV_RESYNC_FAIL, MKV_RESYNC_OK, MKV_STRUCTURE,
    MKV_TIMESTAMP_WARN, MKV_UNRECOGNIZED, MKV_UNSUPPORTED, parse_mkv_line, parse_mkv_timestamp,
)
from .process import ResourceLimits, ScanControl, run_process
from .tools import Toolset

TOOL_MKVMERGE = "mkvmerge (Volldurchlauf)"
TOOL_REMUX = "mkvmerge (Diagnose-Remux)"
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class ContainerOptions:
    run_ebml: bool = True
    run_mkvmerge: bool = True
    run_ffmpeg_demux: bool = True
    remux_check: bool = False
    tmp_dir: str = "/tmp"
    ts_jump_warning: float = 3.0
    ts_jump_error: float = 30.0
    track_gap_warning: float = 5.0
    duration_tolerance_s: float = 5.0
    duration_tolerance_pct: float = 2.0
    max_findings: int = 400


def time_range_for_offset(cluster_index: list[list], offset: int) -> tuple[float | None, float | None]:
    """Zeitbereich eines Byte-Offsets anhand der tatsächlich gelesenen Clusterpositionen.

    Es wird nicht geschätzt: Ergebnis ist der Zeitstempel des Clusters, in dem
    der Offset liegt, bis zum Zeitstempel des nachfolgenden Clusters.
    """
    start = end = None
    for pos, ts in cluster_index:
        if pos <= offset:
            start = ts
        else:
            end = ts
            break
    return start, end


async def run_ebml_validator(path: str, control: ScanControl, limits: ResourceLimits, opts: ContainerOptions,
                             on_progress: Callable[[float], None] | None) -> tuple[dict | None, str | None, bool]:
    """Startet den EBML-Prüfer als Unterprozess. Rückgabe (summary, fehler, abgebrochen)."""
    summary: dict | None = None
    fatal: dict | None = None
    cmd = [sys.executable, "-m", "app.scanners.ebml", path,
           "--ts-jump-warning", str(opts.ts_jump_warning), "--ts-jump-error", str(opts.ts_jump_error),
           "--track-gap-warning", str(opts.track_gap_warning),
           "--duration-tolerance-s", str(opts.duration_tolerance_s),
           "--duration-tolerance-pct", str(opts.duration_tolerance_pct),
           "--max-findings", str(opts.max_findings)]

    def on_line(line: str) -> None:
        nonlocal summary, fatal
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        t = obj.get("t")
        if t == "progress" and on_progress and obj.get("size"):
            on_progress(obj["pos"] / obj["size"])
        elif t == "summary":
            summary = obj
        elif t == "fatal":
            fatal = obj

    env = {"PYTHONPATH": str(BACKEND_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    res = await run_process(cmd, control, limits, on_stdout=on_line, env=env)
    if res.cancelled:
        return None, None, True
    if fatal:
        return None, f"{fatal.get('kind')}: {fatal.get('message')}", False
    if summary is None:
        tail = "\n".join(res.stderr_tail[-10:])
        return None, f"EBML-Prüfer ohne Ergebnis beendet (Exit-Code {res.returncode}). {tail}", False
    return summary, None, False


class MkvmergeParser:
    def __init__(self, cluster_index: list[list] | None):
        self.cluster_index = cluster_index or []
        self.findings: list[Finding] = []
        self.by_pos: dict[int, Finding] = {}
        self.current: Finding | None = None
        self.unrecognized = False
        self.permission = False
        self.progress: float | None = None
        self.raw_lines: list[str] = []
        self.ts_before: dict[int, float] = {}
        self.ts_after: dict[int, float] = {}

    def on_line(self, raw: str) -> None:
        if raw.startswith("#GUI#progress"):
            try:
                self.progress = float(raw.split()[-1].rstrip("%")) / 100
            except ValueError:
                pass
            return
        m = parse_mkv_line(raw)
        if m is None:
            return
        if any(p.search(m.msg) for p in MKV_IGNORE):
            return
        self.raw_lines.append(m.raw)
        msg = m.msg
        sm = MKV_STRUCTURE.search(msg)
        if sm:
            pos = int(sm.group("pos"))
            existing = self.by_pos.get(pos)
            if existing is not None:
                self.current = existing  # mkvmerge meldet denselben Fehler beim Öffnen und beim Muxen
                existing.raw_message = (existing.raw_message or "")
                return
            f = Finding("matroska_structure", Severity.ERROR,
                        f"mkvmerge: Fehler in der Matroska-Struktur bei Byte {pos}; Resynchronisation auf das nächste Level-1-Element.",
                        TOOL_MKVMERGE, byte_offset=pos, raw_message=m.raw)
            self.by_pos[pos] = f
            self.findings.append(f)
            self.current = f
            return
        if self.current is not None:
            cur = self.current
            if MKV_LAST_TS.search(msg):
                ts = parse_mkv_timestamp(msg)
                if ts is not None:
                    self.ts_before[id(cur)] = ts
                self._append_raw(cur, m.raw)
                return
            rm = MKV_RESYNC_OK.search(msg)
            if rm:
                cur.byte_end = int(rm.group("pos"))
                self._append_raw(cur, m.raw)
                return
            if MKV_FIRST_TS.search(msg):
                ts = parse_mkv_timestamp(msg)
                if ts is not None:
                    self.ts_after[id(cur)] = ts
                self._append_raw(cur, m.raw)
                return
            if MKV_RESYNC_FAIL.search(msg):
                cur.severity = Severity.CRITICAL
                cur.description += " Die Resynchronisation ist fehlgeschlagen – der Rest der Datei ist nicht verwertbar."
                self._append_raw(cur, m.raw)
                return
        if MKV_PERMISSION.search(msg) and "permission" in msg.lower():
            self.permission = True
            return
        if m.level == "error" and MKV_UNRECOGNIZED.search(msg):
            self.unrecognized = True
            self.findings.append(Finding("file_unreadable", Severity.CRITICAL,
                                         "mkvmerge erkennt die Datei nicht als lesbaren Container.", TOOL_MKVMERGE,
                                         raw_message=m.raw))
            return
        if MKV_UNSUPPORTED.search(msg) and m.level == "warning":
            self.findings.append(Finding("unsupported_codec", Severity.INFO,
                                         "mkvmerge unterstützt einen Teil der Daten nicht (kein Defektnachweis).",
                                         TOOL_MKVMERGE, raw_message=m.raw))
            return
        if m.level == "warning":
            et = "timestamp_invalid" if MKV_TIMESTAMP_WARN.search(msg) else "structure_warning"
            self.findings.append(Finding(et, Severity.WARNING, "mkvmerge-Warnung (nicht eindeutig).", TOOL_MKVMERGE,
                                         raw_message=m.raw, heuristic=True))
        else:
            self.findings.append(Finding("tool_error", Severity.ERROR,
                                         "mkvmerge meldet einen Fehler beim Lesen der Datei.", TOOL_MKVMERGE,
                                         raw_message=m.raw))

    @staticmethod
    def _append_raw(f: Finding, raw: str) -> None:
        if f.raw_message and raw in f.raw_message:
            return
        f.raw_message = (f.raw_message + "\n" + raw) if f.raw_message else raw

    def finish(self) -> list[Finding]:
        for f in self.findings:
            before, after = self.ts_before.get(id(f)), self.ts_after.get(id(f))
            if before is not None and after is not None:
                f.time_start, f.time_end = before, after
                f.time_precision = TimePrecision.RANGE
                f.time_source = "mkvmerge: letzter Zeitstempel vor dem Fehler bis erster Clusterzeitstempel nach dem Resync"
            elif before is not None or after is not None:
                f.time_start = before if before is not None else after
                f.time_precision = TimePrecision.EXACT
                f.time_source = ("mkvmerge: letzter verarbeiteter Zeitstempel vor dem Fehler" if before is not None
                                 else "mkvmerge: erster Clusterzeitstempel nach dem Resync")
        for f in self.findings:
            if f.byte_offset is not None and f.time_start is None and self.cluster_index:
                start, end = time_range_for_offset(self.cluster_index, f.byte_offset)
                if start is not None:
                    f.time_start, f.time_end = start, end
                    f.time_precision = TimePrecision.RANGE
                    f.time_source = "Clusterindex (Zeitstempel der umgebenden Cluster)"
        return self.findings


async def run_mkvmerge_pass(path: str, toolset: Toolset, control: ScanControl, limits: ResourceLimits,
                            cluster_index: list[list] | None,
                            on_progress: Callable[[float], None] | None) -> tuple[list[Finding], Outcome, dict]:
    parser = MkvmergeParser(cluster_index)

    def on_line(line: str) -> None:
        parser.on_line(line)
        if on_progress and parser.progress is not None and line.startswith("#GUI#progress"):
            on_progress(parser.progress)

    cmd = [toolset.mkvmerge.path or "mkvmerge", "--ui-language", "en_US", "--gui-mode", "-o", os.devnull, path]
    res = await run_process(cmd, control, limits, on_stdout=on_line, on_stderr=on_line)
    info = {"command": cmd, "returncode": res.returncode, "version": toolset.mkvmerge.version}
    if res.cancelled:
        return parser.finish(), Outcome.ABORTED, info
    if res.spawn_error:
        return [Finding("tool_failed", Severity.INCOMPLETE, f"mkvmerge konnte nicht gestartet werden: {res.spawn_error}",
                        TOOL_MKVMERGE)], Outcome.TOOL_FAILED, info
    if parser.permission:
        return [Finding("permission_denied", Severity.INCOMPLETE, "mkvmerge: keine Leseberechtigung.", TOOL_MKVMERGE)], \
            Outcome.PERMISSION_DENIED, info
    findings = parser.finish()
    if res.returncode not in (0, 1, 2):
        findings.append(Finding("tool_failed", Severity.INCOMPLETE,
                                f"mkvmerge endete unerwartet (Exit-Code {res.returncode}).", TOOL_MKVMERGE,
                                raw_message="\n".join(res.stdout_tail[-5:])))
        return findings, Outcome.TOOL_FAILED, info
    if res.returncode == 2 and not any(f.severity in (Severity.ERROR, Severity.CRITICAL) for f in findings):
        findings.append(Finding("tool_error", Severity.ERROR, "mkvmerge hat die Verarbeitung mit einem Fehler beendet.",
                                TOOL_MKVMERGE, raw_message="\n".join(res.stdout_tail[-5:])))
    return findings, Outcome.COMPLETED, info


async def run_remux_check(path: str, probe: ProbeInfo, toolset: Toolset, control: ScanControl, limits: ResourceLimits,
                          tmp_dir: str) -> tuple[list[Finding], dict]:
    """Diagnose-Remux in eine temporäre Datei. Das Original wird nie verändert."""
    size = os.path.getsize(path)
    os.makedirs(tmp_dir, exist_ok=True)
    free = shutil.disk_usage(tmp_dir).free
    if free < size * 1.1 + 512 * 1024 * 1024:
        return [Finding("tool_failed", Severity.INFO,
                        f"Diagnose-Remux übersprungen: zu wenig temporärer Speicher in {tmp_dir}.", TOOL_REMUX)], {}
    out = os.path.join(tmp_dir, f"remux-{uuid.uuid4().hex}.mkv")
    real_src = os.path.realpath(path)
    if os.path.realpath(out) == real_src:  # pragma: no cover - reine Absicherung
        raise RuntimeError("Remux-Ziel darf nicht das Original sein")
    findings: list[Finding] = []
    info: dict = {}
    try:
        lines: list[str] = []
        cmd = [toolset.mkvmerge.path or "mkvmerge", "--ui-language", "en_US", "--gui-mode", "-o", out, path]
        res = await run_process(cmd, control, limits, on_stdout=lines.append, on_stderr=lines.append)
        info = {"command": cmd, "returncode": res.returncode}
        if res.cancelled:
            return findings, info
        if res.returncode == 2 or not os.path.exists(out):
            findings.append(Finding("remux_failed", Severity.ERROR,
                                    "Der Diagnose-Remux mit mkvmerge ist fehlgeschlagen.", TOOL_REMUX,
                                    raw_message="\n".join(l for l in lines if l.startswith("#GUI#"))[-2000:]))
            return findings, info
        from .ffprobe import run_ffprobe
        rprobe, _, _ = await run_ffprobe(out, toolset, control, limits)
        if rprobe.ok and probe.duration and rprobe.duration:
            diff = probe.duration - rprobe.duration
            info["remux_duration"] = rprobe.duration
            if abs(diff) > max(2.0, probe.duration * 0.01):
                findings.append(Finding(
                    "duration_mismatch", Severity.WARNING,
                    f"Nach dem Remux beträgt die Laufzeit {rprobe.duration:.1f} s statt {probe.duration:.1f} s.",
                    TOOL_REMUX, heuristic=True))
    finally:
        try:
            os.unlink(out)
        except FileNotFoundError:
            pass
    return findings, info


def merge_corroborating(findings: list[Finding]) -> list[Finding]:
    """Fasst Befunde mehrerer Werkzeuge an derselben Byteposition zusammen."""
    primary = [f for f in findings if f.tool == EBML_TOOL and f.byte_offset is not None]
    out: list[Finding] = []
    for f in findings:
        if f.tool != EBML_TOOL and f.byte_offset is not None and f.error_type in (
                "matroska_structure", "ebml_structure", "cluster_damaged"):
            match = next((p for p in primary
                          if p.byte_offset is not None and (
                              abs(p.byte_offset - f.byte_offset) <= 64
                              or (p.byte_end is not None and p.byte_offset <= f.byte_offset <= p.byte_end))), None)
            if match is not None:
                match.extra.setdefault("confirmed_by", []).append(f.tool)
                match.raw_message = ((match.raw_message + "\n") if match.raw_message else "") + f"[{f.tool}] " + (f.raw_message or "")
                if match.time_precision == TimePrecision.UNKNOWN and f.time_start is not None:
                    match.time_start, match.time_end = f.time_start, f.time_end
                    match.time_precision, match.time_source = f.time_precision, f.time_source
                continue
        out.append(f)
    return out


async def run_container_check(
    path: str,
    probe: ProbeInfo,
    toolset: Toolset,
    control: ScanControl,
    limits: ResourceLimits,
    copts: ContainerOptions,
    dopts: DecodeOptions,
    on_progress: Callable[[float], None] | None = None,
) -> MethodResult:
    result = MethodResult(method=Method.CONTAINER, outcome=Outcome.COMPLETED, container_duration=probe.duration)
    is_mkv = probe.is_matroska or path.lower().endswith((".mkv", ".mka", ".webm"))
    phases: list[str] = []
    if is_mkv and copts.run_ebml:
        phases.append("ebml")
    if is_mkv and copts.run_mkvmerge and toolset.mkvmerge.available:
        phases.append("mkvmerge")
    if (copts.run_ffmpeg_demux or not is_mkv) and probe.ok:
        # Ohne lesbaren Container (FFprobe gescheitert) ist ein Demux-Durchlauf sinnlos;
        # der Befund "nicht lesbar" wird von der Prüfablaufsteuerung ergänzt.
        phases.append("demux")
    if is_mkv and copts.remux_check and toolset.mkvmerge.available:
        phases.append("remux")
    n = max(len(phases), 1)

    def phase_progress(i: int) -> Callable[[float], None]:
        def cb(frac: float) -> None:
            if on_progress:
                on_progress((i + max(0.0, min(frac, 1.0))) / n)
        return cb

    findings: list[Finding] = []
    cluster_index: list[list] | None = None
    incomplete_reasons: list[str] = []

    if is_mkv and copts.run_mkvmerge and not toolset.mkvmerge.available:
        findings.append(Finding("tool_failed", Severity.INCOMPLETE, "MKVToolNix (mkvmerge) ist nicht installiert.", TOOL_MKVMERGE))
        incomplete_reasons.append("mkvmerge fehlt")

    for i, phase in enumerate(phases):
        if control.cancelled:
            break
        if phase == "ebml":
            summary, err, cancelled = await run_ebml_validator(path, control, limits, copts, phase_progress(i))
            if cancelled:
                break
            if err:
                if err.startswith("permission_denied"):
                    result.outcome = Outcome.PERMISSION_DENIED
                    findings.append(Finding("permission_denied", Severity.INCOMPLETE, "Keine Leseberechtigung.", EBML_TOOL,
                                            raw_message=err))
                    result.findings = findings
                    return result
                findings.append(Finding("tool_failed", Severity.INCOMPLETE, "Der EBML-Strukturprüfer ist fehlgeschlagen.",
                                        EBML_TOOL, raw_message=err[-2000:]))
                incomplete_reasons.append("EBML-Prüfer fehlgeschlagen")
                continue
            assert summary is not None
            cluster_index = summary.get("cluster_index")
            findings.extend(Finding.from_dict(f) for f in summary.get("findings", []))
            result.details["ebml"] = {k: v for k, v in summary.items() if k not in ("findings", "cluster_index", "t")}
            result.tools[EBML_TOOL] = {"version": "1"}
            if summary.get("suppressed_findings"):
                findings.append(Finding("tool_warning", Severity.INFO,
                                        f"{summary['suppressed_findings']} weitere Strukturmeldungen nicht einzeln gespeichert.",
                                        EBML_TOOL))
        elif phase == "mkvmerge":
            mf, outcome, info = await run_mkvmerge_pass(path, toolset, control, limits, cluster_index, phase_progress(i))
            result.tools[TOOL_MKVMERGE] = info
            findings.extend(mf)
            if outcome == Outcome.ABORTED:
                break
            if outcome == Outcome.PERMISSION_DENIED:
                result.outcome = outcome
                result.findings = findings
                return result
            if outcome != Outcome.COMPLETED:
                incomplete_reasons.append("mkvmerge fehlgeschlagen")
        elif phase == "demux":
            dres = await run_ffmpeg_check(path, probe, toolset, control, limits, dopts, mode="demux",
                                          on_progress=phase_progress(i))
            result.tools.update(dres.tools)
            for f in dres.findings:
                if f.time_start is None and f.byte_offset is not None and cluster_index:
                    s, e = time_range_for_offset(cluster_index, f.byte_offset)
                    if s is not None:
                        f.time_start, f.time_end = s, e
                        f.time_precision = TimePrecision.RANGE
                        f.time_source = "Clusterindex (Zeitstempel der umgebenden Cluster)"
                findings.append(f)
            result.details["demux"] = dres.details
            result.decoded_duration = dres.decoded_duration
            if dres.outcome == Outcome.ABORTED:
                break
            if dres.outcome == Outcome.PERMISSION_DENIED:
                result.outcome = dres.outcome
                result.findings = findings
                return result
            if dres.outcome not in (Outcome.COMPLETED, Outcome.PARTIAL):
                incomplete_reasons.append("FFmpeg-Demux fehlgeschlagen")
        elif phase == "remux":
            rf, info = await run_remux_check(path, probe, toolset, control, limits, copts.tmp_dir)
            result.tools[TOOL_REMUX] = info
            findings.extend(rf)

    if control.cancelled:
        result.outcome = Outcome.ABORTED
    elif incomplete_reasons:
        result.outcome = Outcome.TOOL_FAILED
        result.summary = "Nicht vollständig: " + ", ".join(incomplete_reasons)
    if not is_mkv:
        result.details["note"] = ("Für diesen Container besteht die Strukturprüfung aus einem vollständigen "
                                  "Demux-Durchlauf mit FFmpeg.")
    if on_progress:
        on_progress(1.0)
    if cluster_index:
        # interne Übergabe an die Decodierprüfung, wird vor dem Speichern entfernt
        result.details["_cluster_index"] = cluster_index
    result.findings = merge_corroborating(findings)
    return result
