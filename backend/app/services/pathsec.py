"""Pfadvalidierung: Zugriffe nur innerhalb der konfigurierten Medienwurzeln.

Alle Pfade werden über ``os.path.realpath`` aufgelöst, damit Symlinks nicht aus
einer Bibliothek herausführen können.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import get_config


class PathSecurityError(ValueError):
    pass


def real(path: str | os.PathLike) -> str:
    return os.path.realpath(os.fspath(path))


def is_within(child: str, parent: str) -> bool:
    child_r, parent_r = real(child), real(parent)
    try:
        return os.path.commonpath([child_r, parent_r]) == parent_r
    except ValueError:
        return False


def allowed_roots() -> list[str]:
    return [real(p) for p in get_config().media_roots]


def validate_library_path(path: str) -> str:
    """Prüft einen vom Benutzer angegebenen Bibliothekspfad und liefert den kanonischen Pfad."""
    if not path or "\x00" in path:
        raise PathSecurityError("Ungültiger Pfad")
    p = Path(path)
    if not p.is_absolute():
        raise PathSecurityError("Der Pfad muss absolut sein (z. B. /media/Filme)")
    resolved = real(p)
    roots = allowed_roots()
    if not any(os.path.commonpath([resolved, r]) == r for r in roots):
        raise PathSecurityError(
            "Der Pfad liegt außerhalb der freigegebenen Medienverzeichnisse (" + ", ".join(roots) + ")")
    if not os.path.isdir(resolved):
        raise PathSecurityError("Das Verzeichnis existiert nicht oder ist nicht lesbar")
    return resolved


def resolve_media_file(library_path: str, file_path: str) -> str:
    """Kanonischer Pfad einer Mediendatei; verhindert Symlink-Ausbrüche aus der Bibliothek."""
    resolved = real(file_path)
    if not is_within(resolved, library_path):
        raise PathSecurityError("Datei liegt (über einen Symlink) außerhalb der Bibliothek")
    return resolved
