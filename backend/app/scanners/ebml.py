"""Streamender Matroska/EBML-Strukturprüfer.

Der Prüfer liest die Datei sequenziell **vollständig** (jedes Byte wird
gelesen, auch Blockinhalte, damit nicht lesbare Bereiche auffallen) und
validiert dabei:

* EBML-Header und DocType
* Segmentgröße gegenüber der Dateigröße (abgeschnittene Dateien)
* Level-1-Elemente, unbekannte/zerstörte Bereiche inkl. Resynchronisation
* Clusteraufbau, Kindelemente, Elementgrenzen
* Block-/SimpleBlock-Header, Spurnummern, Lacing
* CRC-32-Elemente (falls vom Muxer geschrieben)
* Clusterzeitstempel (rückwärts, Sprünge) und Spurlücken
* Laufzeit laut Segment-Info gegenüber den tatsächlichen Blockzeitstempeln
* Cues (Index) und SeekHead gegen die tatsächlich gefundenen Positionen

Er kann als Unterprozess (``python -m app.scanners.ebml DATEI``) gestartet
werden und gibt dann JSON-Zeilen auf stdout aus. So läuft er mit niedriger
CPU-/E/A-Priorität, kann per SIGSTOP pausiert werden und blockiert den
Webserver nicht.

Grenzen: Der Prüfer validiert die Containerstruktur, nicht den Inhalt der
Videobilder bzw. Audiodaten. Zerstörte Nutzdaten innerhalb eines formal
korrekten Blocks erkennt nur die Decodierprüfung.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .base import Finding, Severity, TimePrecision

TOOL = "ebml-validator"

# --- Element-IDs -----------------------------------------------------------
EBML = 0x1A45DFA3
EBML_VERSION = 0x4286
EBML_READ_VERSION = 0x42F7
EBML_MAX_ID_LENGTH = 0x42F2
EBML_MAX_SIZE_LENGTH = 0x42F3
DOCTYPE = 0x4282
DOCTYPE_VERSION = 0x4287
DOCTYPE_READ_VERSION = 0x4285
VOID = 0xEC
CRC32 = 0xBF

SEGMENT = 0x18538067
SEEKHEAD = 0x114D9B74
SEEK = 0x4DBB
SEEK_ID = 0x53AB
SEEK_POSITION = 0x53AC
INFO = 0x1549A966
TIMESTAMP_SCALE = 0x2AD7B1
DURATION = 0x4489
TRACKS = 0x1654AE6B
TRACK_ENTRY = 0xAE
TRACK_NUMBER = 0xD7
TRACK_TYPE = 0x83
CODEC_ID = 0x86
DEFAULT_DURATION = 0x23E383
CLUSTER = 0x1F43B675
CLUSTER_TIMESTAMP = 0xE7
SIMPLE_BLOCK = 0xA3
BLOCK_GROUP = 0xA0
BLOCK = 0xA1
BLOCK_DURATION = 0x9B
CUES = 0x1C53BB6B
CUE_POINT = 0xBB
CUE_TIME = 0xB3
CUE_TRACK_POSITIONS = 0xB7
CUE_TRACK = 0xF7
CUE_CLUSTER_POSITION = 0xF1
CHAPTERS = 0x1043A770
TAGS = 0x1254C367
ATTACHMENTS = 0x1941A469

LEVEL1_IDS = {SEEKHEAD, INFO, TRACKS, CLUSTER, CUES, CHAPTERS, TAGS, ATTACHMENTS}
LEVEL1_NAMES = {
    SEEKHEAD: "SeekHead",
    INFO: "Info",
    TRACKS: "Tracks",
    CLUSTER: "Cluster",
    CUES: "Cues",
    CHAPTERS: "Chapters",
    TAGS: "Tags",
    ATTACHMENTS: "Attachments",
    VOID: "Void",
    CRC32: "CRC-32",
}

# Erlaubte Kindelemente eines Clusters bzw. einer BlockGroup (Matroska-Spezifikation).
CLUSTER_CHILDREN = {
    CLUSTER_TIMESTAMP,
    SIMPLE_BLOCK,
    BLOCK_GROUP,
    0x5854,  # SilentTracks
    0xA7,  # Position
    0xAB,  # PrevSize
    0xAF,  # EncryptedBlock
    VOID,
    CRC32,
}
BLOCK_GROUP_CHILDREN = {
    BLOCK,
    0xA2,  # BlockVirtual
    0x75A1,  # BlockAdditions
    BLOCK_DURATION,
    0xFA,  # ReferencePriority
    0xFB,  # ReferenceBlock
    0xFD,  # ReferenceVirtual
    0xA4,  # CodecState
    0x75A2,  # DiscardPadding
    0x8E,  # Slices
    0xC8,  # ReferenceFrame (DivX)
    VOID,
    CRC32,
}

# Masterelemente der "kleinen" Level-1-Elemente (für die rekursive Grenzprüfung).
MASTER_IDS = {
    SEEKHEAD, SEEK, INFO, TRACKS, TRACK_ENTRY, CUES, CUE_POINT, CUE_TRACK_POSITIONS,
    CHAPTERS, TAGS, ATTACHMENTS,
    0x6924,  # ChapterTranslate
    0xE0, 0xE1, 0xE2, 0xE3, 0xE4, 0xE9,  # Video, Audio, TrackOperation...
    0x6D80, 0x6240, 0x5034, 0x5035, 0x47E7,  # ContentEncodings...
    0x55B0, 0x55D0, 0x7670, 0x41E4, 0x6624,  # Colour, Mastering, Projection...
    0xDB,  # CueReference
    0x7373, 0x63C0, 0x67C8,  # Tag, Targets, SimpleTag
    0x45B9, 0xB6, 0x8F, 0x80, 0x6944, 0x6911, 0x4520,  # Chapters
    0x61A7,  # AttachedFile
}

TRACK_TYPES = {1: "video", 2: "audio", 3: "complex", 0x10: "logo", 0x11: "subtitle", 0x12: "buttons", 0x20: "control", 0x21: "metadata"}

UNKNOWN_SIZE = -1
SMALL_ELEMENT_LIMIT = 64 * 1024 * 1024
CHUNK = 1024 * 1024


class EBMLError(Exception):
    def __init__(self, message: str, offset: int):
        super().__init__(message)
        self.offset = offset


class ReadError(Exception):
    def __init__(self, offset: int, errno_: int | None, message: str):
        super().__init__(message)
        self.offset = offset
        self.errno = errno_


class EndOfFile(Exception):
    pass


# --- Leser -------------------------------------------------------------------

class Reader:
    """Sequenzieller Leser mit kleinem Puffer.

    ``skip`` liest die übersprungenen Bytes tatsächlich (statt zu seeken), damit
    jeder Dateibereich auf Lesbarkeit geprüft wird. Gelesene Seiten werden per
    ``posix_fadvise(DONTNEED)`` wieder freigegeben, um den Seitencache des NAS
    nicht mit Filmdaten zu füllen.
    """

    def __init__(self, fd: int, size: int, pause_check: Callable[[], None] | None = None):
        self.fd = fd
        self.size = size
        self._buf = b""
        self._buf_start = 0
        self._idx = 0
        self._crc: int | None = None
        self.bytes_read = 0
        self._last_advise = 0
        self._pause_check = pause_check
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
        except (AttributeError, OSError):
            pass

    @property
    def pos(self) -> int:
        return self._buf_start + self._idx

    def _fill(self, need: int) -> None:
        """Sorgt dafür, dass mindestens ``need`` Bytes ab pos im Puffer liegen (sofern vorhanden)."""
        avail = len(self._buf) - self._idx
        if avail >= need:
            return
        rest = self._buf[self._idx:]
        start = self.pos
        read_at = start + len(rest)
        want = max(need - len(rest), CHUNK)
        chunks = [rest]
        got_total = len(rest)
        while got_total < need:
            if read_at >= self.size:
                break
            try:
                data = os.pread(self.fd, min(want, self.size - read_at), read_at)
            except OSError as exc:
                raise ReadError(read_at, exc.errno, f"Lesefehler bei Byte {read_at}: {exc.strerror}") from exc
            if not data:
                break
            chunks.append(data)
            got_total += len(data)
            read_at += len(data)
            self.bytes_read += len(data)
        self._buf = b"".join(chunks)
        self._buf_start = start
        self._idx = 0
        if read_at - self._last_advise > 64 * CHUNK:
            try:
                os.posix_fadvise(self.fd, self._last_advise, start - self._last_advise, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError):
                pass
            self._last_advise = start
        if self._pause_check:
            self._pause_check()

    def peek(self, n: int) -> bytes:
        self._fill(n)
        return self._buf[self._idx:self._idx + n]

    def read(self, n: int) -> bytes:
        self._fill(n)
        data = self._buf[self._idx:self._idx + n]
        self._idx += len(data)
        if self._crc is not None:
            self._crc = zlib.crc32(data, self._crc)
        return data

    def skip(self, n: int) -> int:
        """Liest n Bytes durch. Gibt die tatsächlich gelesene Anzahl zurück."""
        done = 0
        while done < n:
            step = min(n - done, 4 * CHUNK)
            data = self.read(step)
            if not data:
                break
            done += len(data)
            if len(data) < step:
                break
        return done

    def seek(self, pos: int) -> None:
        if self._buf_start <= pos <= self._buf_start + len(self._buf):
            self._idx = pos - self._buf_start
        else:
            self._buf = b""
            self._buf_start = pos
            self._idx = 0

    def crc_begin(self) -> None:
        self._crc = 0

    def crc_end(self) -> int | None:
        value, self._crc = self._crc, None
        return value

    def crc_abort(self) -> None:
        self._crc = None


# --- EBML-Grundfunktionen -----------------------------------------------------

def _vint_length(first: int) -> int:
    if first == 0:
        return 0
    length = 1
    mask = 0x80
    while not first & mask:
        mask >>= 1
        length += 1
    return length


def parse_id(data: bytes, offset: int) -> tuple[int, int]:
    if not data:
        raise EndOfFile()
    length = _vint_length(data[0])
    if length == 0 or length > 4:
        raise EBMLError(f"Ungültiges erstes Byte einer Element-ID (0x{data[0]:02x})", offset)
    if len(data) < length:
        raise EndOfFile()
    value = int.from_bytes(data[:length], "big")
    return value, length


def parse_size(data: bytes, offset: int) -> tuple[int, int]:
    if not data:
        raise EndOfFile()
    length = _vint_length(data[0])
    if length == 0 or length > 8:
        raise EBMLError(f"Ungültige Längenangabe (erstes Byte 0x{data[0]:02x})", offset)
    if len(data) < length:
        raise EndOfFile()
    value = int.from_bytes(data[:length], "big") & ((1 << (7 * length)) - 1)
    if value == (1 << (7 * length)) - 1:
        return UNKNOWN_SIZE, length
    return value, length


def read_header(reader: Reader) -> tuple[int, int, int, int]:
    """Liest einen Elementkopf. Rückgabe: (id, size, header_pos, data_pos)."""
    pos = reader.pos
    head = reader.peek(12)
    if not head:
        raise EndOfFile()
    eid, id_len = parse_id(head, pos)
    size, size_len = parse_size(head[id_len:], pos + id_len)
    reader.read(id_len + size_len)
    return eid, size, pos, pos + id_len + size_len


def decode_uint(data: bytes) -> int:
    return int.from_bytes(data, "big") if data else 0


def decode_float(data: bytes) -> float | None:
    if len(data) == 4:
        return struct.unpack(">f", data)[0]
    if len(data) == 8:
        return struct.unpack(">d", data)[0]
    if len(data) == 0:
        return 0.0
    return None


def iter_children(buf: bytes, start: int, end: int, base_offset: int) -> Iterable[tuple[int, int, int, int]]:
    """Kindelemente innerhalb eines Speicherpuffers. Rückgabe (id, data_start, data_end, header_pos)."""
    pos = start
    while pos < end:
        if end - pos < 2:
            raise EBMLError("Restbytes reichen nicht für einen Elementkopf", base_offset + pos)
        eid, id_len = parse_id(buf[pos:pos + 4], base_offset + pos)
        size, size_len = parse_size(buf[pos + id_len:pos + id_len + 8], base_offset + pos + id_len)
        data_start = pos + id_len + size_len
        if size == UNKNOWN_SIZE:
            raise EBMLError(f"Element 0x{eid:X} mit unbekannter Größe innerhalb eines Masterelements", base_offset + pos)
        data_end = data_start + size
        if data_end > end:
            raise EBMLError(
                f"Element 0x{eid:X} bei Byte {base_offset + pos} endet bei {base_offset + data_end}, "
                f"das übergeordnete Element endet bereits bei {base_offset + end}",
                base_offset + pos,
            )
        yield eid, data_start, data_end, pos
        pos = data_end


# --- Ergebnisstrukturen ----------------------------------------------------------

@dataclass
class TrackInfo:
    number: int
    type: str
    codec_id: str | None = None
    default_duration_ns: int | None = None
    blocks: int = 0
    first_ts: float | None = None
    last_ts: float | None = None
    max_ts: float | None = None
    prev_ts: float | None = None


@dataclass
class Options:
    ts_jump_warning: float = 3.0
    ts_jump_error: float = 30.0
    track_gap_warning: float = 5.0
    duration_tolerance_s: float = 5.0
    duration_tolerance_pct: float = 2.0
    max_findings: int = 400
    cluster_back_tolerance: float = 1.0


@dataclass
class ValidationState:
    file_size: int
    findings: list[Finding] = field(default_factory=list)
    aggregated: dict[tuple, Finding] = field(default_factory=dict)
    timestamp_scale: int = 1_000_000
    info_duration: float | None = None
    tracks: dict[int, TrackInfo] = field(default_factory=dict)
    segment_data_start: int | None = None
    segment_end: int | None = None
    clusters: list[tuple[int, float]] = field(default_factory=list)
    level1: dict[int, int] = field(default_factory=dict)  # position -> id
    seek_entries: list[tuple[int, int]] = field(default_factory=list)
    cue_positions: list[tuple[float, int]] = field(default_factory=list)
    has_cues: bool = False
    crc_checked: int = 0
    crc_failed: int = 0
    resync_bytes: int = 0
    last_block_ts: float | None = None
    max_block_ts: float | None = None
    max_block_end: float | None = None
    prev_cluster_ts: float | None = None
    # Großer Rücksprung, der erst beim nächsten Cluster bewertet wird:
    # {"prev_ts": float, "clusters": [(hpos, ts, set_der_spurtypen)]}
    pending_back: dict | None = None
    cur_cluster_types: set = field(default_factory=set)
    doc_type: str | None = None
    suppressed: int = 0
    tracks_seen: bool = False


class MatroskaValidator:
    def __init__(self, path: str, options: Options | None = None,
                 emit: Callable[[dict], None] | None = None,
                 pause_check: Callable[[], None] | None = None,
                 progress_interval: float = 0.5):
        self.path = path
        self.opt = options or Options()
        self.emit = emit
        self.pause_check = pause_check
        self.progress_interval = progress_interval
        self._last_progress = 0.0

    # -- Hilfsfunktionen für Feststellungen --
    def _ts(self, raw: int) -> float:
        return raw * self.state.timestamp_scale / 1e9

    def add(self, finding: Finding, aggregate_key: tuple | None = None) -> None:
        st = self.state
        if aggregate_key is not None:
            existing = st.aggregated.get(aggregate_key)
            if existing is not None:
                existing.occurrences += 1
                if finding.time_start is not None:
                    end = finding.time_end if finding.time_end is not None else finding.time_start
                    if existing.time_start is None:
                        existing.time_start = finding.time_start
                        existing.time_precision = finding.time_precision
                    existing.time_end = max(existing.time_end or existing.time_start, end)
                    if existing.time_precision == TimePrecision.EXACT and existing.time_end != existing.time_start:
                        existing.time_precision = TimePrecision.RANGE
                return
            st.aggregated[aggregate_key] = finding
        if len(st.findings) >= self.opt.max_findings:
            st.suppressed += 1
            return
        st.findings.append(finding)

    def _f(self, error_type: str, severity: Severity, description: str, **kw) -> Finding:
        return Finding(error_type=error_type, severity=severity, description=description, tool=TOOL, **kw)

    def _cluster_time_after(self, offset: int) -> float | None:
        for pos, ts in self.state.clusters:
            if pos > offset:
                return ts
        return None

    def _progress(self, reader: Reader, force: bool = False) -> None:
        now = time.monotonic()
        if self.emit and (force or now - self._last_progress >= self.progress_interval):
            self._last_progress = now
            self.emit({"t": "progress", "pos": reader.pos, "size": self.state.file_size})

    # -- Einstieg --
    def run(self) -> dict:
        started = time.monotonic()
        flags = os.O_RDONLY | getattr(os, "O_NOATIME", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.path, flags)
        except PermissionError:
            if flags & getattr(os, "O_NOATIME", 0):
                fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            else:
                raise
        try:
            size = os.fstat(fd).st_size
            self.state = ValidationState(file_size=size)
            reader = Reader(fd, size, self.pause_check)
            completed = True
            try:
                self._parse_file(reader)
            except ReadError as exc:
                completed = self._handle_read_error(reader, exc)
            self._final_checks()
            self._progress(reader, force=True)
            st = self.state
            return {
                "completed": completed,
                "file_size": size,
                "bytes_read": reader.bytes_read,
                "doc_type": st.doc_type,
                "timestamp_scale": st.timestamp_scale,
                "info_duration": st.info_duration,
                "last_timestamp": st.max_block_end,
                "clusters": len(st.clusters),
                "cluster_index": [[p, round(t, 6)] for p, t in st.clusters],
                "tracks": {
                    n: {"type": t.type, "codec_id": t.codec_id, "blocks": t.blocks,
                        "first_ts": t.first_ts, "last_ts": t.max_ts}
                    for n, t in st.tracks.items()
                },
                "crc_checked": st.crc_checked,
                "crc_failed": st.crc_failed,
                "resync_bytes": st.resync_bytes,
                "has_cues": st.has_cues,
                "cue_points": len(st.cue_positions),
                "suppressed_findings": st.suppressed,
                "elapsed": round(time.monotonic() - started, 3),
                "findings": [f.to_dict() for f in st.findings],
            }
        finally:
            os.close(fd)

    def _handle_read_error(self, reader: Reader, exc: ReadError) -> bool:
        """Ein E/A-Fehler ist ein echter Befund (nicht lesbarer Bereich)."""
        # Rest der Datei blockweise weiterlesen, um den Umfang zu bestimmen.
        bad_start = exc.offset
        pos = (bad_start // CHUNK + 1) * CHUNK
        bad_end = pos
        readable_after = None
        while pos < self.state.file_size:
            try:
                os.pread(reader.fd, 4096, pos)
                readable_after = pos
                break
            except OSError:
                pos += CHUNK
                bad_end = pos
        ts = self.state.max_block_ts
        self.add(self._f(
            "read_error", Severity.CRITICAL,
            f"Dateibereich ab Byte {bad_start} ist nicht lesbar ({exc}). "
            + ("Danach ist die Datei wieder lesbar." if readable_after else "Bis zum Dateiende nicht lesbar."),
            byte_offset=bad_start, byte_end=min(bad_end, self.state.file_size),
            time_start=ts, time_end=self._cluster_time_after(bad_start) if ts is not None else None,
            time_precision=TimePrecision.RANGE, time_source="Letzter lesbarer Blockzeitstempel",
            raw_message=str(exc),
        ))
        return True

    # -- Dateiebene --
    def _parse_file(self, reader: Reader) -> None:
        st = self.state
        if st.file_size == 0:
            self.add(self._f("file_unreadable", Severity.CRITICAL, "Die Datei ist leer (0 Byte)."))
            return
        try:
            eid, size, hpos, dpos = read_header(reader)
        except (EBMLError, EndOfFile) as exc:
            self.add(self._f("ebml_header", Severity.CRITICAL,
                             "Kein gültiger EBML-Header am Dateianfang – die Datei ist keine lesbare Matroska-Datei.",
                             byte_offset=0, raw_message=str(exc)))
            return
        if eid != EBML:
            self.add(self._f("ebml_header", Severity.CRITICAL,
                             f"Die Datei beginnt nicht mit einem EBML-Header (gefunden: Element-ID 0x{eid:X}).",
                             byte_offset=0))
            return
        if size == UNKNOWN_SIZE or size > 4096:
            self.add(self._f("ebml_header", Severity.CRITICAL, "EBML-Header hat eine unplausible Größe.", byte_offset=0))
            return
        self._parse_ebml_header(reader.read(size), dpos)
        if st.doc_type not in ("matroska", "webm"):
            self.add(self._f("ebml_header", Severity.CRITICAL,
                             f"Unbekannter DocType '{st.doc_type}' – keine Matroska-Datei.", byte_offset=0))
            return

        # Segment suchen (Void-Elemente davor sind erlaubt)
        while True:
            try:
                eid, size, hpos, dpos = read_header(reader)
            except EndOfFile:
                self.add(self._f("element_missing", Severity.CRITICAL, "Kein Segment-Element gefunden – keine Mediendaten.",
                                 byte_offset=reader.pos))
                return
            except EBMLError as exc:
                self.add(self._f("ebml_structure", Severity.CRITICAL,
                                 f"Ungültige Struktur nach dem EBML-Header: {exc}", byte_offset=exc.offset))
                return
            if eid == SEGMENT:
                break
            if eid in (VOID, CRC32) and size != UNKNOWN_SIZE:
                reader.skip(size)
                continue
            self.add(self._f("ebml_structure", Severity.CRITICAL,
                             f"Unerwartetes Element 0x{eid:X} statt Segment.", byte_offset=hpos))
            return

        st.segment_data_start = dpos
        self._segment_unknown_size = size == UNKNOWN_SIZE
        if size == UNKNOWN_SIZE:
            st.segment_end = st.file_size
        else:
            st.segment_end = dpos + size
            if st.segment_end > st.file_size:
                missing = st.segment_end - st.file_size
                self._truncation_pending = missing
        self._parse_segment(reader)

        if st.segment_end is not None and st.segment_end < st.file_size:
            trailing = st.file_size - st.segment_end
            # Nach dem Segment dürfen nur weitere Segmente oder Void folgen
            self.add(self._f("trailing_data", Severity.WARNING,
                             f"{trailing} Byte nach dem Ende des Segments (z. B. Rest eines abgebrochenen Kopiervorgangs).",
                             byte_offset=st.segment_end, byte_end=st.file_size, heuristic=True))
            reader.seek(st.segment_end)
            reader.skip(trailing)

    def _parse_ebml_header(self, data: bytes, base: int) -> None:
        try:
            for eid, s, e, _ in iter_children(data, 0, len(data), base):
                value = data[s:e]
                if eid == DOCTYPE:
                    self.state.doc_type = value.rstrip(b"\x00").decode("ascii", "replace")
                elif eid == EBML_READ_VERSION and decode_uint(value) > 1:
                    self.add(self._f("ebml_header", Severity.WARNING,
                                     f"EBMLReadVersion {decode_uint(value)} wird von gängigen Playern nicht unterstützt."))
                elif eid == DOCTYPE_READ_VERSION and decode_uint(value) > 4:
                    self.add(self._f("metadata_inconsistent", Severity.WARNING,
                                     f"DocTypeReadVersion {decode_uint(value)} ist unbekannt (>4)."))
                elif eid == EBML_MAX_ID_LENGTH and decode_uint(value) > 4:
                    self.add(self._f("ebml_header", Severity.WARNING, "EBMLMaxIDLength > 4 ist ungültig für Matroska."))
                elif eid == EBML_MAX_SIZE_LENGTH and decode_uint(value) > 8:
                    self.add(self._f("ebml_header", Severity.WARNING, "EBMLMaxSizeLength > 8 ist ungültig für Matroska."))
        except EBMLError as exc:
            self.add(self._f("ebml_header", Severity.ERROR, f"EBML-Header beschädigt: {exc}", byte_offset=exc.offset))

    # -- Segment --
    _truncation_pending: int | None = None
    _segment_unknown_size: bool = False

    def _parse_segment(self, reader: Reader) -> None:
        st = self.state
        end = st.segment_end or st.file_size
        hard_end = min(end, st.file_size)
        while reader.pos < hard_end:
            self._progress(reader)
            pos = reader.pos
            try:
                eid, size, hpos, dpos = read_header(reader)
            except EndOfFile:
                break
            except EBMLError as exc:
                self._structure_error_and_resync(reader, pos, f"Ungültiger Elementkopf auf Segmentebene: {exc}")
                continue

            if eid not in LEVEL1_IDS and eid not in (VOID, CRC32):
                self._structure_error_and_resync(
                    reader, pos, f"Unbekanntes oder zerstörtes Element 0x{eid:X} auf Segmentebene bei Byte {pos}.")
                continue

            st.level1[hpos] = eid
            if size == UNKNOWN_SIZE:
                if eid == CLUSTER:
                    self._parse_cluster(reader, hpos, dpos, None)
                    continue
                self._structure_error_and_resync(reader, pos, f"{LEVEL1_NAMES.get(eid)} mit unbekannter Größe.")
                continue

            elem_end = dpos + size
            if eid == CLUSTER and elem_end > st.file_size and self._segment_unknown_size:
                # Segment ohne Größenangabe: ein über das Dateiende ragender Cluster bedeutet Abschneiden
                self._truncation_pending = elem_end - st.file_size
                self._parse_cluster(reader, hpos, dpos, elem_end)
                continue
            if elem_end > end:
                # Element ragt über das Segmentende hinaus
                self._structure_error_and_resync(
                    reader, pos,
                    f"{LEVEL1_NAMES.get(eid, hex(eid))} bei Byte {pos} ist größer als das umgebende Segment.")
                continue

            if eid == CLUSTER:
                self._parse_cluster(reader, hpos, dpos, elem_end)
            elif eid in (VOID, CRC32):
                reader.skip(size)
            elif size <= SMALL_ELEMENT_LIMIT and eid in (INFO, TRACKS, SEEKHEAD, CUES, CHAPTERS, TAGS):
                data = reader.read(size)
                if len(data) < size:
                    self._truncated(hpos, f"{LEVEL1_NAMES[eid]} wird durch das Dateiende abgeschnitten.")
                    return
                self._parse_small_level1(eid, data, dpos)
            else:
                got = reader.skip(size)
                if got < size:
                    self._truncated(hpos, f"{LEVEL1_NAMES.get(eid)} wird durch das Dateiende abgeschnitten.")
                    return

        if self._truncation_pending:
            self._truncated(st.file_size, None)
        if st.pending_back is not None:
            self._resolve_backwards(resumed=True)  # Dateiende: Untertitel-Ausreißer am Schluss

    def _truncated(self, offset: int, detail: str | None) -> None:
        st = self.state
        if any(f.error_type == "truncated" for f in st.findings):
            return
        missing = (st.segment_end or 0) - st.file_size
        desc = "Die Datei ist kürzer als im Segment angegeben – sie wurde vermutlich unvollständig kopiert oder heruntergeladen."
        if missing > 0:
            desc += f" Es fehlen {missing} Byte ({missing / max(st.segment_end or 1, 1) * 100:.1f} %)."
        if detail:
            desc += " " + detail
        ts = st.max_block_end
        kw: dict = {}
        if ts is not None:
            kw = dict(time_start=ts, time_end=st.info_duration if st.info_duration and st.info_duration > ts else None,
                      time_precision=TimePrecision.EXACT if not st.info_duration else TimePrecision.RANGE,
                      time_source="Letzter vollständiger Blockzeitstempel bis Laufzeit laut Segment-Info")
        self.add(self._f("truncated", Severity.CRITICAL, desc, byte_offset=offset, byte_end=st.segment_end, **kw))
        self._truncation_pending = None

    def _structure_error_and_resync(self, reader: Reader, err_pos: int, message: str,
                                    error_type: str = "matroska_structure", ts_hint: float | None = None) -> None:
        st = self.state
        reader.crc_abort()
        resync_at = self._resync(reader, err_pos + 1)
        last_ts = ts_hint if ts_hint is not None else st.max_block_ts
        if resync_at is None:
            truncated_cluster = st.segment_end and st.segment_end > st.file_size
            self.add(self._f(
                error_type, Severity.CRITICAL,
                message + " Keine Resynchronisation möglich – der Rest der Datei ist strukturell unbrauchbar."
                + (" (Datei zusätzlich abgeschnitten.)" if truncated_cluster else ""),
                byte_offset=err_pos, byte_end=st.file_size,
                time_start=last_ts, time_precision=TimePrecision.EXACT if last_ts is not None else TimePrecision.UNKNOWN,
                time_source="Letzter gültiger Blockzeitstempel vor dem Fehler",
            ))
            reader.seek(st.file_size)
            return
        lost = resync_at - err_pos
        st.resync_bytes += lost
        next_ts = None
        # Den Zeitstempel des nächsten Clusters vorab lesen (ohne Puffer zu verlieren)
        head = reader.peek(32)
        if head[:4] == b"\x1f\x43\xb6\x75":
            next_ts = self._peek_cluster_ts(head)
        desc = message + f" {lost} Byte bis zum nächsten gültigen Element (Byte {resync_at}) übersprungen."
        precision = TimePrecision.UNKNOWN
        if last_ts is not None and next_ts is not None:
            precision = TimePrecision.RANGE
        elif last_ts is not None:
            precision = TimePrecision.EXACT
        self.add(self._f(
            error_type, Severity.ERROR, desc,
            byte_offset=err_pos, byte_end=resync_at,
            time_start=last_ts, time_end=next_ts, time_precision=precision,
            time_source="Letzter gültiger Blockzeitstempel bis Zeitstempel des nächsten Clusters",
        ))

    def _peek_cluster_ts(self, head: bytes) -> float | None:
        try:
            _, id_len = parse_id(head, 0)
            _, size_len = parse_size(head[id_len:], 0)
            p = id_len + size_len
            for _ in range(3):
                cid, cl = parse_id(head[p:], 0)
                csize, csl = parse_size(head[p + cl:], 0)
                if cid == CLUSTER_TIMESTAMP and 0 < csize <= 8:
                    return self._ts(decode_uint(head[p + cl + csl:p + cl + csl + csize]))
                if cid in (CRC32, VOID) and csize >= 0:
                    p += cl + csl + csize
                    continue
                break
        except (EBMLError, EndOfFile, IndexError):
            return None
        return None

    def _resync(self, reader: Reader, start: int) -> int | None:
        """Sucht ab ``start`` das nächste plausible Level-1-Element. Liest dabei sequenziell weiter."""
        st = self.state
        end = min(st.segment_end or st.file_size, st.file_size)
        patterns = [eid.to_bytes(4, "big") for eid in LEVEL1_IDS]
        reader.seek(start)
        window = 8 * CHUNK
        pos = start
        while pos < end:
            data = reader.peek(min(window + 64, end - pos + 64))
            if not data:
                return None
            best = None
            for pat in patterns:
                i = data.find(pat)
                while i != -1:
                    if self._plausible_level1(data, i, pos + i, end):
                        if best is None or i < best:
                            best = i
                        break
                    i = data.find(pat, i + 1)
            if best is not None:
                reader.seek(pos)
                reader.skip(best)
                return pos + best
            step = max(len(data) - 64, 1)
            reader.skip(step)
            pos += step
            self._progress(reader)
            if len(data) < window:
                break
        return None

    def _plausible_level1(self, data: bytes, i: int, abs_pos: int, end: int) -> bool:
        try:
            eid, id_len = parse_id(data[i:i + 4], abs_pos)
            size, size_len = parse_size(data[i + id_len:i + id_len + 8], abs_pos)
        except (EBMLError, EndOfFile):
            return False
        if size != UNKNOWN_SIZE and abs_pos + id_len + size_len + size > max(end, self.state.file_size) + 1:
            # Größer als die Datei: nur akzeptieren, wenn die Datei abgeschnitten ist und es ein Cluster ist
            if not (eid == CLUSTER and self.state.segment_end and self.state.segment_end > self.state.file_size):
                return False
        if eid == CLUSTER:
            ts = self._peek_cluster_ts(data[i:i + 32])
            return ts is not None
        return True

    # -- Kleine Level-1-Elemente --
    def _parse_small_level1(self, eid: int, data: bytes, base: int) -> None:
        name = LEVEL1_NAMES[eid]
        try:
            self._validate_tree(data, 0, len(data), base)
        except EBMLError as exc:
            sev = Severity.ERROR if eid in (TRACKS, INFO) else Severity.WARNING
            self.add(self._f("ebml_structure", sev, f"Element {name} ist beschädigt: {exc}", byte_offset=exc.offset,
                             heuristic=sev == Severity.WARNING))
            if eid not in (TRACKS, INFO, CUES):
                return
        try:
            if eid == INFO:
                self._parse_info(data, base)
            elif eid == TRACKS:
                self._parse_tracks(data, base)
            elif eid == SEEKHEAD:
                self._parse_seekhead(data, base)
            elif eid == CUES:
                self._parse_cues(data, base)
        except EBMLError:
            pass  # bereits gemeldet

    def _validate_tree(self, buf: bytes, start: int, end: int, base: int, depth: int = 0) -> None:
        if depth > 16:
            raise EBMLError("Verschachtelungstiefe zu groß", base + start)
        for eid, s, e, _ in iter_children(buf, start, end, base):
            if eid in MASTER_IDS:
                self._validate_tree(buf, s, e, base, depth + 1)

    def _parse_info(self, data: bytes, base: int) -> None:
        for eid, s, e, _ in iter_children(data, 0, len(data), base):
            if eid == TIMESTAMP_SCALE:
                scale = decode_uint(data[s:e])
                if scale <= 0:
                    self.add(self._f("metadata_inconsistent", Severity.ERROR, "TimestampScale ist 0 oder ungültig."))
                else:
                    self.state.timestamp_scale = scale
            elif eid == DURATION:
                dur = decode_float(data[s:e])
                if dur is None or dur != dur or dur < 0:
                    self.add(self._f("metadata_inconsistent", Severity.WARNING, "Ungültige Laufzeitangabe in Segment-Info."))
                else:
                    self._raw_duration = dur
        raw = getattr(self, "_raw_duration", None)
        if raw is not None:
            self.state.info_duration = raw * self.state.timestamp_scale / 1e9

    def _parse_tracks(self, data: bytes, base: int) -> None:
        self.state.tracks_seen = True
        for eid, s, e, _ in iter_children(data, 0, len(data), base):
            if eid != TRACK_ENTRY:
                continue
            number, ttype, codec, default_dur = None, None, None, None
            for cid, cs, ce, _ in iter_children(data, s, e, base):
                v = data[cs:ce]
                if cid == TRACK_NUMBER:
                    number = decode_uint(v)
                elif cid == TRACK_TYPE:
                    ttype = TRACK_TYPES.get(decode_uint(v), "unknown")
                elif cid == CODEC_ID:
                    codec = v.rstrip(b"\x00").decode("ascii", "replace")
                elif cid == DEFAULT_DURATION:
                    default_dur = decode_uint(v)
            if not number:
                self.add(self._f("element_missing", Severity.ERROR, "TrackEntry ohne gültige TrackNumber.",
                                 byte_offset=base + s))
                continue
            if number in self.state.tracks:
                self.add(self._f("metadata_inconsistent", Severity.ERROR, f"TrackNumber {number} ist doppelt vergeben."))
            if not codec:
                self.add(self._f("element_missing", Severity.ERROR, f"Spur {number} hat keine CodecID.",
                                 stream_index=number - 1))
            self.state.tracks[number] = TrackInfo(number=number, type=ttype or "unknown", codec_id=codec,
                                                  default_duration_ns=default_dur)

    def _parse_seekhead(self, data: bytes, base: int) -> None:
        for eid, s, e, _ in iter_children(data, 0, len(data), base):
            if eid != SEEK:
                continue
            sid, spos = None, None
            for cid, cs, ce, _ in iter_children(data, s, e, base):
                if cid == SEEK_ID:
                    sid = decode_uint(data[cs:ce])
                elif cid == SEEK_POSITION:
                    spos = decode_uint(data[cs:ce])
            if sid is not None and spos is not None:
                self.state.seek_entries.append((sid, spos))

    def _parse_cues(self, data: bytes, base: int) -> None:
        st = self.state
        st.has_cues = True
        for eid, s, e, _ in iter_children(data, 0, len(data), base):
            if eid != CUE_POINT:
                continue
            cue_time = None
            for cid, cs, ce, _ in iter_children(data, s, e, base):
                if cid == CUE_TIME:
                    cue_time = self._ts(decode_uint(data[cs:ce]))
                elif cid == CUE_TRACK_POSITIONS:
                    for tid, ts_, te, _ in iter_children(data, cs, ce, base):
                        if tid == CUE_CLUSTER_POSITION and cue_time is not None:
                            st.cue_positions.append((cue_time, decode_uint(data[ts_:te])))

    # -- Cluster --
    def _parse_cluster(self, reader: Reader, hpos: int, dpos: int, end: int | None) -> None:
        st = self.state
        cluster_ts_raw: int | None = None
        cluster_ts: float | None = None
        cluster_max_ts: float | None = None
        first_child = True
        crc_expected: int | None = None
        unknown_size = end is None
        file_end = st.file_size
        limit = end if end is not None else (st.segment_end or file_end)

        while reader.pos < limit:
            self._progress(reader)
            pos = reader.pos
            if pos >= file_end:
                break
            try:
                head = reader.peek(12)
                if not head:
                    break
                eid, id_len = parse_id(head, pos)
                if unknown_size and eid in LEVEL1_IDS:
                    break  # Ende eines Clusters unbekannter Größe
                size, size_len = parse_size(head[id_len:], pos + id_len)
            except EndOfFile:
                break
            except EBMLError as exc:
                self._cluster_damaged(reader, hpos, end, cluster_ts, cluster_max_ts,
                                      f"Ungültiger Elementkopf im Cluster bei Byte {exc.offset}: {exc}", exc.offset)
                return

            if eid not in CLUSTER_CHILDREN:
                self._cluster_damaged(reader, hpos, end, cluster_ts, cluster_max_ts,
                                      f"Unbekanntes oder zerstörtes Element 0x{eid:X} im Cluster bei Byte {pos}.", pos)
                return
            if size == UNKNOWN_SIZE:
                self._cluster_damaged(reader, hpos, end, cluster_ts, cluster_max_ts,
                                      f"Element 0x{eid:X} im Cluster mit unbekannter Größe.", pos)
                return
            data_start = pos + id_len + size_len
            child_end = data_start + size
            if end is not None and child_end > end:
                self._cluster_damaged(reader, hpos, end, cluster_ts, cluster_max_ts,
                                      f"Element 0x{eid:X} bei Byte {pos} reicht über das Clusterende hinaus "
                                      f"({child_end} > {end}).", pos)
                return
            reader.read(id_len + size_len)

            if first_child and eid == CRC32 and size == 4:
                crc_expected = int.from_bytes(reader.read(4), "little")
                reader.crc_begin()
                first_child = False
                continue
            first_child = False

            if eid == CLUSTER_TIMESTAMP:
                if size > 8:
                    self._cluster_damaged(reader, hpos, end, cluster_ts, cluster_max_ts,
                                          "Clusterzeitstempel mit ungültiger Länge.", pos)
                    return
                cluster_ts_raw = decode_uint(reader.read(size))
                cluster_ts = self._ts(cluster_ts_raw)
                self._check_cluster_ts(hpos, cluster_ts)
                st.clusters.append((hpos, cluster_ts))
            elif eid == SIMPLE_BLOCK or eid == BLOCK_GROUP:
                if cluster_ts_raw is None:
                    self.add(self._f("element_missing", Severity.ERROR,
                                     "Block vor dem Clusterzeitstempel – Cluster ohne Zeitstempel.",
                                     byte_offset=pos, time_start=st.max_block_ts,
                                     time_precision=TimePrecision.EXACT if st.max_block_ts is not None else TimePrecision.UNKNOWN,
                                     time_source="Letzter gültiger Blockzeitstempel"),
                             aggregate_key=("no_cluster_ts", hpos))
                    cluster_ts_raw = 0
                    cluster_ts = st.max_block_end or 0.0
                if eid == SIMPLE_BLOCK:
                    ok, ts = self._parse_block(reader, pos, size, cluster_ts_raw, None)
                else:
                    ok, ts = self._parse_block_group(reader, pos, child_end, cluster_ts_raw)
                if not ok:
                    self._cluster_damaged(reader, hpos, end, cluster_ts, cluster_max_ts,
                                          f"Ungültiger Block im Cluster bei Byte {pos}.", pos, already_reported=True)
                    return
                if ts is not None and (cluster_max_ts is None or ts > cluster_max_ts):
                    cluster_max_ts = ts
            else:
                got = reader.skip(size)
                if got < size:
                    break

        if crc_expected is not None and end is not None and reader.pos < end:
            reader.crc_abort()  # unvollständiger Cluster: CRC nicht aussagekräftig
            crc_expected = None
        if crc_expected is not None:
            actual = reader.crc_end()
            st.crc_checked += 1
            if actual is not None and actual != crc_expected:
                st.crc_failed += 1
                self.add(self._f(
                    "cluster_crc", Severity.ERROR,
                    "Die CRC-32-Prüfsumme des Clusters stimmt nicht – Clusterdaten sind verändert/beschädigt.",
                    byte_offset=hpos, byte_end=end,
                    time_start=cluster_ts, time_end=cluster_max_ts,
                    time_precision=TimePrecision.RANGE if cluster_ts is not None else TimePrecision.UNKNOWN,
                    time_source="Zeitstempelbereich des Clusters",
                    raw_message=f"CRC erwartet {crc_expected:08x}, berechnet {actual:08x}",
                ))
        if cluster_ts_raw is None:
            self.add(self._f("element_missing", Severity.ERROR, "Cluster ohne Zeitstempel-Element.",
                             byte_offset=hpos, time_start=st.max_block_end,
                             time_precision=TimePrecision.EXACT if st.max_block_end is not None else TimePrecision.UNKNOWN,
                             time_source="Letzter gültiger Blockzeitstempel"))
        if end is not None and reader.pos < end and reader.pos >= file_end:
            self._truncated(hpos, f"Der Cluster bei Byte {hpos} (Zeit {cluster_ts:.3f} s) ist unvollständig."
                            if cluster_ts is not None else None)

    def _cluster_damaged(self, reader: Reader, hpos: int, end: int | None, cluster_ts: float | None,
                         cluster_max_ts: float | None, message: str, err_pos: int,
                         already_reported: bool = False) -> None:
        st = self.state
        reader.crc_abort()
        start_ts = cluster_max_ts if cluster_max_ts is not None else cluster_ts
        if start_ts is None:
            start_ts = st.max_block_ts
        if end is not None and end <= st.file_size:
            # Rest des Clusters lesen; danach muss ein gültiges Level-1-Element folgen
            reader.seek(err_pos)
            reader.skip(end - err_pos)
            head = reader.peek(32)
            next_ok = False
            next_ts = None
            if not head or end >= (st.segment_end or st.file_size):
                next_ok = True
            else:
                try:
                    nid, _ = parse_id(head, end)
                    next_ok = nid in LEVEL1_IDS or nid in (VOID, CRC32)
                    if nid == CLUSTER:
                        next_ts = self._peek_cluster_ts(head)
                except (EBMLError, EndOfFile):
                    next_ok = False
            if next_ok:
                lost = end - err_pos
                self.add(self._f(
                    "cluster_damaged", Severity.ERROR,
                    message + f" Der Rest des Clusters ({lost} Byte) ist nicht verwertbar.",
                    byte_offset=err_pos, byte_end=end,
                    time_start=start_ts, time_end=next_ts,
                    time_precision=(TimePrecision.RANGE if next_ts is not None else TimePrecision.EXACT)
                    if start_ts is not None else TimePrecision.UNKNOWN,
                    time_source="Letzter gültiger Blockzeitstempel bis Zeitstempel des nächsten Clusters",
                ))
                return
        self._structure_error_and_resync(reader, err_pos, message, error_type="cluster_damaged", ts_hint=start_ts)

    def _resolve_backwards(self, resumed: bool) -> None:
        """Bewertet einen zurückgestellten großen Rücksprung.

        Bestehen die zurückspringenden Cluster nur aus Untertitelblöcken und läuft die Zeitleiste danach normal
        weiter, ist das ein verspätet geschriebenes Untertitelpaket (z. B. PGS) – kein Inhaltssprung."""
        st = self.state
        p = st.pending_back
        st.pending_back = None
        if p is None:
            return
        types = set().union(*(c[2] for c in p["clusters"]))
        first_pos, first_ts = p["clusters"][0][0], p["clusters"][0][1]
        if resumed and types and types <= {"subtitle"}:
            self.add(self._f(
                "timestamp_backwards", Severity.WARNING,
                f"Einzelnes Untertitelpaket mit abweichendem Zeitstempel ({first_ts:.3f} s, in der Datei bei "
                f"{p['prev_ts']:.3f} s) – der Untertitel erscheint ggf. nicht oder zur falschen Zeit, "
                "Bild und Ton sind nicht betroffen.",
                byte_offset=first_pos, time_start=first_ts, time_end=p["prev_ts"],
                time_precision=TimePrecision.EXACT, time_source="Matroska-Clusterzeitstempel", heuristic=True,
            ), aggregate_key=("cluster_stray_subtitle",))
            st.prev_cluster_ts = p["prev_ts"]
            return
        self.add(self._f(
            "timestamp_backwards", Severity.ERROR,
            f"Clusterzeitstempel springt rückwärts von {p['prev_ts']:.3f} s auf {first_ts:.3f} s.",
            byte_offset=first_pos, time_start=first_ts, time_end=p["prev_ts"],
            time_precision=TimePrecision.EXACT, time_source="Matroska-Clusterzeitstempel",
        ), aggregate_key=("cluster_backwards",))
        st.prev_cluster_ts = p["clusters"][-1][1]

    def _check_cluster_ts(self, hpos: int, ts: float) -> None:
        st = self.state
        st.cur_cluster_types = set()
        if st.pending_back is not None:
            if ts + self.opt.cluster_back_tolerance >= st.pending_back["prev_ts"]:
                self._resolve_backwards(resumed=True)
            elif len(st.pending_back["clusters"]) < 16:
                # weiterer Cluster hinter der Zeitleiste – gemeinsam bewerten
                st.pending_back["clusters"].append((hpos, ts, st.cur_cluster_types))
                return
            else:
                self._resolve_backwards(resumed=False)
        prev_end = st.max_block_end
        if st.prev_cluster_ts is not None and ts + 0.001 < st.prev_cluster_ts \
                and st.prev_cluster_ts - ts < self.opt.cluster_back_tolerance:
            # Ein Cluster darf mit einem (z. B. Audio-)Block beginnen, der geringfügig vor dem ersten Block
            # des vorigen Clusters liegt – bei verschachtelten Spuren üblich und von der Spezifikation gedeckt.
            self.add(self._f(
                "timestamp_backwards", Severity.INFO,
                f"Clusterzeitstempel liegt {st.prev_cluster_ts - ts:.3f} s vor dem vorigen Cluster "
                "(zulässige Verschachtelung der Spuren).",
                byte_offset=hpos, time_start=ts, time_end=st.prev_cluster_ts,
                time_precision=TimePrecision.EXACT, time_source="Matroska-Clusterzeitstempel",
            ), aggregate_key=("cluster_backwards_small",))
        elif st.prev_cluster_ts is not None and ts + 0.001 < st.prev_cluster_ts:
            # Bewertung erst, wenn klar ist, was im Cluster steckt und wie es weitergeht
            st.pending_back = {"prev_ts": st.prev_cluster_ts, "clusters": [(hpos, ts, st.cur_cluster_types)]}
            return
        elif prev_end is not None:
            gap = ts - prev_end
            if gap >= self.opt.ts_jump_error:
                self.add(self._f(
                    "timestamp_jump", Severity.ERROR,
                    f"Zeitstempelsprung um {gap:.1f} s (von {prev_end:.3f} s auf {ts:.3f} s). "
                    "Player springen an dieser Stelle typischerweise weiter oder ans Ende.",
                    byte_offset=hpos, time_start=prev_end, time_end=ts,
                    time_precision=TimePrecision.EXACT, time_source="Matroska-Clusterzeitstempel",
                ))
            elif gap >= self.opt.ts_jump_warning:
                self.add(self._f(
                    "timestamp_jump", Severity.WARNING,
                    f"Auffälliger Zeitstempelsprung um {gap:.1f} s (von {prev_end:.3f} s auf {ts:.3f} s).",
                    byte_offset=hpos, time_start=prev_end, time_end=ts,
                    time_precision=TimePrecision.EXACT, time_source="Matroska-Clusterzeitstempel",
                    heuristic=True,
                ))
        st.prev_cluster_ts = ts

    def _parse_block_group(self, reader: Reader, gpos: int, gend: int, cluster_ts_raw: int) -> tuple[bool, float | None]:
        ts = None
        have_block = False
        while reader.pos < gend:
            pos = reader.pos
            try:
                eid, size, hpos, dpos = read_header(reader)
            except (EBMLError, EndOfFile) as exc:
                self.add(self._f("block_invalid", Severity.ERROR, f"BlockGroup bei Byte {gpos} beschädigt: {exc}",
                                 byte_offset=pos, time_start=self.state.max_block_ts,
                                 time_precision=TimePrecision.EXACT if self.state.max_block_ts is not None else TimePrecision.UNKNOWN,
                                 time_source="Letzter gültiger Blockzeitstempel"))
                return False, None
            if eid not in BLOCK_GROUP_CHILDREN or size == UNKNOWN_SIZE or dpos + size > gend:
                self.add(self._f("block_invalid", Severity.ERROR,
                                 f"BlockGroup bei Byte {gpos} enthält ein ungültiges Element 0x{eid:X}.",
                                 byte_offset=pos, time_start=self.state.max_block_ts,
                                 time_precision=TimePrecision.EXACT if self.state.max_block_ts is not None else TimePrecision.UNKNOWN,
                                 time_source="Letzter gültiger Blockzeitstempel"))
                return False, None
            if eid == BLOCK:
                ok, ts = self._parse_block(reader, hpos, size, cluster_ts_raw, None)
                if not ok:
                    return False, None
                have_block = True
            else:
                if reader.skip(size) < size:
                    return True, ts
        if not have_block:
            self.add(self._f("block_invalid", Severity.WARNING, "BlockGroup ohne Block.", byte_offset=gpos,
                             heuristic=True), aggregate_key=("empty_blockgroup",))
        return True, ts

    def _parse_block(self, reader: Reader, pos: int, size: int, cluster_ts_raw: int,
                     _unused) -> tuple[bool, float | None]:
        st = self.state
        start = reader.pos
        head = reader.peek(min(size, 16))
        if len(head) < min(size, 4):
            reader.skip(size)
            return True, None
        try:
            track, tlen = parse_size(head, start)
        except EBMLError as exc:
            self.add(self._f("block_invalid", Severity.ERROR, f"Blockkopf bei Byte {pos} beschädigt: {exc}",
                             byte_offset=pos, time_start=st.max_block_ts,
                             time_precision=TimePrecision.EXACT if st.max_block_ts is not None else TimePrecision.UNKNOWN,
                             time_source="Letzter gültiger Blockzeitstempel"))
            return False, None
        if size < tlen + 3:
            self.add(self._f("block_invalid", Severity.ERROR, f"Block bei Byte {pos} ist zu kurz ({size} Byte).",
                             byte_offset=pos))
            return False, None
        rel = struct.unpack(">h", head[tlen:tlen + 2])[0]
        flags = head[tlen + 2]
        ts = self._ts(cluster_ts_raw + rel)
        info = st.tracks.get(track)
        if info is None:
            if st.tracks_seen:
                self.add(self._f(
                    "block_invalid", Severity.ERROR,
                    f"Block bei Byte {pos} gehört zu einer nicht deklarierten Spur ({track}) – Blockdaten vermutlich zerstört.",
                    byte_offset=pos, time_start=st.max_block_ts,
                    time_precision=TimePrecision.EXACT if st.max_block_ts is not None else TimePrecision.UNKNOWN,
                    time_source="Letzter gültiger Blockzeitstempel"))
                return False, None
            info = st.tracks.setdefault(track, TrackInfo(number=track, type="unknown"))

        lacing = (flags >> 1) & 0x03
        payload_size = size - tlen - 3
        frames = 1
        if lacing:
            ok, frames = self._check_lacing(reader, start + tlen + 3, payload_size, lacing)
            if not ok:
                self.add(self._f("block_invalid", Severity.ERROR,
                                 f"Ungültiges Lacing im Block bei Byte {pos} (Spur {track}).",
                                 byte_offset=pos, stream_index=track - 1, stream_type=info.type,
                                 time_start=ts, time_precision=TimePrecision.EXACT,
                                 time_source="Blockzeitstempel (aus beschädigtem Block, nur Anhaltspunkt)"))
                return False, None
        if reader.skip(size) < size:
            return True, None

        # Zeitstempelprüfungen pro Spur
        st.cur_cluster_types.add(info.type)
        info.blocks += 1
        if info.first_ts is None:
            info.first_ts = ts
        if info.type in ("audio",) and info.prev_ts is not None:
            if ts + 0.001 < info.prev_ts:
                self.add(self._f(
                    "timestamp_backwards", Severity.WARNING,
                    f"Audiozeitstempel der Spur {track} laufen rückwärts.",
                    stream_index=track - 1, stream_type="audio", byte_offset=pos,
                    time_start=ts, time_end=info.prev_ts, time_precision=TimePrecision.EXACT,
                    time_source="Matroska-Blockzeitstempel", heuristic=True),
                    aggregate_key=("audio_backwards", track))
        if info.type in ("audio", "video") and info.max_ts is not None:
            gap = ts - info.max_ts
            if gap >= self.opt.track_gap_warning:
                self.add(self._f(
                    "timestamp_gap", Severity.WARNING,
                    f"Lücke von {gap:.1f} s in Spur {track} ({info.type}) zwischen {info.max_ts:.3f} s und {ts:.3f} s.",
                    stream_index=track - 1, stream_type=info.type, byte_offset=pos,
                    time_start=info.max_ts, time_end=ts, time_precision=TimePrecision.EXACT,
                    time_source="Matroska-Blockzeitstempel", heuristic=True),
                    aggregate_key=("gap", track, round(info.max_ts, 1)))
        info.prev_ts = ts
        if info.max_ts is None or ts > info.max_ts:
            info.max_ts = ts
        frame_dur = (info.default_duration_ns or 0) / 1e9 * frames
        end_ts = ts + frame_dur
        if info.type in ("audio", "video"):
            st.last_block_ts = ts
            if st.max_block_ts is None or ts > st.max_block_ts:
                st.max_block_ts = ts
            if st.max_block_end is None or end_ts > st.max_block_end:
                st.max_block_end = end_ts
        return True, ts

    def _check_lacing(self, reader: Reader, start: int, payload: int, lacing: int) -> tuple[bool, int]:
        head = reader.peek(min(payload + (start - reader.pos), 64 * 1024))
        off = start - reader.pos
        if payload < 1:
            return False, 0
        count = head[off] + 1
        p = off + 1
        sizes = []
        try:
            if lacing == 1:  # Xiph
                for _ in range(count - 1):
                    s = 0
                    while True:
                        b = head[p]
                        p += 1
                        s += b
                        if b != 255:
                            break
                    sizes.append(s)
            elif lacing == 3:  # EBML
                first, l1 = parse_size(head[p:p + 8], start)
                if first == UNKNOWN_SIZE:
                    return False, count
                p += l1
                sizes.append(first)
                prev = first
                for _ in range(count - 2):
                    raw, ln = parse_size(head[p:p + 8], start)
                    if raw == UNKNOWN_SIZE:
                        return False, count
                    p += ln
                    delta = raw - ((1 << (7 * ln - 1)) - 1)
                    prev = prev + delta
                    if prev < 0:
                        return False, count
                    sizes.append(prev)
            elif lacing == 2:  # fixed
                data_len = payload - 1
                return data_len % count == 0, count
        except (IndexError, EBMLError, EndOfFile):
            return False, count
        header_len = p - off
        remaining = payload - header_len - sum(sizes)
        return remaining >= 0, count

    # -- Abschlussprüfungen --
    def _final_checks(self) -> None:
        st = self.state
        if st.doc_type not in ("matroska", "webm") or st.segment_data_start is None:
            return
        if not st.tracks_seen:
            self.add(self._f("element_missing", Severity.CRITICAL, "Kein Tracks-Element gefunden – Spuren unbekannt."))
        if not st.clusters:
            self.add(self._f("element_missing", Severity.CRITICAL, "Keine Cluster gefunden – die Datei enthält keine Mediendaten."))
            return
        if self._truncation_pending:
            self._truncated(st.file_size, None)
        if st.pending_back is not None:
            self._resolve_backwards(resumed=True)  # Dateiende: Untertitel-Ausreißer am Schluss

        # Laufzeit laut Info gegen tatsächliche Blockzeitstempel
        if st.info_duration and st.max_block_end is not None:
            first = min((t.first_ts for t in st.tracks.values() if t.first_ts is not None and t.type in ("audio", "video")),
                        default=0.0)
            actual = st.max_block_end - min(first, 0.0)
            diff = st.info_duration - actual
            tol = max(self.opt.duration_tolerance_s, st.info_duration * self.opt.duration_tolerance_pct / 100)
            if abs(diff) > tol:
                truncated = any(f.error_type == "truncated" for f in st.findings)
                self.add(self._f(
                    "duration_mismatch", Severity.WARNING,
                    f"Laufzeit laut Segment-Info {st.info_duration:.1f} s, letzte Blockdaten bei {actual:.1f} s "
                    f"(Abweichung {diff:+.1f} s)." + (" Ursache ist das abgeschnittene Dateiende." if truncated else ""),
                    heuristic=True,
                    time_start=actual if diff > 0 else None,
                    time_end=st.info_duration if diff > 0 else None,
                    time_precision=TimePrecision.RANGE if diff > 0 else TimePrecision.UNKNOWN,
                    time_source="Letzter Blockzeitstempel bis Laufzeit laut Segment-Info",
                ))

        truncated = any(f.error_type == "truncated" for f in st.findings)
        # Cues prüfen (Einträge hinter einem abgeschnittenen Dateiende sind Folge des Abschneidens)
        cluster_pos = {p for p, _ in st.clusters}
        if st.cue_positions and st.segment_data_start is not None:
            bad = [(t, p) for t, p in st.cue_positions
                   if st.segment_data_start + p not in cluster_pos and st.segment_data_start + p < st.file_size]
            if bad:
                first_bad = bad[0]
                self.add(self._f(
                    "index_invalid", Severity.WARNING,
                    f"{len(bad)} von {len(st.cue_positions)} Indexeinträgen (Cues) verweisen nicht auf einen Clusteranfang. "
                    "Spulen kann an diesen Stellen fehlschlagen oder an eine falsche Stelle springen.",
                    time_start=first_bad[0], time_precision=TimePrecision.EXACT,
                    time_source="CueTime des ersten fehlerhaften Indexeintrags", heuristic=True,
                    extra={"invalid_cues": len(bad)},
                ))
        elif not st.has_cues and len(st.clusters) > 1 and not truncated:
            self.add(self._f("missing_index", Severity.INFO,
                             "Kein Index (Cues) vorhanden – Spulen ist langsamer, aber die Datei ist abspielbar."))

        # SeekHead prüfen
        if st.seek_entries and st.segment_data_start is not None:
            bad = []
            for sid, spos in st.seek_entries:
                abs_pos = st.segment_data_start + spos
                if abs_pos >= st.file_size:
                    if not truncated:
                        bad.append((sid, spos))
                elif st.level1.get(abs_pos) not in (sid, None):
                    bad.append((sid, spos))
                elif abs_pos not in st.level1 and sid in LEVEL1_IDS:
                    bad.append((sid, spos))
            if bad:
                names = ", ".join(sorted({LEVEL1_NAMES.get(s, hex(s)) for s, _ in bad}))
                self.add(self._f("index_invalid", Severity.WARNING,
                                 f"SeekHead verweist auf ungültige Positionen ({names}).", heuristic=True))


def validate(path: str, options: Options | None = None, emit: Callable[[dict], None] | None = None,
             pause_check: Callable[[], None] | None = None) -> dict:
    return MatroskaValidator(path, options, emit, pause_check).run()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Matroska-Strukturprüfung (JSON-Zeilen auf stdout)")
    parser.add_argument("path")
    parser.add_argument("--ts-jump-warning", type=float, default=3.0)
    parser.add_argument("--ts-jump-error", type=float, default=30.0)
    parser.add_argument("--track-gap-warning", type=float, default=5.0)
    parser.add_argument("--duration-tolerance-s", type=float, default=5.0)
    parser.add_argument("--duration-tolerance-pct", type=float, default=2.0)
    parser.add_argument("--max-findings", type=int, default=400)
    args = parser.parse_args(argv)

    def emit(obj: dict) -> None:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    opts = Options(args.ts_jump_warning, args.ts_jump_error, args.track_gap_warning,
                   args.duration_tolerance_s, args.duration_tolerance_pct, args.max_findings)
    try:
        result = validate(args.path, opts, emit)
    except PermissionError as exc:
        emit({"t": "fatal", "kind": "permission_denied", "message": str(exc)})
        return 3
    except FileNotFoundError as exc:
        emit({"t": "fatal", "kind": "not_found", "message": str(exc)})
        return 4
    except OSError as exc:
        emit({"t": "fatal", "kind": "os_error", "message": str(exc)})
        return 5
    emit({"t": "summary", **result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
