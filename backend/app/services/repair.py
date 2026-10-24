"""Vorbereitete Reparaturfunktion – standardmäßig DEAKTIVIERT.

Grundsätze:
* nur aktiv, wenn ``REPAIR_ENABLED=true`` gesetzt ist
* nur nach ausdrücklicher Bestätigung (Dateiname muss erneut eingegeben werden)
* schreibt ausschließlich in eine neue Zieldatei unter ``/config/repaired``
  (nie in eine Medienbibliothek, nie über eine bestehende Datei)
* das Original wird nur gelesen

Derzeit ist eine Aktion umgesetzt: ``remux`` (Neu-Multiplexen mit mkvmerge).
Ein Remux kann zerstörte Cluster nicht wiederherstellen, macht aber Dateien mit
kaputtem Index/SeekHead oder fehlerhaften Elementen oft wieder spulbar.
Weitere Aktionen können über ``ACTIONS`` ergänzt werden.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from ..config import get_config
from ..models import utcnow
from ..scanners.process import ResourceLimits, ScanControl, run_process
from ..scanners.tools import get_toolset
from .pathsec import allowed_roots, is_within


class RepairDisabled(PermissionError):
    pass


class RepairError(ValueError):
    pass


@dataclass
class RepairTask:
    file_id: int
    action: str
    target: str
    state: str = "running"
    started: datetime = field(default_factory=utcnow)
    finished: datetime | None = None
    message: str | None = None
    log: list[str] = field(default_factory=list)


ACTIONS = {"remux": "Remux mit mkvmerge in eine neue Datei (Original bleibt unverändert)"}
TASKS: dict[int, RepairTask] = {}


def repair_enabled() -> bool:
    return get_config().repair_enabled


def _target_path(file_id: int, filename: str) -> str:
    cfg = get_config()
    safe = re.sub(r"[^\w.\- ()\[\]äöüÄÖÜß]", "_", os.path.splitext(filename)[0])[:150]
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    target = cfg.repair_dir / f"{file_id}-{safe}.remux-{stamp}.mkv"
    for root in allowed_roots():
        if is_within(str(target.parent), root):
            raise RepairError("Das Reparaturziel darf nicht in einer Medienbibliothek liegen")
    return str(target)


async def start_repair(file_id: int, source_path: str, filename: str, action: str, confirm: bool,
                       confirm_filename: str | None) -> RepairTask:
    if not repair_enabled():
        raise RepairDisabled("Die Reparaturfunktion ist deaktiviert (REPAIR_ENABLED=false).")
    if action not in ACTIONS:
        raise RepairError("Unbekannte Reparaturaktion")
    if not confirm or confirm_filename != filename:
        raise RepairError("Ausdrückliche Bestätigung erforderlich: Dateinamen exakt eingeben.")
    if file_id in TASKS and TASKS[file_id].state == "running":
        raise RepairError("Für diese Datei läuft bereits eine Reparatur")
    target = _target_path(file_id, filename)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    # Ziel exklusiv anlegen: niemals eine bestehende Datei überschreiben
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.close(fd)
    os.unlink(target)
    task = RepairTask(file_id=file_id, action=action, target=target)
    TASKS[file_id] = task

    async def run() -> None:
        toolset = get_toolset()
        cmd = [toolset.mkvmerge.path or "mkvmerge", "--ui-language", "en_US", "--gui-mode", "-o", target, source_path]
        res = await run_process(cmd, ScanControl(), ResourceLimits(), on_stdout=task.log.append,
                                on_stderr=task.log.append)
        task.finished = utcnow()
        if res.returncode in (0, 1) and os.path.exists(target):
            task.state = "done"
            task.message = f"Neue Datei geschrieben: {target}" + (" (mit Warnungen)" if res.returncode == 1 else "")
        else:
            task.state = "failed"
            task.message = f"mkvmerge fehlgeschlagen (Exit-Code {res.returncode})"
            if os.path.exists(target):
                os.unlink(target)
        del task.log[:-200]

    asyncio.create_task(run())
    return task
