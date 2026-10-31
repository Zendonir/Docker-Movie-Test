"""Erzeugt Testmedien programmatisch (FFmpeg) und beschädigt sie gezielt."""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

CLUSTER_ID = b"\x1f\x43\xb6\x75"


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def video_encoder() -> list[str]:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if " libx264 " in out:
        return ["-c:v", "libx264", "-preset", "veryfast", "-g", "50", "-pix_fmt", "yuv420p"]
    return ["-c:v", "mpeg4", "-q:v", "5", "-g", "50"]


def make_mkv(path: Path, seconds: int = 20, size: str = "640x360") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000", "-t", str(seconds), *video_encoder(), "-c:a", "aac",
            "-b:a", "96k", str(path))
    return path


def make_mkv_mkvmerge(src: Path, dst: Path) -> Path:
    """Remux mit mkvmerge (andere Clusterstruktur, keine CRC-Elemente)."""
    subprocess.run(["mkvmerge", "-q", "-o", str(dst), str(src)], check=False)
    return dst


def make_mp4(src: Path, dst: Path) -> Path:
    _ffmpeg("-i", str(src), "-c", "copy", str(dst))
    return dst


def make_ts(src: Path, dst: Path) -> Path:
    _ffmpeg("-i", str(src), "-c", "copy", str(dst))
    return dst


def cluster_positions(data: bytes) -> list[int]:
    out, i = [], data.find(CLUSTER_ID)
    while i != -1:
        out.append(i)
        i = data.find(CLUSTER_ID, i + 1)
    return out


def copy(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


def corrupt_cluster_header(src: Path, dst: Path, index: int = 4) -> Path:
    data = bytearray(src.read_bytes())
    p = cluster_positions(data)[index]
    data[p:p + 12] = b"\xde\xad\xbe\xef" * 3
    dst.write_bytes(data)
    return dst


def corrupt_payload(src: Path, dst: Path, index: int = 7, stride: int = 7000, span: int = 140000) -> Path:
    """Flippt Bytes innerhalb der Blockdaten eines Clusters (Decoderfehler)."""
    data = bytearray(src.read_bytes())
    p = cluster_positions(data)[index]
    for k in range(p + 3000, min(p + span, len(data) - 64), stride):
        for j in range(40):
            data[k + j] ^= 0xFF
    dst.write_bytes(data)
    return dst


def flip_bytes_in_frame(src: Path, dst: Path, index: int = 5) -> Path:
    """Wenige Bytes mitten in einem Frame überschreiben (Struktur intakt, nur CRC/Decoder merken es)."""
    data = bytearray(src.read_bytes())
    p = cluster_positions(data)[index] + 60000
    data[p:p + 8] = b"\x00" * 8
    dst.write_bytes(data)
    return dst


def truncate(src: Path, dst: Path, fraction: float = 0.6) -> Path:
    data = src.read_bytes()
    dst.write_bytes(data[: int(len(data) * fraction)])
    return dst


def corrupt_ebml_header(src: Path, dst: Path) -> Path:
    data = bytearray(src.read_bytes())
    data[0:4] = b"\x00\x00\x00\x00"
    dst.write_bytes(data)
    return dst


def unknown_codec(src: Path, dst: Path) -> Path:
    """Ersetzt die CodecID der Videospur durch eine gleich lange unbekannte ID (Struktur bleibt gültig)."""
    data = src.read_bytes()
    for old in (b"V_MPEG4/ISO/AVC", b"V_MPEG4/ISO/ASP"):
        if old in data:
            data = data.replace(old, b"V_XYZ12/ISO/QQQ"[: len(old)], 1)
            break
    else:
        raise RuntimeError("Keine bekannte CodecID gefunden")
    dst.write_bytes(data)
    return dst


def timestamp_jump(src: Path, dst: Path, index: int = 6) -> Path:
    data = bytearray(src.read_bytes())
    p = cluster_positions(data)[index]
    q = data.find(b"\xe7\x82", p, p + 30)
    data[q + 2:q + 4] = b"\xff\xff"
    dst.write_bytes(data)
    return dst


def damage_ts(src: Path, dst: Path) -> Path:
    data = bytearray(src.read_bytes())
    rnd = random.Random(3)
    for k in range(300000, min(800000, len(data) - 200), 50000):
        data[k:k + 188] = bytes(rnd.getrandbits(8) for _ in range(188))
    dst.write_bytes(data)
    return dst


def make_multi_mkv(path: Path, seconds: int = 8) -> Path:
    """Video + GER AC3 5.1 (Standard) + GER DTS 5.1 + ENG AAC + Untertitel GER/ENG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    srt_de = path.parent / f".{path.stem}.de.srt"
    srt_en = path.parent / f".{path.stem}.en.srt"
    srt_de.write_text("1\n00:00:01,000 --> 00:00:03,000\nHallo Welt – Größe\n", encoding="utf-8")
    srt_en.write_text("1\n00:00:01,000 --> 00:00:03,000\nHello world\n", encoding="utf-8")
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-f", "lavfi", "-i", "sine=440:sample_rate=48000",
            "-f", "lavfi", "-i", "sine=660:sample_rate=48000", "-f", "lavfi", "-i", "sine=880:sample_rate=48000",
            "-i", str(srt_de), "-i", str(srt_en), "-t", str(seconds),
            "-map", "0", "-map", "1", "-map", "2", "-map", "3", "-map", "4", "-map", "5", *video_encoder(),
            "-c:a:0", "ac3", "-ac:a:0", "6", "-c:a:1", "dca", "-strict", "-2", "-ac:a:1", "6", "-c:a:2", "aac",
            "-c:s", "srt", "-metadata:s:a:0", "language=ger", "-metadata:s:a:1", "language=ger",
            "-metadata:s:a:2", "language=eng", "-metadata:s:s:0", "language=ger", "-metadata:s:s:1", "language=eng",
            "-disposition:a:0", "default", "-metadata", "title=Testfilm", str(path))
    srt_de.unlink()
    srt_en.unlink()
    return path


def stray_subtitle_cluster(src: Path, dst: Path, ts_ms: int = 1000) -> Path:
    """Fügt vor Cluster `index` einen Cluster ein, der nur ein Untertitelpaket mit früherem Zeitstempel enthält
    (wie ein verspätet geschriebenes PGS-Paket). Die Zeitleiste läuft danach normal weiter."""
    srt = dst.parent / f".{dst.stem}.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHallo\n", encoding="utf-8")
    tmp = dst.parent / f".{dst.stem}.tmp.mkv"
    subprocess.run(["mkvmerge", "-q", "-o", str(tmp), str(src), str(srt)], check=False)
    srt.unlink()
    data = bytearray(tmp.read_bytes())
    tmp.unlink()
    sub_track = 3  # Video 1, Audio 2, Untertitel 3
    payload = b"x"
    block = bytes([0x80 | sub_track]) + (0).to_bytes(2, "big") + b"\x80" + payload
    ts = ts_ms.to_bytes(4, "big")
    body = b"\xe7\x84" + ts + b"\xa3" + bytes([0x80 | len(block)]) + block
    cluster = b"\x1f\x43\xb6\x75" + bytes([0x80 | len(body)]) + body
    clusters = cluster_positions(data)
    assert len(clusters) >= 3, clusters
    pos = clusters[-2]  # zwischen zwei normalen Clustern gegen Ende
    data[pos:pos] = cluster
    # Segmentgröße anpassen (mkvmerge schreibt sie mit 8 Byte)
    seg = data.find(b"\x18\x53\x80\x67")
    assert data[seg + 4] == 0x01
    size = int.from_bytes(data[seg + 5:seg + 12], "big") + len(cluster)
    data[seg + 5:seg + 12] = size.to_bytes(7, "big")
    dst.write_bytes(data)
    return dst
