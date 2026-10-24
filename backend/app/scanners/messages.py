"""Auswertung der Meldungen von FFmpeg, FFprobe und MKVToolNix.

Die Regeln sind bewusst konservativ: Nur Meldungen, die nachweislich auf
beschädigte Daten hinweisen, werden als Fehler eingestuft. Unbekannte
Meldungen werden als nicht eindeutige Warnung bzw. nur als Info geführt und
nie als zweifelsfreier Defekt dargestellt. Die Originalmeldung wird immer
gespeichert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .base import Severity

# ---------------------------------------------------------------------------
# FFmpeg-Zeilen: "[ctx @ 0x55..] [level] message" oder "[level] message"
FFMPEG_LINE = re.compile(
    r"^(?:\[(?P<ctx>[^\]@]+?)(?: @ 0x[0-9a-fA-F]+)?\] )?\[(?P<level>panic|fatal|error|warning|info|verbose|debug|trace)\] (?P<msg>.*)$"
)
STREAM_CTX = re.compile(r"^(?P<kind>[vasdt])(?:ist|ost)#(?P<file>\d+):(?P<stream>\d+)(?:/(?P<codec>[\w-]+))?$")
DEMUX_CTX_PREFIX = ("in#",)
OUTPUT_CTX = ("out#", "null", "wrapped_avframe")

# Nur Containerformate der unterstützten Dateitypen. Namen wie "h264" oder "aac" sind
# bewusst nicht enthalten: Sie bezeichnen in unseren Containern immer den Decoder.
KNOWN_DEMUXERS = {
    "matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2", "mpegts", "avi", "mpeg", "flv", "asf", "ogg", "wav",
}


@dataclass
class ParsedLine:
    level: str
    ctx: str | None
    msg: str
    raw: str
    stream_index: int | None = None
    stream_kind: str | None = None
    codec: str | None = None
    ctx_role: str = "other"  # demuxer, decoder, stream, output, other


def parse_ffmpeg_line(raw: str, decoders: set[str] | None = None) -> ParsedLine | None:
    line = raw.rstrip("\r\n")
    if not line.strip():
        return None
    m = FFMPEG_LINE.match(line)
    if not m:
        return ParsedLine(level="unknown", ctx=None, msg=line, raw=line)
    ctx = (m.group("ctx") or "").strip() or None
    p = ParsedLine(level=m.group("level"), ctx=ctx, msg=m.group("msg"), raw=line)
    if ctx:
        # "in#0/matroska,webm" -> Demuxer; "vist#0:1/h264" -> Eingangsstream
        if ctx.startswith(DEMUX_CTX_PREFIX):
            p.ctx_role = "demuxer"
        sm = STREAM_CTX.match(ctx)
        if sm:
            if "ist" in ctx.split("#")[0]:
                p.ctx_role = "stream"
                p.stream_index = int(sm.group("stream"))
                p.stream_kind = {"v": "video", "a": "audio", "s": "subtitle", "d": "data", "t": "attachment"}[sm.group("kind")]
                p.codec = sm.group("codec")
            else:
                p.ctx_role = "output"
        elif ctx in KNOWN_DEMUXERS:
            p.ctx_role = "demuxer"
        elif ctx.startswith(OUTPUT_CTX):
            p.ctx_role = "output"
        elif ctx.startswith("dec:"):
            p.ctx_role = "decoder"
            p.codec = ctx[4:]
        elif decoders is not None and ctx in decoders:
            p.ctx_role = "decoder"
            p.codec = ctx
        elif decoders is None and re.fullmatch(r"[a-z0-9_]+", ctx) and ctx not in ("null", "file", "AVIOContext"):
            p.ctx_role = "decoder"
            p.codec = ctx
    return p


@dataclass
class Rule:
    pattern: re.Pattern
    error_type: str
    severity: Severity | None  # None = Schweregrad nach Kontext
    description: str
    roles: tuple[str, ...] | None = None
    heuristic: bool = False
    stream_group: str | None = None  # Name der Regex-Gruppe mit Streamindex
    offset_group: str | None = None
    offset_hex: bool = False
    dts_group: str | None = None
    start_artifact: bool = False  # typisch für geschnittene Aufnahmen, wenn ganz am Anfang


def R(pattern: str, error_type: str, severity: Severity | None, description: str, **kw) -> Rule:
    return Rule(re.compile(pattern, re.IGNORECASE), error_type, severity, description, **kw)


S = Severity

# Reihenfolge ist relevant: erste passende Regel gewinnt.
FFMPEG_RULES: list[Rule] = [
    # --- Harmlos / rein informativ ---------------------------------------
    R(r"Guessed Channel Layout|not enough frames to estimate rate|Could not update timestamps for (skipped|discarded) samples"
      r"|Estimating duration from bitrate|Discarding ID3 tags|Increasing reorder buffer|max_analyze_duration .* reached"
      r"|Thread message queue blocking|deprecated pixel format|No pixel format specified"
      r"|Consider increasing the value for the 'analyzeduration'|Late SEI is not implemented|Automatically inserted bitstream filter"
      r"|Starting new cluster|Changing video frame properties on the fly|Application provided duration"
      r"|SEI type \d+ size \d+ truncated|Invalid return value 0 for stream protocol|Stream \S+ of \d+ is a .* stream"
      r"|unused SEI|frame size not set|Multiple frames in a packet|Skipping NAL unit",
      "info", S.INFO, "Informative Meldung ohne Hinweis auf Beschädigung"),
    # --- Nicht unterstützte Codecs (kein Defekt) ---------------------------
    R(r"Decoding requested, but no decoder found for: (?P<codec>\S+)", "unsupported_codec", S.INCOMPLETE,
      "Für diesen Stream ist kein Decoder verfügbar – der Stream wurde nicht geprüft."),
    R(r"Decoder \(codec (?P<codec>\S+)\) not found for input stream #\d+:(?P<stream>\d+)", "unsupported_codec", S.INCOMPLETE,
      "Für diesen Stream ist kein Decoder verfügbar – der Stream wurde nicht geprüft.", stream_group="stream"),
    R(r"Error while opening decoder for input stream #\d+:(?P<stream>\d+)", "unsupported_codec", S.INCOMPLETE,
      "Der Decoder konnte nicht initialisiert werden (nicht unterstützte Variante oder defekte Initialisierungsdaten).",
      stream_group="stream"),
    R(r"Could not find codec parameters for stream (?P<stream>\d+) .*unknown codec", "unsupported_codec", S.INCOMPLETE,
      "Unbekannter Codec – der Stream kann nicht decodiert werden.", stream_group="stream"),
    R(r"Could not find codec parameters for stream (?P<stream>\d+)", "metadata_inconsistent", S.WARNING,
      "Codecparameter des Streams konnten nicht vollständig ermittelt werden.", stream_group="stream", heuristic=True),
    # --- Lesbarkeit / Abschneiden ----------------------------------------
    R(r"File ended prematurely", "truncated", S.CRITICAL,
      "Vorzeitiges Dateiende – die Datei ist abgeschnitten oder ein Element reicht über das Dateiende hinaus."),
    R(r"stream \d+, offset 0x(?P<off>[0-9a-f]+): partial file", "truncated", S.CRITICAL,
      "Unvollständige Datei: Mediendaten liegen hinter dem Dateiende.", offset_group="off", offset_hex=True),
    R(r"moov atom not found", "file_unreadable", S.CRITICAL,
      "MP4/MOV-Index (moov) fehlt – die Datei ist nicht abspielbar (häufig: unvollständiger Download)."),
    R(r"Read error at pos\. (?P<off>\d+)", "read_error", S.CRITICAL, "Lesefehler beim Demuxen.", offset_group="off"),
    R(r"Input/output error|I/O error", "read_error", S.CRITICAL, "Ein-/Ausgabefehler beim Lesen der Datei."),
    R(r"EBML header parsing failed|Invalid EBML number size tag", "ebml_header", S.CRITICAL,
      "EBML-Header konnte nicht gelesen werden."),
    # --- Matroska-Struktur -------------------------------------------------
    R(r"Element at 0x(?P<off>[0-9a-f]+) ending at 0x[0-9a-f]+ exceeds containing master element", "matroska_structure",
      S.ERROR, "Ein Matroska-Element reicht über sein übergeordnetes Element hinaus (zerstörte Struktur).",
      offset_group="off", offset_hex=True),
    R(r"at pos (?P<off>\d+) \(0x[0-9a-f]+\) invalid as first byte of an EBML number", "ebml_structure", S.ERROR,
      "Ungültige EBML-Zahl – die Containerstruktur ist an dieser Stelle zerstört.", offset_group="off"),
    R(r"indicated by an EBML number's first byte 0x[0-9a-f]+ at pos (?P<off>\d+)", "ebml_structure", S.ERROR,
      "Ungültige EBML-Längenangabe – die Containerstruktur ist an dieser Stelle zerstört.", offset_group="off"),
    R(r"Unknown-sized element at 0x(?P<off>[0-9a-f]+) inside parent with finite size|Found unknown-length element",
      "matroska_structure", S.ERROR, "Element mit unbekannter Größe an unzulässiger Stelle.", offset_group="off",
      offset_hex=True),
    R(r"Duplicate element|Unknown entry 0x[0-9A-F]+ in info header", "structure_warning", S.WARNING,
      "Ungewöhnliche Containerstruktur.", heuristic=True),
    R(r"Seek to desired resync point failed", "demux_error", S.ERROR, "Der Demuxer musste sich neu synchronisieren."),
    # --- Pakete / Demuxing -------------------------------------------------
    R(r"Packet corrupt \(stream = (?P<stream>\d+), dts = (?P<dts>-?\d+|NOPTS)\)", "packet_corrupt", S.ERROR,
      "Beschädigtes Medienpaket (vom Demuxer erkannt).", stream_group="stream", dts_group="dts"),
    R(r"corrupt input packet in stream (?P<stream>\d+)", "packet_corrupt", S.ERROR,
      "Beschädigtes Eingangspaket.", stream_group="stream"),
    R(r"Truncating packet of size|Packet had \d+ bytes left|PES packet size mismatch|Invalid frame size|truncated packet",
      "packet_corrupt", S.ERROR, "Unvollständiges oder fehlerhaftes Medienpaket."),
    R(r"Continuity check failed", "packet_corrupt", S.WARNING, "Kontinuitätsfehler im Transportstrom (Paketverlust).",
      heuristic=True),
    R(r"Error during demuxing|Error retrieving a packet from demuxer", "demux_error", S.ERROR,
      "Fehler beim Demuxing – nicht alle Pakete konnten gelesen werden."),
    # --- Zeitstempel -------------------------------------------------------
    R(r"non monotonically increasing dts|Non-monoton(ous|ic) DTS|DTS \d+ < \d+ out of order|Invalid DTS|Invalid timestamps"
      r"|Timestamps are unset in a packet|pts has no value|invalid dropping|Discontinuity|timestamp discontinuity",
      "timestamp_invalid", S.WARNING, "Auffällige oder fehlerhafte Zeitstempel.", heuristic=True),
    R(r"Format .* detected only with low score", "metadata_inconsistent", S.WARNING,
      "Das Containerformat konnte nur unsicher erkannt werden.", heuristic=True),
    # --- Video-Decoder -------------------------------------------------------
    R(r"non-existing PPS \d+ referenced|decode_slice_header error|no frame!|sps_id \d+ out of range|PPS id out of range",
      "video_decode_error", S.ERROR,
      "Videodaten verweisen auf fehlende Parametersätze (SPS/PPS).", start_artifact=True),
    R(r"corrupt decoded frame", "corrupt_frame", S.ERROR, "Der Decoder hat einen beschädigten Videoframe ausgegeben."),
    R(r"concealing \d+ DC, \d+ AC, \d+ MV errors", "video_decode_error", S.ERROR,
      "Fehlerverschleierung im Videobild nötig (beschädigte Makroblöcke)."),
    R(r"error while decoding MB|Error at MB|corrupted macroblock|ac-tex damaged|illegal (dc|ac) vlc|marker does not match"
      r"|slice end not reached|cabac decode of qscale diff failed|left block unavailable|top block unavailable"
      r"|out of range intra chroma pred mode|negative number of zero coeffs|dquant out of range|Invalid level prefix"
      r"|Invalid NAL unit size|Error splitting the input into NAL units|missing picture in access unit"
      r"|reference picture missing during reorder|Missing reference picture|mmco: unref short failure"
      r"|Could not find ref with POC|Error parsing NAL unit|Two slices reporting being the first|Invalid UE golomb"
      r"|Cannot use next picture in error concealment|co located POCs unavailable|Frame num gap",
      "video_decode_error", S.ERROR, "Fehler beim Decodieren des Videostreams."),
    # --- Audio-Decoder -----------------------------------------------------
    R(r"Header missing|invalid new backstep|overread, skip|frame CRC mismatch|expacc \d+ is out-of-range"
      r"|error decoding the audio block|incomplete frame|Input buffer exhausted before END element found"
      r"|channel element \d+\.\d+ is not allocated|Reserved bit set|invalid band type|Number of bands .* exceeds limit"
      r"|Prediction is not allowed|Invalid core frame|Read past end of|Invalid data in|invalid frame header|CRC check failed"
      r"|skip_data_stream_element|Error decoding (AAC|audio) frame",
      "audio_decode_error", S.ERROR, "Fehler beim Decodieren des Audiostreams."),
    # --- Allgemeine Decoderfehler --------------------------------------------
    R(r"Error submitting packet to decoder|Error while decoding stream #\d+:(?P<stream>\d+)|Decoding error",
      "decode_error", S.ERROR, "Der Decoder hat ein Paket als fehlerhaft abgelehnt.", stream_group="stream"),
    R(r"Invalid data found when processing input", "demux_error", S.ERROR,
      "Ungültige Daten beim Verarbeiten der Eingabe."),
]

BENIGN_FFMPEG_CONTEXTS = ("AVIOContext",)


@dataclass
class Classified:
    error_type: str
    severity: Severity
    description: str
    heuristic: bool
    stream_index: int | None = None
    stream_kind: str | None = None
    codec: str | None = None
    byte_offset: int | None = None
    dts: int | None = None
    start_artifact: bool = False
    rule_matched: bool = True
    extra: dict = field(default_factory=dict)


def classify_ffmpeg(p: ParsedLine) -> Classified | None:
    """Ordnet eine FFmpeg-Zeile einem Fehlertyp zu (None = ignorieren)."""
    if p.level in ("info", "verbose", "debug", "trace", "unknown"):
        return None
    for rule in FFMPEG_RULES:
        m = rule.pattern.search(p.msg)
        if not m:
            continue
        if rule.roles and p.ctx_role not in rule.roles:
            continue
        if rule.severity == S.INFO:
            return None
        c = Classified(
            error_type=rule.error_type,
            severity=rule.severity or S.ERROR,
            description=rule.description,
            heuristic=rule.heuristic,
            stream_index=p.stream_index,
            stream_kind=p.stream_kind,
            codec=p.codec,
            start_artifact=rule.start_artifact,
        )
        gd = m.groupdict()
        if rule.stream_group and gd.get(rule.stream_group) is not None:
            c.stream_index = int(gd[rule.stream_group])
        if rule.offset_group and gd.get(rule.offset_group):
            c.byte_offset = int(gd[rule.offset_group], 16 if rule.offset_hex else 10)
        if rule.dts_group and gd.get(rule.dts_group) not in (None, "NOPTS"):
            c.dts = int(gd[rule.dts_group])
        if gd.get("codec"):
            c.codec = gd["codec"]
        # Generische Decoderfehler nach Streamtyp spezialisieren
        if c.error_type == "decode_error":
            if p.stream_kind == "audio":
                c.error_type = "audio_decode_error"
            elif p.stream_kind == "video":
                c.error_type = "video_decode_error"
        if c.severity == S.ERROR and p.level == "warning" and rule.error_type in ("timestamp_invalid",):
            c.severity = S.WARNING
        return c

    # Keine Regel: nach Kontext und Stufe konservativ einordnen
    if p.ctx in BENIGN_FFMPEG_CONTEXTS:
        return None
    if p.level in ("error", "fatal", "panic"):
        if p.ctx_role in ("decoder", "stream"):
            return Classified("decode_error", S.ERROR, "Nicht näher klassifizierte Fehlermeldung des Decoders.", False,
                              p.stream_index, p.stream_kind, p.codec, rule_matched=False)
        if p.ctx_role == "demuxer":
            return Classified("demux_error", S.ERROR, "Nicht näher klassifizierte Fehlermeldung des Demuxers.", False,
                              p.stream_index, p.stream_kind, p.codec, rule_matched=False)
        return Classified("tool_error", S.WARNING, "Nicht klassifizierte Fehlermeldung von FFmpeg.", True,
                          rule_matched=False)
    if p.level == "warning":
        if p.ctx_role in ("decoder", "stream", "demuxer"):
            return Classified("tool_warning", S.WARNING, "Nicht eindeutige Warnung von FFmpeg.", True,
                              p.stream_index, p.stream_kind, p.codec, rule_matched=False)
        return None
    return None


# ---------------------------------------------------------------------------
# MKVToolNix (mkvmerge --gui-mode / mkvinfo)

MKV_GUI_LINE = re.compile(r"^#GUI#(?P<level>warning|error)\s+(?P<msg>.*)$")
MKV_PLAIN_LINE = re.compile(r"^(?P<level>Warning|Error):\s+(?P<msg>.*)$")
MKV_TS = r"(?P<h>\d+):(?P<m>\d\d):(?P<s>\d\d(?:\.\d+)?)"


def parse_mkv_timestamp(text: str) -> float | None:
    m = re.search(MKV_TS, text)
    if not m:
        return None
    return int(m.group("h")) * 3600 + int(m.group("m")) * 60 + float(m.group("s"))


@dataclass
class MkvMessage:
    level: str
    msg: str
    raw: str


def parse_mkv_line(raw: str) -> MkvMessage | None:
    line = raw.rstrip("\r\n")
    m = MKV_GUI_LINE.match(line) or MKV_PLAIN_LINE.match(line)
    if not m:
        return None
    return MkvMessage(level=m.group("level").lower(), msg=m.group("msg").strip(), raw=line)


MKV_IGNORE = [
    # Entsteht nur, weil die Ausgabe nach /dev/null geschrieben wird – kein Befund der Quelldatei.
    re.compile(r"Updating the 'document type version' or 'document type read version' header fields failed"),
    re.compile(r"No 'EBML head' element was found", re.I),
]

MKV_STRUCTURE = re.compile(r"Error in the Matroska file structure at position (?P<pos>\d+)", re.I)
MKV_LAST_TS = re.compile(r"last timestamp processed before the error was encountered was", re.I)
MKV_RESYNC_OK = re.compile(r"Resyncing successful at position (?P<pos>\d+)", re.I)
MKV_FIRST_TS = re.compile(r"first cluster timestamp after the resync is", re.I)
MKV_RESYNC_FAIL = re.compile(r"Resync failed|no valid Matroska level 1 element found", re.I)
MKV_UNRECOGNIZED = re.compile(r"could not be recognized|Have not found any header atoms|could not be opened for reading"
                              r"|is not a valid|unknown file format", re.I)
MKV_TIMESTAMP_WARN = re.compile(r"timestamp|timecode", re.I)
MKV_UNSUPPORTED = re.compile(r"not supported|unsupported|is unknown", re.I)
MKV_PERMISSION = re.compile(r"Permission denied|could not be opened for reading", re.I)
MKV_TRUNCATED = re.compile(r"premature end|end of file|truncated|incomplete", re.I)
