"""Modus A – Schnellprüfung mit FFprobe (und mkvmerge -J für Matroska).

Die Schnellprüfung liest nur Header und Indexdaten. Sie ist eine
Bestandsaufnahme und **keine** vollständige Integritätsprüfung.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from typing import Any

from .base import Finding, Severity
from .process import ResourceLimits, ScanControl, run_process
from .tools import Toolset


@dataclass
class StreamInfo:
    index: int
    type: str
    codec_name: str | None
    codec_long_name: str | None = None
    codec_tag: str | None = None
    profile: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    pix_fmt: str | None = None
    bit_depth: int | None = None
    channels: int | None = None
    channel_layout: str | None = None
    sample_rate: int | None = None
    language: str | None = None
    title: str | None = None
    bit_rate: int | None = None
    duration: float | None = None
    is_default: bool = False
    forced: bool = False
    attached_pic: bool = False
    time_base: str | None = None
    start_time: float | None = None
    decodable: bool = False
    tag_duration: float | None = None


@dataclass
class ProbeInfo:
    ok: bool
    format_name: str | None = None
    format_long_name: str | None = None
    duration: float | None = None
    size: int | None = None
    bit_rate: int | None = None
    start_time: float | None = None
    tags: dict[str, str] = field(default_factory=dict)
    streams: list[StreamInfo] = field(default_factory=list)
    chapters: int = 0
    error: str | None = None
    stderr: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def stream(self, index: int) -> StreamInfo | None:
        for s in self.streams:
            if s.index == index:
                return s
        return None

    @property
    def is_matroska(self) -> bool:
        return bool(self.format_name and "matroska" in self.format_name)


def _float(v: Any) -> float | None:
    try:
        if v in (None, "", "N/A"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    f = _float(v)
    return int(f) if f is not None else None


def _rate(v: str | None) -> float | None:
    if not v or v in ("0/0", "N/A"):
        return None
    try:
        fr = Fraction(v)
        return float(fr) if fr else None
    except (ValueError, ZeroDivisionError):
        return None


def _tag_duration(tags: dict) -> float | None:
    for key in ("DURATION", "duration", "DURATION-eng"):
        val = tags.get(key)
        if val:
            from .messages import parse_mkv_timestamp
            return parse_mkv_timestamp(val)
    return None


def parse_probe_json(data: dict, toolset: Toolset | None) -> ProbeInfo:
    fmt = data.get("format") or {}
    info = ProbeInfo(
        ok=True,
        format_name=fmt.get("format_name"),
        format_long_name=fmt.get("format_long_name"),
        duration=_float(fmt.get("duration")),
        size=_int(fmt.get("size")),
        bit_rate=_int(fmt.get("bit_rate")),
        start_time=_float(fmt.get("start_time")),
        tags={str(k): str(v) for k, v in (fmt.get("tags") or {}).items()},
        chapters=len(data.get("chapters") or []),
    )
    decodable = toolset.decodable_codecs if toolset else frozenset()
    for s in data.get("streams") or []:
        tags = s.get("tags") or {}
        disp = s.get("disposition") or {}
        codec = s.get("codec_name")
        bit_depth = _int(s.get("bits_per_raw_sample"))
        if bit_depth is None and s.get("pix_fmt"):
            pf = s["pix_fmt"]
            bit_depth = 10 if "10" in pf else 12 if "12" in pf else 8 if s.get("codec_type") == "video" else None
        si = StreamInfo(
            index=int(s.get("index", 0)),
            type=s.get("codec_type") or "unknown",
            codec_name=codec,
            codec_long_name=s.get("codec_long_name"),
            codec_tag=s.get("codec_tag_string"),
            profile=s.get("profile"),
            width=_int(s.get("width")),
            height=_int(s.get("height")),
            fps=_rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate")),
            pix_fmt=s.get("pix_fmt"),
            bit_depth=bit_depth,
            channels=_int(s.get("channels")),
            channel_layout=s.get("channel_layout"),
            sample_rate=_int(s.get("sample_rate")),
            language=tags.get("language"),
            title=tags.get("title"),
            bit_rate=_int(s.get("bit_rate")) or _int(tags.get("BPS")),
            duration=_float(s.get("duration")),
            is_default=bool(disp.get("default")),
            forced=bool(disp.get("forced")),
            attached_pic=bool(disp.get("attached_pic")),
            time_base=s.get("time_base"),
            start_time=_float(s.get("start_time")),
            decodable=bool(codec) and (codec in decodable if decodable else True),
            tag_duration=_tag_duration(tags),
        )
        info.streams.append(si)
    return info


async def run_ffprobe(path: str, toolset: Toolset, control: ScanControl, limits: ResourceLimits) -> tuple[ProbeInfo, int | None, bool]:
    """Liefert (ProbeInfo, returncode, cancelled)."""
    out: list[str] = []
    err: list[str] = []
    cmd = [
        toolset.ffprobe.path or "ffprobe", "-hide_banner", "-loglevel", "repeat+level+warning",
        "-show_format", "-show_streams", "-show_chapters", "-show_error", "-of", "json",
        "file:" + path,
    ]
    res = await run_process(cmd, control, limits, on_stdout=out.append, on_stderr=err.append)
    if res.cancelled:
        return ProbeInfo(ok=False, error="abgebrochen", stderr=err), res.returncode, True
    if res.spawn_error:
        return ProbeInfo(ok=False, error=res.spawn_error, stderr=err), None, False
    try:
        data = json.loads("\n".join(out) or "{}")
    except json.JSONDecodeError:
        data = {}
    if res.returncode != 0 or "error" in data:
        msg = (data.get("error") or {}).get("string") or (err[-1] if err else f"Exit-Code {res.returncode}")
        info = ProbeInfo(ok=False, error=msg, stderr=err)
        return info, res.returncode, False
    info = parse_probe_json(data, toolset)
    info.stderr = err
    return info, res.returncode, False


async def run_mkvmerge_identify(path: str, toolset: Toolset, control: ScanControl, limits: ResourceLimits) -> dict | None:
    if not toolset.mkvmerge.available:
        return None
    out: list[str] = []
    cmd = [toolset.mkvmerge.path or "mkvmerge", "--ui-language", "en_US", "-J", path]
    res = await run_process(cmd, control, limits, on_stdout=out.append)
    if res.cancelled or res.spawn_error:
        return None
    try:
        data = json.loads("\n".join(out))
    except json.JSONDecodeError:
        return {"_failed": True, "_exit": res.returncode}
    data["_exit"] = res.returncode
    return data


def _f(error_type: str, severity: Severity, description: str, tool: str = "ffprobe", **kw) -> Finding:
    return Finding(error_type=error_type, severity=severity, description=description, tool=tool, **kw)


def analyze_probe(info: ProbeInfo, toolset: Toolset, file_size: int) -> list[Finding]:
    """Bewertet die Header-Informationen. Liefert Feststellungen der Schnellprüfung."""
    findings: list[Finding] = []
    if info.stderr:
        # Gleiche Auswertung wie bei FFmpeg (inkl. Zusammenfassen und DTS-Zeitangaben), ohne Fortschrittszeit
        from .ffmpeg import DecodeOptions, FfmpegAnalyzer
        analyzer = FfmpegAnalyzer(info, toolset, [s.index for s in info.streams], "ffprobe", DecodeOptions(),
                                  progress_based=False)
        for line in info.stderr:
            analyzer.on_stderr_line(line)
        findings.extend(f for f in analyzer.finish() if f.severity != Severity.INFO)
    if not info.ok:
        findings.append(_f("file_unreadable", Severity.CRITICAL,
                           "FFprobe kann die Datei nicht öffnen – Container nicht lesbar oder beschädigt.",
                           raw_message=info.error))
        return findings

    av = [s for s in info.streams if s.type in ("video", "audio") and not s.attached_pic]
    if not av:
        findings.append(_f("no_streams", Severity.CRITICAL, "Die Datei enthält keine Video- oder Audiostreams."))
    if not any(s.type == "video" and not s.attached_pic for s in info.streams):
        findings.append(_f("metadata_inconsistent", Severity.WARNING, "Kein Videostream vorhanden.", heuristic=True))

    for s in info.streams:
        if s.type not in ("video", "audio"):
            continue
        if not s.codec_name:
            findings.append(_f("unsupported_codec", Severity.INCOMPLETE,
                               f"Stream {s.index} ({s.type}): unbekannter Codec – kann nicht decodiert werden. "
                               "Das ist kein Hinweis auf eine Beschädigung.",
                               stream_index=s.index, stream_type=s.type, raw_message=s.codec_tag))
        elif not s.decodable:
            findings.append(_f("unsupported_codec", Severity.INCOMPLETE,
                               f"Stream {s.index} ({s.type}): Codec '{s.codec_name}' wird von dieser FFmpeg-Version "
                               "nicht decodiert. Das ist kein Hinweis auf eine Beschädigung.",
                               stream_index=s.index, stream_type=s.type))
        if s.type == "video" and not s.attached_pic and s.codec_name and (not s.width or not s.height):
            findings.append(_f("metadata_inconsistent", Severity.WARNING,
                               f"Videostream {s.index} ohne gültige Auflösung.", stream_index=s.index,
                               stream_type="video", heuristic=True))
        if s.type == "audio" and s.codec_name and (not s.sample_rate or not s.channels):
            findings.append(_f("metadata_inconsistent", Severity.WARNING,
                               f"Audiostream {s.index} ohne gültige Abtastrate/Kanalzahl.", stream_index=s.index,
                               stream_type="audio", heuristic=True))

    if not info.duration or info.duration <= 0:
        findings.append(_f("metadata_inconsistent", Severity.WARNING, "Keine Laufzeit im Container angegeben.",
                           heuristic=True))
    else:
        for s in av:
            sd = s.duration or s.tag_duration
            if sd and abs(sd - info.duration) > max(5.0, info.duration * 0.05):
                findings.append(_f(
                    "duration_mismatch", Severity.WARNING,
                    f"Laufzeit von Stream {s.index} ({sd:.1f} s) weicht von der Containerlaufzeit "
                    f"({info.duration:.1f} s) ab.", stream_index=s.index, stream_type=s.type, heuristic=True))
    if info.size and file_size and info.size != file_size:
        findings.append(_f("metadata_inconsistent", Severity.WARNING,
                           f"FFprobe meldet {info.size} Byte, die Datei hat {file_size} Byte.", heuristic=True))
    return dedupe_unsupported(findings)


def dedupe_unsupported(findings: list[Finding]) -> list[Finding]:
    seen: set = set()
    out = []
    for f in reversed(findings):
        if f.error_type == "unsupported_codec":
            key = f.stream_index
            if key in seen:
                continue
            seen.add(key)
        out.append(f)
    return list(reversed(out))


def analyze_mkvmerge_identify(data: dict | None, info: ProbeInfo) -> list[Finding]:
    findings: list[Finding] = []
    if data is None:
        return findings
    tool = "mkvmerge -J"
    if data.get("_failed"):
        findings.append(_f("tool_failed", Severity.INCOMPLETE, "mkvmerge -J lieferte keine auswertbare Ausgabe.", tool))
        return findings
    container = data.get("container") or {}
    for e in data.get("errors") or []:
        findings.append(_f("matroska_structure", Severity.ERROR, "mkvmerge meldet bei der Identifikation einen Fehler.",
                           tool, raw_message=str(e)))
    for w in data.get("warnings") or []:
        findings.append(_f("structure_warning", Severity.WARNING, "mkvmerge meldet bei der Identifikation eine Warnung.",
                           tool, raw_message=str(w), heuristic=True))
    if container and not container.get("recognized", True):
        findings.append(_f("not_media", Severity.CRITICAL, "mkvmerge erkennt den Container nicht.", tool))
    elif container and not container.get("supported", True):
        findings.append(_f("unsupported_codec", Severity.INCOMPLETE, "mkvmerge unterstützt diesen Container nicht.", tool))
    props = container.get("properties") or {}
    dur_ns = props.get("duration")
    if dur_ns and info.duration:
        mk = dur_ns / 1e9
        if abs(mk - info.duration) > max(2.0, info.duration * 0.01):
            findings.append(_f("metadata_inconsistent", Severity.WARNING,
                               f"mkvmerge ({mk:.1f} s) und FFprobe ({info.duration:.1f} s) melden unterschiedliche Laufzeiten.",
                               tool, heuristic=True))
    tracks = [t for t in data.get("tracks") or [] if t.get("type") in ("video", "audio", "subtitles")]
    ff_tracks = [s for s in info.streams if s.type in ("video", "audio", "subtitle")]
    if info.ok and tracks and len(tracks) != len(ff_tracks):
        findings.append(_f("metadata_inconsistent", Severity.WARNING,
                           f"mkvmerge findet {len(tracks)} Spuren, FFprobe {len(ff_tracks)}.", tool, heuristic=True))
    return findings
