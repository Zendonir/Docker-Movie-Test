"""Ausführung externer Prüfwerkzeuge.

* Programme werden ausschließlich mit Argumentlisten gestartet (kein Shell).
* CPU- und E/A-Priorität werden über ``nice``/``ionice`` gesenkt.
* Pausieren erfolgt per SIGSTOP/SIGCONT an die Prozessgruppe; das ist unter
  Linux zuverlässig, der Prozess setzt danach exakt an derselben Stelle fort.
* Ein Abbruch durch den Benutzer wird getrennt von einem Werkzeugfehler
  festgehalten (``ProcessResult.cancelled``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

LineHandler = Callable[[str], Awaitable[None] | None]

IO_CLASSES = {
    "idle": ["-c", "3"],
    "best-effort-low": ["-c", "2", "-n", "7"],
    "best-effort": ["-c", "2", "-n", "4"],
}


@dataclass
class ResourceLimits:
    nice: int = 10
    io_class: str = "best-effort-low"
    stall_timeout: float = 1800.0  # Sekunden ohne Ausgabe -> Werkzeug gilt als hängend


def wrap_command(cmd: list[str], limits: ResourceLimits) -> list[str]:
    prefix: list[str] = []
    if limits.io_class in IO_CLASSES and shutil.which("ionice"):
        prefix += ["ionice", *IO_CLASSES[limits.io_class]]
    if limits.nice > 0 and shutil.which("nice"):
        prefix += ["nice", "-n", str(int(limits.nice))]
    return prefix + cmd


class ScanControl:
    """Steuerung aller Prozesse einer Dateiprüfung."""

    def __init__(self) -> None:
        self._procs: set[asyncio.subprocess.Process] = set()
        self.paused = False
        self.cancelled = False
        self.cancel_reason: str | None = None
        self._running = asyncio.Event()
        self._running.set()
        self.paused_seconds = 0.0
        self._paused_at: float | None = None

    def register(self, proc: asyncio.subprocess.Process) -> None:
        self._procs.add(proc)
        if self.paused:
            self._signal(proc, signal.SIGSTOP)

    def unregister(self, proc: asyncio.subprocess.Process) -> None:
        self._procs.discard(proc)

    @staticmethod
    def _signal(proc: asyncio.subprocess.Process, sig: int) -> None:
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)

    def pause(self) -> None:
        if self.paused or self.cancelled:
            return
        self.paused = True
        self._paused_at = time.monotonic()
        self._running.clear()
        for p in list(self._procs):
            self._signal(p, signal.SIGSTOP)

    def resume(self) -> None:
        if not self.paused:
            return
        self.paused = False
        if self._paused_at is not None:
            self.paused_seconds += time.monotonic() - self._paused_at
            self._paused_at = None
        for p in list(self._procs):
            self._signal(p, signal.SIGCONT)
        self._running.set()

    def cancel(self, reason: str = "user") -> None:
        if self.cancelled:
            return
        self.cancelled = True
        self.cancel_reason = reason
        for p in list(self._procs):
            self._signal(p, signal.SIGCONT)
            self._signal(p, signal.SIGTERM)
        self._running.set()
        asyncio.get_event_loop().call_later(5.0, self._kill_all)

    def _kill_all(self) -> None:
        for p in list(self._procs):
            self._signal(p, signal.SIGKILL)

    async def wait_running(self) -> None:
        await self._running.wait()


@dataclass
class ProcessResult:
    returncode: int | None
    cancelled: bool = False
    stalled: bool = False
    spawn_error: str | None = None
    duration: float = 0.0
    stdout_tail: list[str] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)

    @property
    def killed_by_signal(self) -> int | None:
        if self.returncode is not None and self.returncode < 0:
            return -self.returncode
        return None


async def _pump(stream: asyncio.StreamReader, handler: LineHandler | None, tail: list[str], activity: list[float],
                tail_size: int = 50) -> None:
    while True:
        try:
            raw = await stream.readline()
        except ValueError:
            # Zeile länger als das Pufferlimit: Rest verwerfen, weiterlesen
            raw = await stream.read(64 * 1024)
        except asyncio.LimitOverrunError:
            raw = await stream.read(64 * 1024)
        if not raw:
            break
        activity[0] = time.monotonic()
        line = raw.decode("utf-8", "replace")
        # ffmpeg trennt Statuszeilen teilweise mit \r
        for part in line.replace("\r", "\n").split("\n"):
            if not part:
                continue
            tail.append(part)
            if len(tail) > tail_size:
                del tail[0]
            if handler is not None:
                r = handler(part)
                if asyncio.iscoroutine(r):
                    await r


async def run_process(
    cmd: list[str],
    control: ScanControl,
    limits: ResourceLimits,
    on_stdout: LineHandler | None = None,
    on_stderr: LineHandler | None = None,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    """Startet ``cmd`` und streamt stdout/stderr zeilenweise an die Handler."""
    if control.cancelled:
        return ProcessResult(returncode=None, cancelled=True)
    await control.wait_running()
    full_cmd = wrap_command(cmd, limits)
    start = time.monotonic()
    proc_env = dict(os.environ)
    proc_env.setdefault("LC_ALL", "C.UTF-8")
    if env:
        proc_env.update(env)
    try:
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=1024 * 1024,
            env=proc_env,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return ProcessResult(returncode=None, spawn_error=f"{cmd[0]}: {exc}")

    control.register(proc)
    result = ProcessResult(returncode=None)
    activity = [time.monotonic()]

    async def watchdog() -> None:
        while proc.returncode is None:
            await asyncio.sleep(5)
            if control.paused:
                activity[0] = time.monotonic()
                continue
            if limits.stall_timeout and time.monotonic() - activity[0] > limits.stall_timeout:
                result.stalled = True
                control._signal(proc, signal.SIGKILL)
                return

    wd = asyncio.create_task(watchdog())
    try:
        assert proc.stdout is not None and proc.stderr is not None
        await asyncio.gather(
            _pump(proc.stdout, on_stdout, result.stdout_tail, activity),
            _pump(proc.stderr, on_stderr, result.stderr_tail, activity),
        )
        result.returncode = await proc.wait()
    except asyncio.CancelledError:
        control._signal(proc, signal.SIGCONT)
        control._signal(proc, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)
        raise
    finally:
        wd.cancel()
        control.unregister(proc)
    result.duration = time.monotonic() - start - control.paused_seconds
    result.cancelled = control.cancelled
    return result
