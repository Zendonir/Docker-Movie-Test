"""Spuranalyse mit mkvmerge -J (Spur-IDs entsprechen denen, die mkvmerge beim Remux erwartet)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from ..scanners.process import ResourceLimits, ScanControl, run_process
from ..scanners.tools import get_toolset
from .languages import normalize_language


@dataclass
class Track:
    track_id: int
    type: str  # video, audio, subtitles, buttons
    codec: str | None
    codec_id: str | None
    codec_family: str | None
    language: str
    language_ietf: str | None
    name: str | None
    is_default: bool
    is_forced: bool
    channels: int | None
    sample_rate: int | None
    size_bytes: int | None = None
    size_source: str | None = None  # "tags" (exakt laut Statistik-Tags) | "bitrate" (Bitrate × Laufzeit)
    bit_rate: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Analysis:
    ok: bool
    error: str | None
    duration: float | None
    title: str | None
    tracks: list[Track]

    @property
    def audio(self) -> list[Track]:
        return [t for t in self.tracks if t.type == "audio"]

    @property
    def subtitles(self) -> list[Track]:
        return [t for t in self.tracks if t.type == "subtitles"]


def codec_family(codec_id: str | None, codec: str | None) -> str | None:
    """Kurzname der Codecfamilie, wie er im Codecfilter verwendet wird (AC3, DTS, DTS-HD, TRUEHD …)."""
    cid = (codec_id or "").upper()
    name = (codec or "").upper()
    if cid.startswith("A_EAC3") or "E-AC-3" in name:
        return "EAC3"
    if cid.startswith("A_AC3"):
        return "AC3"
    if cid.startswith("A_DTS"):
        return "DTS-HD" if ("DTS-HD" in name or "MASTER" in name or "DTS:X" in name) else "DTS"
    if cid.startswith("A_TRUEHD") or "TRUEHD" in name:
        return "TRUEHD"
    if cid.startswith("A_AAC"):
        return "AAC"
    if cid.startswith("A_FLAC"):
        return "FLAC"
    if cid.startswith("A_PCM"):
        return "PCM"
    if cid.startswith("A_OPUS"):
        return "OPUS"
    if cid.startswith("A_VORBIS"):
        return "VORBIS"
    if cid.startswith("A_MPEG/L3"):
        return "MP3"
    if cid.startswith("A_MPEG/L2"):
        return "MP2"
    if cid in ("S_TEXT/UTF8", "S_TEXT/ASCII"):
        return "SRT"
    if cid in ("S_TEXT/ASS", "S_TEXT/SSA", "S_ASS", "S_SSA"):
        return "ASS"
    if cid == "S_HDMV/PGS":
        return "PGS"
    if cid == "S_VOBSUB":
        return "VOBSUB"
    if cid == "S_DVBSUB":
        return "DVB"
    if cid.startswith("S_TEXT/WEBVTT"):
        return "WEBVTT"
    if cid.startswith("V_"):
        return cid[2:].split("/")[0] if "/" in cid else cid[2:]
    return (codec or cid or "?").upper()[:16]


def parse_identify(data: dict) -> Analysis:
    container = data.get("container") or {}
    if not container.get("recognized", False) or not container.get("supported", False):
        errs = "; ".join(str(e) for e in data.get("errors") or []) or "Container nicht erkannt/unterstützt"
        return Analysis(False, errs, None, None, [])
    props = container.get("properties") or {}
    tracks = []
    for t in data.get("tracks") or []:
        p = t.get("properties") or {}
        tracks.append(Track(
            track_id=int(t.get("id", 0)),
            type=t.get("type") or "unknown",
            codec=t.get("codec"),
            codec_id=p.get("codec_id"),
            codec_family=codec_family(p.get("codec_id"), t.get("codec")),
            language=normalize_language(p.get("language") or p.get("language_ietf")),
            language_ietf=p.get("language_ietf"),
            name=p.get("track_name"),
            is_default=bool(p.get("default_track")),
            is_forced=bool(p.get("forced_track")),
            channels=p.get("audio_channels"),
            sample_rate=p.get("audio_sampling_frequency"),
        ))
    dur = props.get("duration")
    return Analysis(True, None, dur / 1e9 if dur else None, props.get("title"), tracks)


async def identify(path: str, control: ScanControl | None = None, limits: ResourceLimits | None = None) -> Analysis:
    toolset = get_toolset()
    if not toolset.mkvmerge.available:
        return Analysis(False, "MKVToolNix (mkvmerge) ist nicht installiert", None, None, [])
    out: list[str] = []
    res = await run_process([toolset.mkvmerge.path or "mkvmerge", "--ui-language", "en_US", "-J", path],
                            control or ScanControl(), limits or ResourceLimits(nice=5), on_stdout=out.append)
    if res.cancelled:
        return Analysis(False, "abgebrochen", None, None, [])
    try:
        data = json.loads("\n".join(out))
    except json.JSONDecodeError:
        return Analysis(False, f"mkvmerge -J lieferte keine Ausgabe (Exit-Code {res.returncode})", None, None, [])
    analysis = parse_identify(data)
    if analysis.ok:
        await enrich_sizes(analysis, path, control or ScanControl(), limits or ResourceLimits(nice=5))
    return analysis


# Codecs mit (nahezu) konstanter Bitrate: Bitrate × Laufzeit ist eine belastbare Schätzung
CBR_FAMILIES = {"AC3", "EAC3", "DTS", "PCM", "MP2"}


def _duration_tag(tags: dict) -> float | None:
    for k, v in tags.items():
        if k.upper().startswith("DURATION") and v:
            try:
                h, m, sec = str(v).split(":")
                return int(h) * 3600 + int(m) * 60 + float(sec)
            except ValueError:
                return None
    return None


def apply_stream_sizes(analysis: Analysis, streams: list[dict]) -> None:
    """Ergänzt Spurgrößen aus FFprobe-Streamdaten (Index = mkvmerge-Track-ID)."""
    type_map = {"video": "video", "audio": "audio", "subtitles": "subtitle"}
    by_index = {int(s.get("index", -1)): s for s in streams}
    for t in analysis.tracks:
        s = by_index.get(t.track_id)
        if s is None or (t.type in type_map and s.get("codec_type") != type_map[t.type]):
            continue  # Zuordnung nicht eindeutig -> keine Angabe
        tags = {str(k).upper(): v for k, v in (s.get("tags") or {}).items()}
        nob = next((v for k, v in tags.items() if k.startswith("NUMBER_OF_BYTES")), None)
        bps = next((v for k, v in tags.items() if k == "BPS" or k.startswith("BPS-")), None)
        try:
            t.bit_rate = int(s.get("bit_rate")) if s.get("bit_rate") not in (None, "N/A") else (int(bps) if bps else None)
        except (TypeError, ValueError):
            t.bit_rate = None
        if nob:
            try:
                t.size_bytes, t.size_source = int(nob), "tags"
                continue
            except ValueError:
                pass
        dur = _duration_tag(tags) or analysis.duration
        if t.bit_rate and dur and (t.codec_family or "").upper() in CBR_FAMILIES:
            t.size_bytes, t.size_source = int(t.bit_rate * dur / 8), "bitrate"


async def enrich_sizes(analysis: Analysis, path: str, control: ScanControl, limits: ResourceLimits) -> None:
    toolset = get_toolset()
    if not toolset.ffprobe.available:
        return
    out: list[str] = []
    res = await run_process([toolset.ffprobe.path or "ffprobe", "-v", "error", "-show_streams", "-of", "json",
                             "file:" + path], control, limits, on_stdout=out.append)
    if res.cancelled or res.returncode != 0:
        return
    try:
        apply_stream_sizes(analysis, json.loads("\n".join(out)).get("streams") or [])
    except json.JSONDecodeError:
        return
