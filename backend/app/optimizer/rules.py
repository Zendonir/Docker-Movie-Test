"""Regeln für das Entfernen von Spuren und Berechnung des Remux-Plans (reine Logik, ohne I/O)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .languages import language_label, normalize_language
from .tracks import Analysis, Track


class TrackRule(BaseModel):
    mode: Literal["language", "track_id"] = "language"
    languages: list[str] = Field(default_factory=list, max_length=100)
    codecs: list[str] = Field(default_factory=list, max_length=50)
    track_ids: list[int] = Field(default_factory=list, max_length=200)

    @field_validator("languages")
    @classmethod
    def _langs(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for x in v:
            n = normalize_language(x)
            if n not in out:
                out.append(n)  # Reihenfolge = gewünschte Spurreihenfolge
        return out

    @field_validator("codecs")
    @classmethod
    def _codecs(cls, v: list[str]) -> list[str]:
        return [c.strip().upper() for c in v if c and c.strip()]

    @field_validator("track_ids")
    @classmethod
    def _ids(cls, v: list[int]) -> list[int]:
        if any(i < 0 or i > 1000 for i in v):
            raise ValueError("Ungültige Track-ID")
        return sorted(set(v))


class Rules(BaseModel):
    audio: TrackRule = Field(default_factory=lambda: TrackRule(languages=["ger"]))
    subtitles: TrackRule = Field(default_factory=lambda: TrackRule(languages=["ger"]))


@dataclass
class FileOps:
    """Momentaufnahme der Dateioperationen zum Zeitpunkt der Auftragserstellung."""

    output_mode: str = "output_dir"
    suffix: str = ".optimiert"
    backup_dir: str = ".mis-originale"
    remove_metadata: bool = False
    reorder_tracks: bool = True
    set_default_track: bool = True
    protected_languages: list[str] = field(default_factory=lambda: ["ger"])
    verify_output: bool = True
    skip_damaged: bool = True

    @classmethod
    def from_settings(cls, s) -> "FileOps":
        return cls(output_mode=s.optimizer_output_mode, suffix=s.optimizer_suffix, backup_dir=s.optimizer_backup_dir,
                   remove_metadata=s.optimizer_remove_metadata, reorder_tracks=s.optimizer_reorder_tracks,
                   set_default_track=s.optimizer_set_default_track,
                   protected_languages=list(s.optimizer_protected_languages), verify_output=s.optimizer_verify_output,
                   skip_damaged=s.optimizer_skip_damaged)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TrackDecision:
    track_id: int
    type: str
    keep: bool
    reason: str


@dataclass
class RemuxPlan:
    decisions: list[TrackDecision]
    default_track: int | None = None
    track_order: list[int] | None = None
    changed: bool = False
    skip_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Vorhersage: Summe der bekannten Größen entfernter Spuren (Container-Overhead nicht berücksichtigt)
    predicted_saved: int | None = None
    predicted_exact: bool = False  # alle Größen aus Statistik-Tags
    predicted_unknown: int = 0  # entfernte Spuren ohne Größenangabe

    @property
    def kept(self) -> list[int]:
        return [d.track_id for d in self.decisions if d.keep]

    @property
    def removed(self) -> list[int]:
        return [d.track_id for d in self.decisions if not d.keep]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kept"] = self.kept
        d["removed"] = self.removed
        return d


def _select(tracks: list[Track], rule: TrackRule, protected: list[str], kind: str,
            warnings: list[str]) -> dict[int, str]:
    """Liefert {track_id: Begründung} der zu behaltenden Spuren."""
    keep: dict[int, str] = {}
    if rule.mode == "track_id":
        for t in tracks:
            if t.track_id in rule.track_ids:
                keep[t.track_id] = f"Track-ID {t.track_id} ausgewählt"
    else:
        wanted = list(rule.languages) + [p for p in protected if p not in rule.languages]
        groups: dict[str, list[Track]] = {}
        for t in tracks:
            if t.language in wanted:
                groups.setdefault(t.language, []).append(t)
        for lang, group in groups.items():
            chosen = group
            if rule.codecs and len(group) > 1:
                filtered = [t for t in group if (t.codec_family or "").upper() in rule.codecs]
                if filtered:
                    chosen = filtered
                else:
                    warnings.append(f"{kind}: Codecfilter passt auf keine {language_label(lang)}-Spur – alle behalten")
            for t in chosen:
                why = "Sprache " + language_label(lang)
                if lang in protected and lang not in rule.languages:
                    why += " (geschützt)"
                if rule.codecs and len(group) > 1 and t in chosen and chosen is not group:
                    why += f", Codec {t.codec_family}"
                keep[t.track_id] = why
    # Geschützte Sprachen nie vollständig entfernen (auch im Track-ID-Modus)
    for lang in protected:
        of_lang = [t for t in tracks if t.language == lang]
        if of_lang and not any(t.track_id in keep for t in of_lang):
            for t in of_lang:
                keep[t.track_id] = f"Sprache {language_label(lang)} ist geschützt"
            warnings.append(f"{kind}: geschützte Sprache {language_label(lang)} wurde beibehalten")
    return keep


def _order_key(t: Track, languages: list[str]) -> tuple[int, int]:
    try:
        return (languages.index(t.language), t.track_id)
    except ValueError:
        return (len(languages), t.track_id)


def compute_plan(analysis: Analysis, rules: Rules, ops: FileOps) -> RemuxPlan:
    if not analysis.ok:
        return RemuxPlan([], skip_reason=f"Datei nicht analysierbar: {analysis.error}")
    warnings: list[str] = []
    protected = [normalize_language(x) for x in ops.protected_languages]
    keep_audio = _select(analysis.audio, rules.audio, protected, "Audio", warnings)
    keep_subs = _select(analysis.subtitles, rules.subtitles, protected, "Untertitel", warnings)
    decisions: list[TrackDecision] = []
    for t in analysis.tracks:
        if t.type == "audio":
            decisions.append(TrackDecision(t.track_id, t.type, t.track_id in keep_audio,
                                           keep_audio.get(t.track_id, "nicht ausgewählt")))
        elif t.type == "subtitles":
            decisions.append(TrackDecision(t.track_id, t.type, t.track_id in keep_subs,
                                           keep_subs.get(t.track_id, "nicht ausgewählt")))
        else:
            decisions.append(TrackDecision(t.track_id, t.type, True, "Video/sonstige Spuren bleiben immer erhalten"))
    plan = RemuxPlan(decisions, warnings=warnings)
    if analysis.audio and not keep_audio:
        plan.skip_reason = "Es bliebe keine Audiospur übrig – Datei wird nicht verändert"
        return plan
    plan.changed = bool(plan.removed)
    predict_savings(plan, analysis)
    if not plan.changed:
        plan.skip_reason = "Keine Spur zu entfernen"
        return plan

    kept_audio = [t for t in analysis.audio if t.track_id in keep_audio]
    if ops.set_default_track and kept_audio and not any(t.is_default for t in kept_audio):
        first = sorted(kept_audio, key=lambda t: _order_key(t, rules.audio.languages))[0]
        plan.default_track = first.track_id

    if ops.reorder_tracks:
        kept = [t for t in analysis.tracks if t.track_id in plan.kept]
        video = [t for t in kept if t.type == "video"]
        audio = sorted([t for t in kept if t.type == "audio"], key=lambda t: _order_key(t, rules.audio.languages))
        subs = sorted([t for t in kept if t.type == "subtitles"],
                      key=lambda t: _order_key(t, rules.subtitles.languages))
        other = [t for t in kept if t.type not in ("video", "audio", "subtitles")]
        order = [t.track_id for t in video + audio + subs + other]
        if order != [t.track_id for t in kept]:
            plan.track_order = order
    return plan


def predict_savings(plan: RemuxPlan, analysis: Analysis) -> None:
    removed = [t for t in analysis.tracks if t.track_id in set(plan.removed)]
    known = [t for t in removed if t.size_bytes is not None]
    plan.predicted_unknown = len(removed) - len(known)
    plan.predicted_saved = sum(t.size_bytes for t in known) if known else (0 if not removed else None)
    plan.predicted_exact = bool(removed) and all(t.size_source == "tags" for t in removed)


def mkvmerge_args(plan: RemuxPlan, analysis: Analysis, ops: FileOps) -> list[str]:
    """Optionen für die Quelldatei (vor dem Eingabepfad einzufügen)."""
    args: list[str] = []
    audio_keep = [t.track_id for t in analysis.audio if t.track_id in plan.kept]
    subs_keep = [t.track_id for t in analysis.subtitles if t.track_id in plan.kept]
    if analysis.audio:
        args += ["--audio-tracks", ",".join(map(str, audio_keep))] if audio_keep else ["--no-audio"]
    if analysis.subtitles:
        args += ["--subtitle-tracks", ",".join(map(str, subs_keep))] if subs_keep else ["--no-subtitles"]
    if plan.default_track is not None:
        args += ["--default-track-flag", f"{plan.default_track}:1"]
    if ops.remove_metadata:
        args += ["--no-global-tags", "--no-track-tags"]
    return args


def global_args(plan: RemuxPlan, ops: FileOps, file_index: int = 0) -> list[str]:
    args: list[str] = []
    if ops.remove_metadata:
        args += ["--title", ""]
    if plan.track_order:
        args += ["--track-order", ",".join(f"{file_index}:{i}" for i in plan.track_order)]
    return args


def collect_choices(analyses: list[Analysis]) -> dict:
    """Gefundene Sprachen und Codecs (für die Vorbelegung der Auswahlfelder)."""
    out = {"audio_languages": {}, "audio_codecs": {}, "subtitle_languages": {}, "subtitle_codecs": {}}
    for a in analyses:
        for t in a.audio:
            out["audio_languages"][t.language] = out["audio_languages"].get(t.language, 0) + 1
            out["audio_codecs"][t.codec_family or "?"] = out["audio_codecs"].get(t.codec_family or "?", 0) + 1
        for t in a.subtitles:
            out["subtitle_languages"][t.language] = out["subtitle_languages"].get(t.language, 0) + 1
            out["subtitle_codecs"][t.codec_family or "?"] = out["subtitle_codecs"].get(t.codec_family or "?", 0) + 1
    return {k: [{"value": x, "label": language_label(x) if "languages" in k else x, "count": c}
                for x, c in sorted(v.items(), key=lambda kv: -kv[1])] for k, v in out.items()}
