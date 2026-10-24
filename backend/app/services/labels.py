"""Deutsche Bezeichnungen für Status, Methoden und Genauigkeiten."""

from ..scanners.base import ERROR_TYPES

STATUS_LABELS = {
    "unchecked": "Nicht geprüft",
    "ok": "Ohne erkannte Fehler",
    "warning": "Warnung",
    "error": "Fehler",
    "critical": "Kritisch",
    "incomplete": "Nicht vollständig geprüft",
}
SEVERITY_LABELS = {
    "info": "Info",
    "incomplete": "Nicht geprüft",
    "warning": "Warnung",
    "error": "Fehler",
    "critical": "Kritisch",
}
CHECK_LEVEL_LABELS = {
    "none": "Keine",
    "quick": "Nur Schnellprüfung",
    "container": "Containerprüfung",
    "full": "Vollständige Decodierung",
    "container_full": "Container + vollständige Decodierung",
}
METHOD_LABELS = {"quick": "Schnellprüfung", "container": "Erweiterte Containerprüfung", "full": "Vollständige Prüfung"}
PRECISION_LABELS = {
    "exact": "exakt (Container-/Paketzeitstempel)",
    "range": "Bereich (zwischen gemessenen Zeitstempeln)",
    "approx": "ungefähr (Decoderfortschritt)",
    "unknown": "nicht bestimmbar",
}
OUTCOME_LABELS = {
    "completed": "vollständig",
    "aborted": "abgebrochen",
    "interrupted": "unterbrochen",
    "modified": "Datei verändert",
    "tool_failed": "Werkzeug fehlgeschlagen",
    "unsupported": "nicht unterstützt",
    "permission_denied": "keine Berechtigung",
    "not_found": "nicht gefunden",
    "partial": "teilweise (nicht alle Streams prüfbar)",
}


def error_type_label(t: str) -> str:
    return ERROR_TYPES.get(t, t)


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = max(seconds, 0.0)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}" if sec % 1 else f"{h:02d}:{m:02d}:{int(sec):02d}"


def fmt_position(start: float | None, end: float | None, precision: str) -> str:
    if precision == "unknown" or start is None:
        return "Position nicht bestimmbar"
    prefix = "ca. " if precision == "approx" else ""
    if end is not None and abs(end - start) > 0.0005:
        return f"{prefix}{fmt_time(start)} – {fmt_time(end)}"
    return f"{prefix}{fmt_time(start)}"


def fmt_size(n: int | None) -> str:
    if n is None:
        return ""
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    v = float(n)
    for u in units:
        if v < 1000 or u == units[-1]:
            return (f"{v:.0f} {u}" if u == "B" else f"{v:.2f} {u}").replace(".", ",")
        v /= 1000
    return str(n)
