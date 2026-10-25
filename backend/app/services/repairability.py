"""Reparaturbewertung: Was lässt sich per Remux beheben, was ist verloren?

Die Bewertung ist eine *Auslegung* der gespeicherten Befunde – sie verändert
keine Prüfergebnisse. Grundlage:

* Datenverlust (zerstörte Cluster, CRC-Fehler, fehlende Laufzeit am Ende,
  beschädigte Pakete, Decodierfehler) lässt sich durch Neu-Multiplexen nicht
  rückgängig machen → Datei ersetzen.
* Strukturfehler ohne Datenverlust (Index/Cues, Laufzeitangabe, Restdaten nach
  Segmentende, ungeordnete Zeitstempel) behebt ein Remux mit mkvmerge.
* Typische harmlose Werkzeugmeldungen (PGS-Untertitel ohne Startpaket, Dolby-
  Vision-Block-Additions, gleiche DTS bei TrueHD/E-AC-3 im Null-Muxer) zählen
  nicht als Befund.
"""

from __future__ import annotations

import re

CATEGORIES: dict[str, dict[str, str]] = {
    "replace": {
        "label": "Nicht mehr reparierbar",
        "action": "Datei neu beschaffen bzw. neu rippen. Ein Remux kann fehlende oder zerstörte Daten nicht wiederherstellen.",
    },
    "repair": {
        "label": "Reparatur empfohlen",
        "action": "Remux mit mkvmerge in eine neue Datei (behebt Index, Laufzeitangabe, Restdaten und Zeitstempel), danach erneut prüfen.",
    },
    "optional": {
        "label": "Kleinere Auffälligkeiten",
        "action": "In der Regel problemlos abspielbar. Remux optional, wenn der Player an dieser Stelle hakt.",
    },
    "ok": {"label": "Kein Handlungsbedarf", "action": "Keine oder nur harmlose Werkzeugmeldungen."},
    "unchecked": {
        "label": "Nicht ausreichend geprüft",
        "action": "Erweiterte Containerprüfung oder vollständige Decodierung ausführen.",
    },
}
ORDER = ("replace", "repair", "optional", "unchecked", "ok")

# Schwelle, ab der eine fehlende Spielzeit als Datenverlust gilt (Sekunden)
LOSS_SECONDS = 10.0
# Clusterzeitstempel dürfen durch verschachtelte Audio-/Videoblöcke geringfügig zurückgehen
CLUSTER_BACK_TOLERANCE = 1.0

LOSS_TYPES = {
    "cluster_damaged", "cluster_crc", "read_error", "file_unreadable", "truncated", "packet_corrupt",
    "corrupt_frame", "block_invalid", "matroska_structure", "ebml_structure", "ebml_header", "not_media",
    "no_streams",
}
DECODE_TYPES = {"video_decode_error", "audio_decode_error", "decode_error"}
FIXABLE_TYPES = {"index_invalid", "missing_index", "trailing_data", "element_missing", "demux_error"}
INCOMPLETE_TYPES = {"tool_failed", "decode_aborted", "scan_aborted", "file_modified", "remux_failed",
                    "permission_denied"}

_BACK = re.compile(r"rückwärts von (-?[\d.]+) s auf (-?[\d.]+) s")
_DTS = re.compile(r"increasing dts to muxer in stream \d+: (-?\d+) >= (-?\d+)")
_DEVIATION = re.compile(r"Abweichung ([+-]?[\d.]+) s")
_MISSING = re.compile(r"es fehlen ([\d.]+) s")
_JUMP = re.compile(r"Zeitstempelsprung um ([\d.]+) s")
_BENIGN_DECODE = ("ignoring pic cod ext",)


def classify_finding(f: dict) -> tuple[str, str | None]:
    """Liefert (Klasse, Begründung). Klassen: loss, fixable, minor, benign, incomplete."""
    et = f.get("error_type") or ""
    sev = f.get("severity") or "info"
    desc = f.get("description") or ""
    raw = f.get("raw_message") or ""
    if sev == "info":
        return "benign", None
    if et in INCOMPLETE_TYPES or sev == "incomplete":
        return "incomplete", None

    if et == "metadata_inconsistent" and ("Could not find codec parameters" in raw or "Spuren, FFprobe" in desc):
        return "benign", None  # PGS ohne Startpaket, Anhänge – keine Mediendaten betroffen
    if et == "tool_warning" and "Block Addition" in raw:
        return "benign", None  # Dolby-Vision-Zusatzdaten, die FFmpeg nicht kennt
    if et == "tool_warning" and "extends beyond end of segment" in raw:
        return "fixable", "Daten nach Segmentende"
    if et == "unsupported_codec":
        return ("benign", None) if f.get("stream_type") == "attachment" else ("incomplete", None)
    if et == "timestamp_invalid" and "non monotonically" in raw:
        pairs = _DTS.findall(raw)
        if pairs and all(a == b for a, b in pairs):
            return "benign", None  # gleiche DTS (TrueHD/E-AC-3 im Null-Muxer) – Werkzeugartefakt
        return "minor", "Ungeordnete Paketzeitstempel"

    if et == "timestamp_backwards":
        m = _BACK.search(desc)
        if m:
            delta = float(m.group(1)) - float(m.group(2))
            if delta < CLUSTER_BACK_TOLERANCE:
                return "benign", None  # zulässige Verschachtelung (ältere Validatorversion meldete das als Fehler)
            if delta >= LOSS_SECONDS:
                return "loss", f"Zeitstempel springt {delta:.0f} s zurück (Inhalt fehlt oder ist doppelt)"
            return "fixable", f"Zeitstempel springt {delta:.1f} s zurück"
        return ("minor", "Spurzeitstempel laufen kurz rückwärts") if sev == "warning" else ("fixable", "Zeitstempel rückwärts")

    if et == "timestamp_jump":
        m = _JUMP.search(desc)
        gap = float(m.group(1)) if m else 0.0
        if sev in ("error", "critical") or gap >= LOSS_SECONDS:
            return "loss", f"Lücke von {gap:.0f} s in der Spielzeit"
        return "minor", f"Zeitstempelsprung um {gap:.1f} s"
    if et == "duration_mismatch":
        m = _DEVIATION.search(desc)
        if m:
            dev = abs(float(m.group(1)))
            if dev >= LOSS_SECONDS:
                return "loss", f"Ende fehlt ({dev:.0f} s weniger Daten als angegeben)"
            return "fixable", "Laufzeitangabe falsch"
        return "minor", "Abweichende Streamlaufzeit"
    if et == "unexpected_eof":
        m = _MISSING.search(desc)
        miss = float(m.group(1)) if m else 0.0
        if miss >= LOSS_SECONDS or sev in ("error", "critical"):
            return "loss", f"Decodierung endet {miss:.0f} s vor dem Ende"
        return "minor", "Decodierung endet kurz vor dem Ende"

    if et in DECODE_TYPES:
        if any(p in raw for p in _BENIGN_DECODE):
            return "benign", None  # bekannte, folgenlose MPEG-2-Decodermeldung
        if sev in ("error", "critical"):
            return "loss", "Bildfehler/Decodierfehler"
        return "minor", "Decoderwarnung"
    if et in LOSS_TYPES:
        if sev in ("error", "critical"):
            return "loss", None
        return "minor", None
    if et in FIXABLE_TYPES:
        return ("loss", None) if sev == "critical" else ("fixable", None)
    if sev == "critical":
        return "loss", None
    if sev == "error":
        return "fixable", None
    return "minor", None


def assess(status: str | None, findings: list[dict]) -> dict:
    """Gesamtempfehlung für eine Datei aus ihren aktuellen Befunden."""
    classes: dict[str, dict[str, int]] = {}
    for f in findings:
        cls, reason = classify_finding(f)
        if cls == "benign":
            continue
        label = reason or f.get("error_type_label") or f.get("error_type") or "Befund"
        bucket = classes.setdefault(cls, {})
        bucket[label] = bucket.get(label, 0) + max(1, int(f.get("occurrences") or 1))
    if "loss" in classes:
        cat = "replace"
    elif "fixable" in classes:
        cat = "repair"
    elif (status in (None, "unchecked", "incomplete") and not classes) or ("incomplete" in classes and "minor" not in classes):
        cat = "unchecked"
    elif "minor" in classes:
        cat = "optional"
    else:
        cat = "ok"
    reasons: list[str] = []
    for cls in ("loss", "fixable", "minor", "incomplete"):
        for label, n in sorted(classes.get(cls, {}).items(), key=lambda kv: -kv[1]):
            reasons.append(f"{label} (×{n})" if n > 1 else label)
    return {"category": cat, "label": CATEGORIES[cat]["label"], "action": CATEGORIES[cat]["action"],
            "reasons": reasons[:6], "reason_text": "; ".join(reasons[:6])}
