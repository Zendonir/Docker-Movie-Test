"""Zielpfade, temporäre Dateien und sicheres Abschließen (nie überschreiben, Original nur auf Freigabe)."""

from __future__ import annotations

import errno
import os
import shutil
import time
from dataclasses import dataclass

from ..config import get_config
from ..services.pathsec import allowed_roots, is_within, real
from .rules import FileOps

MODES = {
    "output_dir": "Neue Datei im Ausgabeverzeichnis (Original bleibt unverändert)",
    "beside": "Neue Datei neben dem Original (mit Suffix)",
    "replace_backup": "Original ersetzen, Original in Sicherungsordner verschieben",
    "replace_delete": "Original ersetzen und löschen",
}
REPLACE_MODES = ("replace_backup", "replace_delete")


class FileOpError(Exception):
    pass


@dataclass
class Target:
    final_path: str
    temp_dir: str
    replace: bool
    backup_path: str | None = None


def output_root() -> str:
    return real(get_config().optimizer_output_dir)


def mode_availability(library_paths: list[str]) -> dict[str, dict]:
    """Welche Ausgabemodi sind mit den aktuellen Mounts möglich?"""
    out_root = output_root()
    out_ok = os.path.isdir(out_root) and os.access(out_root, os.W_OK)
    lib_writable = any(os.access(p, os.W_OK) for p in library_paths)
    return {
        "output_dir": {"available": out_ok, "reason": None if out_ok else
                       f"Ausgabeverzeichnis {out_root} fehlt oder ist nicht beschreibbar (Volume /output einbinden)"},
        "beside": {"available": lib_writable, "reason": None if lib_writable else
                   "Medienverzeichnis ist schreibgeschützt eingebunden (:ro)"},
        "replace_backup": {"available": lib_writable, "reason": None if lib_writable else
                           "Medienverzeichnis ist schreibgeschützt eingebunden (:ro)"},
        "replace_delete": {"available": lib_writable, "reason": None if lib_writable else
                           "Medienverzeichnis ist schreibgeschützt eingebunden (:ro)"},
    }


def _unique(path: str) -> str:
    if not os.path.lexists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(2, 1000):
        cand = f"{stem} ({i}){ext}"
        if not os.path.lexists(cand):
            return cand
    raise FileOpError("Kein freier Dateiname gefunden")


def plan_target(source: str, library_path: str, rel_path: str, ops: FileOps, filename: str | None = None) -> Target:
    """filename: gewünschter Dateiname (ohne Pfad) für Ausgabe-/Daneben-Modus."""
    src_dir = os.path.dirname(source)
    stem = os.path.splitext(os.path.basename(source))[0]
    if ops.output_mode in REPLACE_MODES:
        backup = None
        if ops.output_mode == "replace_backup":
            backup = os.path.join(library_path, ops.backup_dir, rel_path)
            if not is_within(backup, library_path):
                raise FileOpError("Sicherungsordner liegt außerhalb der Bibliothek")
            if os.path.lexists(backup):
                b_stem, b_ext = os.path.splitext(backup)
                backup = f"{b_stem}.{time.strftime('%Y%m%d-%H%M%S')}{b_ext}"
        return Target(final_path=source, temp_dir=src_dir, replace=True, backup_path=backup)
    if ops.output_mode == "beside":
        name = filename or f"{stem}{ops.suffix}.mkv"
        if os.path.join(src_dir, name) == source:
            name = f"{stem}{ops.suffix or '.neu'}.mkv"
        return Target(final_path=_unique(os.path.join(src_dir, name)), temp_dir=src_dir, replace=False)
    # output_dir
    # Das Ausgabeverzeichnis spiegelt die Struktur unterhalb der Medienwurzel (/media/Filme/x.mkv -> /output/Filme/x.mkv)
    root = output_root()
    lib_real = real(library_path)
    base = ""
    for media_root in allowed_roots():
        if is_within(lib_real, media_root):
            base = os.path.relpath(lib_real, media_root)
            if len(allowed_roots()) > 1:
                base = os.path.join(os.path.basename(media_root.rstrip("/")) or "media", base)
            break
    dest_dir = os.path.normpath(os.path.join(root, base, os.path.dirname(rel_path)))
    name = filename or os.path.basename(source)
    final = os.path.join(dest_dir, name)
    if not is_within(final, root):
        raise FileOpError("Zielpfad liegt außerhalb des Ausgabeverzeichnisses")
    return Target(final_path=_unique(final), temp_dir=dest_dir, replace=False)


def prepare_dir(target: Target, needed_bytes: int) -> None:
    os.makedirs(target.temp_dir, exist_ok=True)
    if not os.access(target.temp_dir, os.W_OK):
        raise FileOpError(f"Keine Schreibberechtigung in {target.temp_dir}")
    free = shutil.disk_usage(target.temp_dir).free
    if free < needed_bytes:
        raise FileOpError(f"Zu wenig Speicherplatz in {target.temp_dir}: {free / 1e9:.1f} GB frei, "
                          f"{needed_bytes / 1e9:.1f} GB benötigt")
    if target.backup_path:
        os.makedirs(os.path.dirname(target.backup_path), exist_ok=True)


def temp_path(target: Target, item_id: int, label: str = "", ext: str = ".mkv") -> str:
    stem = os.path.splitext(os.path.basename(target.final_path))[0][:120]
    return os.path.join(target.temp_dir, f".{stem}.mis-tmp-{item_id}{label}{ext}")


def remove_quiet(paths: list[str]) -> None:
    for p in paths:
        try:
            if p and os.path.basename(p).startswith(".") and ".mis-tmp-" in p:
                os.unlink(p)
        except FileNotFoundError:
            pass


def finalize(target: Target, temp: str, source: str, source_stat: os.stat_result) -> dict:
    """Schließt eine erfolgreich geprüfte temporäre Datei ab. Rückgabe: Informationen für das Protokoll."""
    try:
        shutil.copymode(source, temp)
    except OSError:
        pass
    st = os.stat(source)
    if (st.st_size, st.st_mtime_ns, st.st_ino) != (source_stat.st_size, source_stat.st_mtime_ns, source_stat.st_ino):
        raise FileOpError("Das Original wurde während der Bearbeitung verändert – Ergebnis verworfen")
    if not target.replace:
        _link_no_overwrite(temp, target.final_path)
        return {"final": target.final_path}
    if target.backup_path:
        try:
            os.rename(source, target.backup_path)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise FileOpError("Sicherungsordner liegt auf einem anderen Dateisystem – Original nicht verschoben") from exc
            raise
        try:
            os.rename(temp, source)
        except OSError:
            os.rename(target.backup_path, source)  # Rückgängig machen
            raise
        return {"final": source, "backup": target.backup_path}
    os.replace(temp, source)
    return {"final": source}


def _link_no_overwrite(temp: str, final: str) -> None:
    try:
        os.link(temp, final)  # schlägt fehl, falls das Ziel existiert -> nie überschreiben
        os.unlink(temp)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise FileOpError(f"Zieldatei existiert bereits: {final}") from exc
        if exc.errno in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK):
            if os.path.lexists(final):
                raise FileOpError(f"Zieldatei existiert bereits: {final}") from exc
            os.rename(temp, final)
        else:
            raise
