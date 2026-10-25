"""Audiokonvertierung: Profile, Auswahl der Spuren, Befehle (ffmpeg codiert, mkvmerge setzt zusammen)."""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..scanners.tools import get_toolset
from .languages import language_label, normalize_language
from .tracks import Analysis, Track

CODECS: dict[str, dict] = {
    # Zielcodec: Encoderkandidaten, Codecfamilie (für Prüfung), Bitraten-Grenzen, max. Kanäle
    "ac3": {"encoders": ["ac3"], "family": "AC3", "label": "Dolby Digital (AC3)", "bitrate": (96, 640), "channels": 6},
    "eac3": {"encoders": ["eac3"], "family": "EAC3", "label": "Dolby Digital Plus (E-AC3)", "bitrate": (96, 1536), "channels": 8},
    "aac": {"encoders": ["aac"], "family": "AAC", "label": "AAC", "bitrate": (32, 512), "channels": 8},
    "flac": {"encoders": ["flac"], "family": "FLAC", "label": "FLAC (verlustfrei)", "bitrate": None, "channels": 8},
    "opus": {"encoders": ["libopus"], "family": "OPUS", "label": "Opus", "bitrate": (32, 512), "channels": 8},
    "mp3": {"encoders": ["libmp3lame"], "family": "MP3", "label": "MP3", "bitrate": (64, 320), "channels": 2},
}

PATTERN_TOKENS = ("{name}", "{codec}", "{profile}")


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    codec: Literal["ac3", "eac3", "aac", "flac", "opus", "mp3"]
    bitrate_kbps: int | None = Field(None, ge=32, le=1536)
    max_channels: int | None = Field(None, ge=1, le=8)
    sample_rate: int | None = Field(None, ge=8000, le=192000)
    source_codecs: list[str] = Field(default_factory=list, max_length=30)
    source_languages: list[str] = Field(default_factory=list, max_length=50)
    mode: Literal["replace", "add"] = "replace"
    track_name: str | None = Field(None, max_length=200)
    filename_pattern: str = Field("{name}", min_length=1, max_length=200)

    @field_validator("source_codecs")
    @classmethod
    def _codecs(cls, v: list[str]) -> list[str]:
        return [c.strip().upper() for c in v if c.strip()]

    @field_validator("source_languages")
    @classmethod
    def _langs(cls, v: list[str]) -> list[str]:
        return sorted({normalize_language(x) for x in v if x.strip()})

    @field_validator("filename_pattern")
    @classmethod
    def _pattern(cls, v: str) -> str:
        v = v.strip()
        if "/" in v or "\\" in v or "\x00" in v or ".." in v:
            raise ValueError("Dateinamensmuster darf keine Pfadangaben enthalten")
        rest = v
        for t in PATTERN_TOKENS:
            rest = rest.replace(t, "")
        if "{" in rest or "}" in rest:
            raise ValueError("Erlaubte Platzhalter: {name}, {codec}, {profile}")
        if "{name}" not in v:
            raise ValueError("Das Muster muss {name} enthalten")
        return v


BUILTIN_PROFILES: list[dict] = [
    {"name": "DTS/TrueHD → AC3 5.1 (640 kbit/s), ersetzen", "codec": "ac3", "bitrate_kbps": 640, "max_channels": 6,
     "source_codecs": ["DTS", "DTS-HD", "TRUEHD"], "mode": "replace", "track_name": "AC3 {channels}"},
    {"name": "DTS/TrueHD → AC3 5.1 (640 kbit/s), zusätzlich", "codec": "ac3", "bitrate_kbps": 640, "max_channels": 6,
     "source_codecs": ["DTS", "DTS-HD", "TRUEHD"], "mode": "add", "track_name": "AC3 {channels}"},
    {"name": "DTS/TrueHD → E-AC3 (1024 kbit/s), ersetzen", "codec": "eac3", "bitrate_kbps": 1024, "max_channels": 8,
     "source_codecs": ["DTS", "DTS-HD", "TRUEHD"], "mode": "replace", "track_name": "E-AC3 {channels}"},
    {"name": "FLAC/PCM → AC3 5.1 (640 kbit/s), ersetzen", "codec": "ac3", "bitrate_kbps": 640, "max_channels": 6,
     "source_codecs": ["FLAC", "PCM"], "mode": "replace", "track_name": "AC3 {channels}"},
    {"name": "Alle Spuren → AAC Stereo (192 kbit/s), zusätzlich", "codec": "aac", "bitrate_kbps": 192, "max_channels": 2,
     "source_codecs": [], "mode": "add", "track_name": "AAC Stereo"},
]


@lru_cache(maxsize=1)
def available_encoders() -> frozenset[str]:
    ts = get_toolset()
    if not ts.ffmpeg.available:
        return frozenset()
    try:
        out = subprocess.run([ts.ffmpeg.path or "ffmpeg", "-hide_banner", "-encoders"], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(m.group(1) for m in re.finditer(r"^\s[A-Z.]{6}\s+(\S+)", out, re.M))


def encoder_for(codec: str) -> str | None:
    enc = available_encoders()
    for e in CODECS[codec]["encoders"]:
        if e in enc:
            return e
    return None


def channel_label(n: int | None) -> str:
    return {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(n or 0, f"{n} Kanäle" if n else "")


@dataclass
class Conversion:
    track_id: int
    language: str
    source_codec: str | None
    channels_in: int | None
    channels_out: int | None
    is_default: bool
    track_name: str


@dataclass
class AudioPlan:
    conversions: list[Conversion] = field(default_factory=list)
    mode: str = "replace"
    skip_reason: str | None = None
    track_order: list[str] = field(default_factory=list)  # "datei:track"
    # Vorhersage der Größenänderung (positiv = Ersparnis, negativ = Datei wird größer)
    predicted_saved: int | None = None
    predicted_exact: bool = False
    predicted_unknown: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.conversions)

    def to_dict(self) -> dict:
        return asdict(self)


def compute_audio_plan(analysis: Analysis, profile: dict) -> AudioPlan:
    if not analysis.ok:
        return AudioPlan(skip_reason=f"Datei nicht analysierbar: {analysis.error}")
    codec = profile["codec"]
    target_family = CODECS[codec]["family"]
    sources = [c.upper() for c in profile.get("source_codecs") or []]
    langs = [normalize_language(x) for x in profile.get("source_languages") or []]
    plan = AudioPlan(mode=profile.get("mode", "replace"))
    for t in analysis.audio:
        fam = (t.codec_family or "").upper()
        if sources and fam not in sources:
            continue
        if langs and t.language not in langs:
            continue
        if fam == target_family and not sources:
            continue  # bereits im Zielformat
        max_ch = profile.get("max_channels") or CODECS[codec]["channels"]
        max_ch = min(max_ch, CODECS[codec]["channels"])
        ch_out = min(t.channels, max_ch) if t.channels else None
        name_tpl = profile.get("track_name") or "{codec} {channels}"
        name = name_tpl.replace("{codec}", codec.upper()).replace("{channels}", channel_label(ch_out)) \
            .replace("{lang}", language_label(t.language)).strip()
        plan.conversions.append(Conversion(t.track_id, t.language, t.codec_family, t.channels, ch_out,
                                           t.is_default, name))
    if not plan.conversions:
        plan.skip_reason = "Keine passende Audiospur für dieses Profil"
        return plan
    _predict_audio(plan, analysis, profile)
    conv_index = {c.track_id: i + 1 for i, c in enumerate(plan.conversions)}
    for t in analysis.tracks:
        if t.track_id in conv_index:
            if plan.mode == "replace":
                plan.track_order.append(f"{conv_index[t.track_id]}:0")
            else:
                plan.track_order += [f"0:{t.track_id}", f"{conv_index[t.track_id]}:0"]
        else:
            plan.track_order.append(f"0:{t.track_id}")
    return plan


def _predict_audio(plan: AudioPlan, analysis: Analysis, profile: dict) -> None:
    """Neue Spurgröße = Zielbitrate × Laufzeit; FLAC (variable Größe) ist nicht vorhersagbar."""
    by_id = {t.track_id: t for t in analysis.tracks}
    total, unknown = 0, 0
    codec = profile["codec"]
    bitrate = profile.get("bitrate_kbps") if CODECS[codec]["bitrate"] else None
    if bitrate:
        lo, hi = CODECS[codec]["bitrate"]
        bitrate = max(lo, min(hi, int(bitrate)))
    for c in plan.conversions:
        src = by_id.get(c.track_id)
        new = int(bitrate * 1000 * analysis.duration / 8) if bitrate and analysis.duration else None
        if new is None or (plan.mode == "replace" and (src is None or src.size_bytes is None)):
            unknown += 1
            continue
        total += (src.size_bytes - new) if plan.mode == "replace" else -new
    plan.predicted_unknown = unknown
    plan.predicted_saved = total if unknown < len(plan.conversions) else None
    plan.predicted_exact = False  # Encoderausgabe ist immer eine Schätzung


def ffmpeg_encode_cmd(src: str, track_id: int, conv: Conversion, profile: dict, out: str) -> list[str]:
    enc = encoder_for(profile["codec"])
    if enc is None:
        raise RuntimeError(f"FFmpeg hat keinen Encoder für {profile['codec']}")
    ts = get_toolset()
    cmd = [ts.ffmpeg.path or "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "repeat+level+warning",
           "-progress", "pipe:1", "-stats_period", "0.5", "-i", "file:" + src, "-map", f"0:{track_id}",
           "-vn", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1", "-c:a", enc]
    if profile.get("bitrate_kbps") and CODECS[profile["codec"]]["bitrate"]:
        lo, hi = CODECS[profile["codec"]]["bitrate"]
        cmd += ["-b:a", f"{max(lo, min(hi, int(profile['bitrate_kbps'])))}k"]
    if conv.channels_out and conv.channels_in and conv.channels_out < conv.channels_in:
        cmd += ["-ac", str(conv.channels_out)]
    if profile.get("sample_rate"):
        cmd += ["-ar", str(int(profile["sample_rate"]))]
    if profile["codec"] == "ac3" and not profile.get("sample_rate"):
        cmd += ["-ar", "48000"]  # AC3 unterstützt nur 32/44,1/48 kHz
    return cmd + ["-f", "matroska", "-n", out]


def mkvmerge_merge_args(plan: AudioPlan, analysis: Analysis, src: str, tmp_tracks: list[str],
                        remove_metadata: bool) -> list[str]:
    """Argumente nach "-o ZIEL": Quelldatei (ggf. ohne ersetzte Spuren) + codierte Spuren."""
    args: list[str] = []
    replaced = [c.track_id for c in plan.conversions] if plan.mode == "replace" else []
    keep_audio = [t.track_id for t in analysis.audio if t.track_id not in replaced]
    if analysis.audio:
        args += ["--audio-tracks", ",".join(map(str, keep_audio))] if keep_audio else ["--no-audio"]
    if remove_metadata:
        args += ["--no-global-tags", "--no-track-tags"]
    args.append(src)
    for conv, tmp in zip(plan.conversions, tmp_tracks):
        default = conv.is_default if plan.mode == "replace" else False
        args += ["--language", f"0:{conv.language}", "--track-name", f"0:{conv.track_name}",
                 "--default-track-flag", f"0:{1 if default else 0}", "--no-global-tags", "--no-chapters", tmp]
    if remove_metadata:
        args += ["--title", ""]
    args += ["--track-order", ",".join(plan.track_order)]
    return args


def expected_after_conversion(plan: AudioPlan, analysis: Analysis, profile: dict) -> list[tuple[str, str | None]]:
    """Erwartete Spurtypen/Codecfamilien in der Ausgabedatei (in Spurreihenfolge)."""
    by_id: dict[int, Track] = {t.track_id: t for t in analysis.tracks}
    family = CODECS[profile["codec"]]["family"]
    out: list[tuple[str, str | None]] = []
    for ref in plan.track_order:
        f, tid = ref.split(":")
        if f == "0":
            t = by_id[int(tid)]
            out.append((t.type, t.codec_family))
        else:
            out.append(("audio", family))
    return out
