"""Dateikennungen für die Wiedererkennung verschobener/umbenannter Dateien."""

from __future__ import annotations

import hashlib
import os

SAMPLE = 64 * 1024


def quick_hash(path: str, size: int | None = None) -> str:
    """SHA-256 über Größe sowie je 64 KiB vom Anfang, aus der Mitte und vom Ende.

    Liest höchstens 192 KiB pro Datei und ist damit auch für große Bibliotheken
    tragbar. Für einen Inhaltsnachweis ist ``full_hash`` zu verwenden.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        if size is None:
            size = os.fstat(fd).st_size
        h = hashlib.sha256()
        h.update(str(size).encode())
        offsets = [0]
        if size > SAMPLE:
            offsets += [max(size // 2 - SAMPLE // 2, 0), max(size - SAMPLE, 0)]
        for off in offsets:
            h.update(os.pread(fd, SAMPLE, off))
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
        return h.hexdigest()
    finally:
        os.close(fd)


def full_hash(path: str, cancel_check=None) -> str:
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
        except (AttributeError, OSError):
            pass
        pos = 0
        while True:
            chunk = os.pread(fd, 4 * 1024 * 1024, pos)
            if not chunk:
                break
            h.update(chunk)
            pos += len(chunk)
            if cancel_check and cancel_check():
                raise InterruptedError("abgebrochen")
            if pos % (256 * 1024 * 1024) < 4 * 1024 * 1024:
                try:
                    os.posix_fadvise(fd, 0, pos, os.POSIX_FADV_DONTNEED)
                except (AttributeError, OSError):
                    pass
        return h.hexdigest()
    finally:
        os.close(fd)
