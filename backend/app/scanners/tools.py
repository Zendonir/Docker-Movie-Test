"""Erkennung der installierten Prüfwerkzeuge und ihrer Fähigkeiten."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass
class ToolInfo:
    name: str
    path: str | None
    version: str | None
    available: bool


@dataclass
class Toolset:
    ffmpeg: ToolInfo
    ffprobe: ToolInfo
    mkvmerge: ToolInfo
    mkvinfo: ToolInfo
    python: str
    decodable_codecs: frozenset[str] = field(default_factory=frozenset)
    hwaccels: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            t.name: {"path": t.path, "version": t.version, "available": t.available}
            for t in (self.ffmpeg, self.ffprobe, self.mkvmerge, self.mkvinfo)
        } | {"python": self.python, "hwaccels": list(self.hwaccels), "decodable_codecs": len(self.decodable_codecs)}

    def versions(self) -> dict[str, str | None]:
        return {t.name: t.version for t in (self.ffmpeg, self.ffprobe, self.mkvmerge, self.mkvinfo)}


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        return out.stdout + out.stderr
    except (OSError, subprocess.SubprocessError):
        return ""


def _tool(name: str, version_args: list[str], pattern: str) -> ToolInfo:
    path = shutil.which(name)
    if not path:
        return ToolInfo(name, None, None, False)
    out = _run([path, *version_args])
    m = re.search(pattern, out)
    return ToolInfo(name, path, m.group(1) if m else None, True)


def parse_codecs(output: str) -> frozenset[str]:
    """Liest ``ffmpeg -codecs`` aus und liefert alle Codec-IDs mit Decoder."""
    codecs: set[str] = set()
    started = False
    for line in output.splitlines():
        if line.strip().startswith("-------"):
            started = True
            continue
        if not started:
            continue
        m = re.match(r"^\s([D.])([E.])([VASDT.])[I.][L.][S.]\s+(\S+)", line)
        if m and m.group(1) == "D":
            codecs.add(m.group(4))
    return frozenset(codecs)


@lru_cache(maxsize=1)
def get_toolset() -> Toolset:
    ffmpeg = _tool("ffmpeg", ["-version"], r"ffmpeg version (\S+)")
    ffprobe = _tool("ffprobe", ["-version"], r"ffprobe version (\S+)")
    mkvmerge = _tool("mkvmerge", ["--version"], r"mkvmerge (v[\d.]+)")
    mkvinfo = _tool("mkvinfo", ["--version"], r"mkvinfo (v[\d.]+)")
    decodable: frozenset[str] = frozenset()
    hwaccels: tuple[str, ...] = ()
    if ffmpeg.available:
        decodable = parse_codecs(_run([ffmpeg.path or "ffmpeg", "-hide_banner", "-codecs"]))
        hw = _run([ffmpeg.path or "ffmpeg", "-hide_banner", "-hwaccels"])
        hwaccels = tuple(line.strip() for line in hw.splitlines()[1:] if line.strip())
    return Toolset(ffmpeg, ffprobe, mkvmerge, mkvinfo, sys.version.split()[0], decodable, hwaccels)
