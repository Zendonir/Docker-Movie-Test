"""Gemeinsame Datentypen der Prüfverfahren."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Schweregrad einer einzelnen Feststellung.

    ``info`` wird protokolliert, beeinflusst aber nie den Dateistatus.
    ``incomplete`` bedeutet: Die Prüfung konnte (teilweise) nicht durchgeführt
    werden – das ist ausdrücklich *kein* Defektnachweis.
    """

    INFO = "info"
    INCOMPLETE = "incomplete"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.INCOMPLETE: 1,
    Severity.WARNING: 2,
    Severity.ERROR: 3,
    Severity.CRITICAL: 4,
}


class TimePrecision(str, Enum):
    """Wie belastbar eine Zeitangabe ist.

    exact   – aus Zeitstempeln des Containers/Pakets (z. B. Block-/Clusterzeitstempel, DTS)
    range   – Bereich zwischen zwei gemessenen Container-Zeitstempeln
    approx  – Bereich aus dem Fortschritt des Decoders (keine framegenaue Position)
    unknown – keine Zeitposition ermittelbar; darf nicht auf der Zeitleiste erscheinen
    """

    EXACT = "exact"
    RANGE = "range"
    APPROX = "approx"
    UNKNOWN = "unknown"


class Method(str, Enum):
    QUICK = "quick"
    CONTAINER = "container"
    FULL = "full"


METHOD_LABELS = {
    Method.QUICK: "Schnellprüfung",
    Method.CONTAINER: "Erweiterte Containerprüfung",
    Method.FULL: "Vollständige Decodierprüfung",
}

# Bei inhaltlichen Änderungen eines Prüfverfahrens wird die Version erhöht.
# Ergebnisse älterer Versionen gelten bei inkrementellen Scans als veraltet.
SCANNER_VERSIONS = {
    Method.QUICK: 1,
    Method.CONTAINER: 2,  # v2: geringfügig rückwärts laufende Clusterzeitstempel sind kein Fehler mehr
    Method.FULL: 1,
}


class Outcome(str, Enum):
    COMPLETED = "completed"  # Prüfung vollständig durchgeführt
    ABORTED = "aborted"  # vom Benutzer abgebrochen
    INTERRUPTED = "interrupted"  # Neustart/Absturz während der Prüfung
    MODIFIED = "modified"  # Datei während der Prüfung verändert
    TOOL_FAILED = "tool_failed"  # Prüfwerkzeug fehlgeschlagen (kein Mediendefekt)
    UNSUPPORTED = "unsupported"  # Codec/Format nicht prüfbar
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    PARTIAL = "partial"  # Prüfung lief, aber einzelne Streams waren nicht prüfbar


# Fehlertypen mit deutscher Bezeichnung. Die Schweregrade werden von den
# Analysatoren vergeben; hier stehen nur die Anzeigenamen.
ERROR_TYPES: dict[str, str] = {
    "file_unreadable": "Datei nicht lesbar",
    "read_error": "Nicht lesbarer Dateibereich (E/A-Fehler)",
    "permission_denied": "Unzureichende Zugriffsrechte",
    "not_media": "Container nicht erkannt",
    "ebml_header": "Fehlerhafter EBML-Header",
    "ebml_structure": "Fehlerhafte EBML-Struktur",
    "matroska_structure": "Matroska-Strukturfehler",
    "cluster_damaged": "Beschädigter Cluster",
    "cluster_crc": "CRC-Prüfsumme eines Clusters falsch",
    "element_missing": "Fehlendes Pflichtelement",
    "block_invalid": "Ungültiger Block",
    "truncated": "Datei abgeschnitten / vorzeitiges Dateiende",
    "trailing_data": "Daten nach Segmentende",
    "timestamp_backwards": "Zeitstempel läuft rückwärts",
    "timestamp_jump": "Zeitstempelsprung",
    "timestamp_gap": "Lücke im Stream",
    "timestamp_invalid": "Fehlerhafte Zeitstempel",
    "duration_mismatch": "Laufzeitabweichung",
    "metadata_inconsistent": "Inkonsistente Metadaten",
    "index_invalid": "Fehlerhafter Index (Cues/SeekHead)",
    "demux_error": "Fehler beim Demuxing",
    "packet_corrupt": "Beschädigtes oder nicht lesbares Medienpaket",
    "video_decode_error": "Videodecodierungsfehler",
    "corrupt_frame": "Beschädigter Videoframe",
    "audio_decode_error": "Audiodecodierungsfehler",
    "decode_error": "Decodierungsfehler",
    "decode_aborted": "Decodierung abgebrochen",
    "unexpected_eof": "Unerwartetes Dateiende",
    "unsupported_codec": "Nicht unterstützter Codec",
    "tool_failed": "Prüfwerkzeug fehlgeschlagen",
    "tool_warning": "Werkzeugwarnung (nicht eindeutig)",
    "tool_error": "Werkzeugfehlermeldung",
    "structure_warning": "Strukturwarnung (nicht eindeutig)",
    "file_modified": "Datei während der Prüfung verändert",
    "scan_aborted": "Prüfung abgebrochen",
    "remux_failed": "Diagnose-Remux fehlgeschlagen",
    "no_streams": "Keine Medienstreams",
    "missing_index": "Kein Index vorhanden",
}


@dataclass
class Finding:
    error_type: str
    severity: Severity
    description: str
    tool: str
    stream_index: int | None = None
    stream_type: str | None = None
    time_start: float | None = None
    time_end: float | None = None
    time_precision: TimePrecision = TimePrecision.UNKNOWN
    time_source: str | None = None
    byte_offset: int | None = None
    byte_end: int | None = None
    raw_message: str | None = None
    occurrences: int = 1
    heuristic: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.severity = Severity(self.severity)
        self.time_precision = TimePrecision(self.time_precision)
        if self.time_start is None:
            self.time_precision = TimePrecision.UNKNOWN
            self.time_end = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["time_precision"] = self.time_precision.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Finding":
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class MethodResult:
    method: Method
    outcome: Outcome
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    tools: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    decoded_duration: float | None = None
    container_duration: float | None = None
    log_path: str | None = None
    raw_message_count: int = 0

    @property
    def worst(self) -> Severity:
        worst = Severity.INFO
        for f in self.findings:
            if SEVERITY_RANK[f.severity] > SEVERITY_RANK[worst]:
                worst = f.severity
        return worst

    def status(self) -> str:
        """Gesamtergebnis dieser Methode.

        Ein "ok" ist nur möglich, wenn die Prüfung vollständig abgeschlossen
        wurde. Erkannte Defekte bleiben auch bei abgebrochener Prüfung
        sichtbar, ein abgebrochener Lauf ohne Befund ist dagegen "incomplete".
        """

        worst = self.worst
        if worst in (Severity.CRITICAL, Severity.ERROR):
            return worst.value
        if self.outcome != Outcome.COMPLETED:
            return "incomplete"
        if worst == Severity.WARNING:
            return "warning"
        if worst == Severity.INCOMPLETE:
            return "incomplete"
        return "ok"


def max_severity(*values: Severity) -> Severity:
    return max(values, key=lambda s: SEVERITY_RANK[s])
