"""Modus C – vollständige Decodierprüfung (und Demux-Durchlauf für Modus B).

Die Datei wird von FFmpeg vollständig gelesen; alle unterstützten Video- und
Audiostreams werden decodiert und verworfen (``-f null``). Es wird nichts
geschrieben und nichts neu codiert.

Stderr wird zeilenweise vollständig ausgewertet (auch wenn FFmpeg mit Exit-Code
0 endet). Wiederholte Meldungen werden zu Befunden mit Zeitbereich und Anzahl
zusammengefasst; das vollständige Protokoll wird komprimiert abgelegt.

Zeitangaben:
* Meldungen mit DTS (z. B. "Packet corrupt") werden über die Zeitbasis des
  Streams exakt umgerechnet.
* Decoderfehler enthalten keine Zeitstempel. Sie werden dem Fortschritt von
  FFmpeg (``-progress``, out_time) zugeordnet und als *ungefährer Bereich*
  gekennzeichnet – nie als exakte Position.
"""

from __future__ import annotations

import gzip
import os
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable

from .base import Finding, Method, MethodResult, Outcome, Severity, TimePrecision
from .ffprobe import ProbeInfo
from .messages import classify_ffmpeg, parse_ffmpeg_line
from .process import ResourceLimits, ScanControl, run_process
from .tools import Toolset

TOOL_DECODE = "ffmpeg (Decodierung)"
TOOL_DEMUX = "ffmpeg (Demux)"

DECODER_ALIASES = {
    "mp3float": "mp3", "mp3": "mp3", "aac_fixed": "aac", "ac3_fixed": "ac3", "dca": "dts", "libdav1d": "av1",
    "libaom-av1": "av1", "libopus": "opus", "libvpx-vp9": "vp9", "libvpx": "vp8", "h264_cuvid": "h264",
    "hevc_cuvid": "hevc", "mpeg2video": "mpeg2video", "mpeg4": "mpeg4", "msmpeg4": "msmpeg4v3",
    "pcm_s16le": "pcm_s16le", "truehd": "truehd", "eac3": "eac3", "flac": "flac", "vorbis": "vorbis",
}

MERGE_WINDOW = 10.0  # Sekunden: gleichartige Meldungen innerhalb dieses Abstands werden zusammengefasst
MAX_SAMPLES = 8


@dataclass
class DecodeOptions:
    threads: int = 2
    hwaccel: str = "none"
    strict: bool = False
    readrate: float = 0.0
    stats_period: float = 0.5
    max_findings: int = 400
    log_path: str | None = None
    max_log_bytes: int = 20 * 1024 * 1024
    duration_tolerance_s: float = 5.0
    duration_tolerance_pct: float = 2.0


@dataclass
class _Pending:
    finding: Finding
    samples: list[str] = field(default_factory=list)
    open_end: bool = False


class FfmpegAnalyzer:
    """Wertet FFmpeg-Stderr und -Progress fortlaufend aus."""

    def __init__(self, probe: ProbeInfo, toolset: Toolset, mapped: list[int], tool: str, opts: DecodeOptions,
                 progress_based: bool = True):
        self.progress_based = progress_based
        self.probe = probe
        self.toolset = toolset
        self.mapped = mapped
        self.tool = tool
        self.opts = opts
        self.out_time: float | None = None
        self.progress_seen = False
        self.findings: list[_Pending] = []
        self.by_key: dict[tuple, _Pending] = {}
        self.awaiting_end: list[_Pending] = []
        self.suppressed = 0
        self.line_count = 0
        self.fatal_lines: list[str] = []
        self.data_errors = 0
        self.permission_denied = False
        self.open_failed = False
        self.unsupported_streams: set[int] = set()
        self.speed: float | None = None
        self.frames: int | None = None
        self._log = None
        self._log_bytes = 0
        if opts.log_path:
            os.makedirs(os.path.dirname(opts.log_path), exist_ok=True)
            self._log = gzip.open(opts.log_path, "wt", encoding="utf-8")
        codec_map: dict[str, list[int]] = {}
        for s in probe.streams:
            if s.index in mapped and s.codec_name:
                codec_map.setdefault(s.codec_name, []).append(s.index)
        self.codec_map = codec_map

    def close(self) -> None:
        if self._log:
            if self._log_bytes >= self.opts.max_log_bytes:
                self._log.write("... Protokoll gekürzt (Größenlimit erreicht) ...\n")
            self._log.close()
            self._log = None

    # -- Progress (stdout) --
    def on_progress_line(self, line: str) -> None:
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key in ("out_time_us", "out_time_ms"):
            try:
                us = int(value)
                if us >= 0:
                    self._new_time = us / 1e6
            except ValueError:
                pass
        elif key == "speed":
            try:
                self.speed = float(value.rstrip("x"))
            except ValueError:
                pass
        elif key == "frame":
            try:
                self.frames = int(value)
            except ValueError:
                pass
        elif key == "progress":
            t = getattr(self, "_new_time", None)
            if t is not None:
                self.out_time = t
                self.progress_seen = True
                for p in self.awaiting_end:
                    p.finding.time_end = max(p.finding.time_end or 0.0, t + 1.0)
                    p.open_end = False
                self.awaiting_end.clear()

    # -- Stderr --
    def on_stderr_line(self, raw: str) -> None:
        self.line_count += 1
        if self._log and self._log_bytes < self.opts.max_log_bytes:
            self._log.write(raw + "\n")
            self._log_bytes += len(raw) + 1
        p = parse_ffmpeg_line(raw, self.toolset.decodable_codecs or None)
        if p is None:
            return
        if p.level in ("fatal", "panic"):
            self.fatal_lines.append(p.raw)
        low = p.msg.lower()
        if "permission denied" in low:
            self.permission_denied = True
        if "error opening input" in low:
            self.open_failed = True
        c = classify_ffmpeg(p)
        if c is None:
            return
        stream = c.stream_index
        stype = c.stream_kind
        if stream is None and c.codec:
            cand = self.codec_map.get(DECODER_ALIASES.get(c.codec, c.codec)) or self.codec_map.get(c.codec)
            if cand and len(cand) == 1:
                stream = cand[0]
        if stream is not None and stype is None:
            si = self.probe.stream(stream)
            if si:
                stype = si.type
        error_type = c.error_type
        if error_type == "decode_error" and stype in ("audio", "video"):
            error_type = f"{stype}_decode_error"
        if error_type == "unsupported_codec" and stream is not None:
            self.unsupported_streams.add(stream)

        severity = c.severity
        description = c.description

        # Zeitbestimmung
        time_start: float | None = None
        time_end: float | None = None
        precision = TimePrecision.UNKNOWN
        source = None
        if c.dts is not None and stream is not None:
            si = self.probe.stream(stream)
            if si and si.time_base:
                try:
                    tb = Fraction(si.time_base)
                    t = float(c.dts * tb) - (self.probe.start_time or 0.0)
                    if t >= -1:
                        time_start = max(t, 0.0)
                        precision = TimePrecision.EXACT
                        source = "DTS des betroffenen Pakets"
                except (ValueError, ZeroDivisionError):
                    pass
        if time_start is None and self.progress_based and not self.open_failed and severity not in (Severity.INCOMPLETE,):
            if self.progress_seen or self.line_count > 0:
                time_start = max((self.out_time or 0.0) - 0.5, 0.0)
                time_end = None
                precision = TimePrecision.APPROX
                source = "Decoderfortschritt (out_time) – ungefährer Bereich, keine framegenaue Position"
            if c.error_type in ("matroska_structure", "ebml_structure") and c.byte_offset is not None and not self.progress_seen:
                time_start = None
                precision = TimePrecision.UNKNOWN

        if c.start_artifact and (time_start is None or time_start < 3.0) and severity == Severity.ERROR:
            severity = Severity.WARNING
            description += " Tritt nur am Streamanfang auf (typisch für geschnittene TV-Aufnahmen)."
            c.heuristic = True

        if severity in (Severity.ERROR, Severity.CRITICAL) and error_type not in ("unsupported_codec",):
            self.data_errors += 1

        key = (error_type, stream, severity.value, precision == TimePrecision.EXACT)
        existing = self.by_key.get(key)
        if existing is not None:
            ef = existing.finding
            near = (
                time_start is None and ef.time_start is None
            ) or (
                time_start is not None and ef.time_start is not None
                and time_start <= (ef.time_end if ef.time_end is not None else ef.time_start) + MERGE_WINDOW
            )
            if near:
                ef.occurrences += 1
                if len(existing.samples) < MAX_SAMPLES:
                    existing.samples.append(p.raw)
                if time_start is not None:
                    if precision == TimePrecision.APPROX or ef.time_precision == TimePrecision.APPROX:
                        ef.time_precision = TimePrecision.APPROX
                        ef.time_source = source or ef.time_source
                        if not existing.open_end:
                            existing.open_end = True
                            self.awaiting_end.append(existing)
                    else:
                        ef.time_end = max(ef.time_end or ef.time_start, time_start)
                        if ef.time_end > ef.time_start:
                            ef.time_precision = TimePrecision.RANGE
                            ef.time_source = "DTS der betroffenen Pakete (erstes bis letztes)"
                if ef.byte_offset is None and c.byte_offset is not None:
                    ef.byte_offset = c.byte_offset
                return

        if len(self.findings) >= self.opts.max_findings:
            self.suppressed += 1
            return
        f = Finding(
            error_type=error_type, severity=severity, description=description, tool=self.tool,
            stream_index=stream, stream_type=stype, time_start=time_start, time_end=time_end,
            time_precision=precision, time_source=source, byte_offset=c.byte_offset,
            heuristic=c.heuristic,
        )
        pend = _Pending(f, [p.raw])
        if precision == TimePrecision.APPROX:
            pend.open_end = True
            self.awaiting_end.append(pend)
        self.findings.append(pend)
        self.by_key[key] = pend

    def finish(self) -> list[Finding]:
        out = []
        final_t = self.out_time
        for p in self.findings:
            f = p.finding
            if p.open_end and f.time_start is not None and self.progress_based:
                f.time_end = max(final_t or f.time_start, f.time_start)
            if f.time_precision == TimePrecision.APPROX and f.time_end is None:
                f.time_end = f.time_start
            f.raw_message = "\n".join(p.samples) + (
                f"\n… insgesamt {f.occurrences} Meldungen" if f.occurrences > len(p.samples) else "")
            out.append(f)
        if self.suppressed:
            out.append(Finding("tool_warning", Severity.INFO,
                               f"{self.suppressed} weitere Meldungen wurden nicht einzeln gespeichert (Limit).",
                               self.tool))
        return out


def build_command(path: str, probe: ProbeInfo, toolset: Toolset, opts: DecodeOptions, mode: str) -> tuple[list[str], list[int], list[int]]:
    """Erzeugt die Argumentliste. Rückgabe: (cmd, gemappte Streams, nicht prüfbare Streams)."""
    cmd = [toolset.ffmpeg.path or "ffmpeg", "-nostdin", "-hide_banner", "-nostats",
           "-loglevel", "repeat+level+warning", "-progress", "pipe:1", "-stats_period", str(opts.stats_period)]
    mapped: list[int] = []
    skipped: list[int] = []
    if mode == "decode":
        if opts.threads:
            cmd += ["-threads", str(int(opts.threads))]
        if opts.hwaccel and opts.hwaccel != "none":
            cmd += ["-hwaccel", opts.hwaccel]
        if opts.strict:
            cmd += ["-err_detect", "crccheck+bitstream+buffer"]
        for s in probe.streams:
            if s.type in ("video", "audio") and not s.attached_pic:
                (mapped if s.decodable else skipped).append(s.index)
    else:
        for s in probe.streams:
            if s.type in ("video", "audio", "subtitle") and s.codec_name:
                mapped.append(s.index)
    if opts.readrate and opts.readrate > 0:
        cmd += ["-readrate", f"{opts.readrate:g}"]
    cmd += ["-i", "file:" + path]
    for idx in mapped:
        cmd += ["-map", f"0:{idx}"]
    if mode == "demux":
        cmd += ["-c", "copy"]
    cmd += ["-f", "null", "-"]
    return cmd, mapped, skipped


async def run_ffmpeg_check(
    path: str,
    probe: ProbeInfo,
    toolset: Toolset,
    control: ScanControl,
    limits: ResourceLimits,
    opts: DecodeOptions,
    mode: str = "decode",
    on_progress: Callable[[float], None] | None = None,
) -> MethodResult:
    method = Method.FULL if mode == "decode" else Method.CONTAINER
    tool = TOOL_DECODE if mode == "decode" else TOOL_DEMUX
    cmd, mapped, skipped = build_command(path, probe, toolset, opts, mode)
    result = MethodResult(method=method, outcome=Outcome.COMPLETED, container_duration=probe.duration)
    result.tools[tool] = {"version": toolset.ffmpeg.version, "command": cmd}
    if not toolset.ffmpeg.available:
        result.outcome = Outcome.TOOL_FAILED
        result.findings.append(Finding("tool_failed", Severity.INCOMPLETE, "FFmpeg ist nicht installiert.", tool))
        return result
    if not mapped:
        result.outcome = Outcome.UNSUPPORTED
        result.findings.append(Finding(
            "unsupported_codec", Severity.INCOMPLETE,
            "Kein Stream dieser Datei kann von FFmpeg decodiert werden – keine Decodierprüfung möglich. "
            "Das ist kein Hinweis auf eine Beschädigung.", tool))
        return result

    analyzer = FfmpegAnalyzer(probe, toolset, mapped, tool, opts)
    duration = probe.duration or 0.0

    def progress(line: str) -> None:
        analyzer.on_progress_line(line)
        if on_progress and duration > 0 and line.startswith("progress=") and analyzer.out_time is not None:
            on_progress(min(analyzer.out_time / duration, 1.0))

    try:
        res = await run_process(cmd, control, limits, on_stdout=progress, on_stderr=analyzer.on_stderr_line,
                                progress_value=lambda: analyzer.out_time)
    finally:
        analyzer.close()
    findings = analyzer.finish()
    result.raw_message_count = analyzer.line_count
    result.log_path = opts.log_path
    result.decoded_duration = analyzer.out_time
    result.details = {"mapped_streams": mapped, "skipped_streams": skipped, "speed": analyzer.speed,
                      "frames": analyzer.frames, "returncode": res.returncode, "stderr_lines": analyzer.line_count}

    if res.spawn_error:
        result.outcome = Outcome.TOOL_FAILED
        findings.append(Finding("tool_failed", Severity.INCOMPLETE, f"FFmpeg konnte nicht gestartet werden: {res.spawn_error}", tool))
        result.findings = findings
        return result
    if res.cancelled:
        result.outcome = Outcome.ABORTED
        result.findings = [f for f in findings if f.severity in (Severity.ERROR, Severity.CRITICAL, Severity.WARNING)]
        return result
    if res.stalled:
        result.outcome = Outcome.TOOL_FAILED
        findings.append(Finding("tool_failed", Severity.INCOMPLETE,
                                f"FFmpeg kam {limits.stall_timeout / 60:.0f} Minuten lang nicht voran"
                                + (f" (hängt bei {analyzer.out_time:.0f} s)" if analyzer.out_time is not None else "")
                                + " und wurde beendet. Die Datei ist an dieser Stelle vermutlich stark beschädigt.",
                                tool, time_start=analyzer.out_time, time_precision="approx",
                                time_source="Letzter Fortschritt von FFmpeg"))
        result.findings = findings
        return result

    if analyzer.permission_denied and analyzer.open_failed:
        result.outcome = Outcome.PERMISSION_DENIED
        result.findings = [Finding("permission_denied", Severity.INCOMPLETE,
                                   "Keine Leseberechtigung für die Datei.", tool, raw_message="\n".join(analyzer.fatal_lines))]
        return result

    if res.returncode != 0:
        if analyzer.open_failed:
            findings.append(Finding(
                "file_unreadable", Severity.CRITICAL,
                "FFmpeg kann die Datei nicht öffnen – Container nicht lesbar.", tool,
                raw_message="\n".join(analyzer.fatal_lines[-5:] or res.stderr_tail[-5:])))
        elif analyzer.data_errors > 0:
            t = analyzer.out_time
            findings.append(Finding(
                "decode_aborted", Severity.CRITICAL,
                f"Die Verarbeitung durch FFmpeg wurde mit Exit-Code {res.returncode} abgebrochen"
                + (f" (bei ca. {t:.1f} s)." if t is not None else "."), tool,
                time_start=t, time_end=t, time_precision=TimePrecision.APPROX if t is not None else TimePrecision.UNKNOWN,
                time_source="Decoderfortschritt (out_time)",
                raw_message="\n".join(analyzer.fatal_lines[-5:] or res.stderr_tail[-5:])))
        else:
            result.outcome = Outcome.TOOL_FAILED
            reason = (f"durch Signal {res.killed_by_signal} beendet (z. B. Speichermangel)" if res.killed_by_signal
                      else f"mit Exit-Code {res.returncode} beendet, ohne einen Datenfehler zu melden")
            findings.append(Finding(
                "tool_failed", Severity.INCOMPLETE,
                f"FFmpeg wurde {reason} – kein Befund zur Datei.",
                tool, raw_message="\n".join(res.stderr_tail[-5:])))

    # Laufzeitanalyse (nur wenn regulär beendet)
    if res.returncode == 0 and duration > 0 and analyzer.out_time is not None:
        decoded = analyzer.out_time
        tol = max(opts.duration_tolerance_s, duration * opts.duration_tolerance_pct / 100)
        has_trunc = any(f.error_type in ("truncated", "read_error", "file_unreadable") for f in findings)
        if duration - decoded > tol and not has_trunc:
            findings.append(Finding(
                "unexpected_eof", Severity.ERROR if has_trunc else Severity.WARNING,
                f"Die Decodierung endete bei {decoded:.1f} s, der Container gibt {duration:.1f} s an "
                f"(es fehlen {duration - decoded:.1f} s)."
                + (" Ursache: vorzeitiges Dateiende." if has_trunc else
                   " Mögliche Ursachen: fehlende Daten am Ende oder falsche Laufzeitangabe."),
                tool, time_start=decoded, time_end=duration, time_precision=TimePrecision.RANGE,
                time_source="Ende der Decodierung bis Laufzeit laut Container", heuristic=not has_trunc))
        elif decoded - duration > tol:
            findings.append(Finding(
                "duration_mismatch", Severity.WARNING,
                f"Es wurden {decoded:.1f} s decodiert, der Container gibt nur {duration:.1f} s an "
                "(Zeitstempel- oder Metadatenproblem).", tool, heuristic=True))

    if skipped:
        findings = [f for f in findings if not (f.error_type == "unsupported_codec" and f.stream_index in skipped)]
    for f in findings:
        if f.error_type == "truncated" and analyzer.out_time is not None:
            f.time_start = analyzer.out_time
            f.time_end = duration if duration > analyzer.out_time else None
            f.time_precision = TimePrecision.RANGE if f.time_end else TimePrecision.APPROX
            f.time_source = "Ende der decodierbaren Daten bis Laufzeit laut Container"
    if skipped and mode == "decode":
        for idx in skipped:
            s = probe.stream(idx)
            findings.append(Finding(
                "unsupported_codec", Severity.INCOMPLETE,
                f"Stream {idx} ({s.type if s else '?'}, Codec {s.codec_name if s and s.codec_name else 'unbekannt'}) "
                "kann nicht decodiert werden und wurde nicht geprüft. Kein Hinweis auf eine Beschädigung.",
                tool, stream_index=idx, stream_type=s.type if s else None))
        if result.outcome == Outcome.COMPLETED:
            result.outcome = Outcome.PARTIAL
    if analyzer.unsupported_streams and result.outcome == Outcome.COMPLETED:
        result.outcome = Outcome.PARTIAL
    if result.outcome == Outcome.PARTIAL:
        result.summary = "Nicht alle Streams prüfbar (Codec nicht unterstützt – kein Defektnachweis)"

    result.findings = findings
    return result
