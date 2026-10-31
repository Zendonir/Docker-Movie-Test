from datetime import datetime, timezone

import pytest

from app.scanners.base import Finding, Method, MethodResult, Outcome, Severity
from app.services.cron import Cron, CronError, cron_for
from app.services.exports import _csv_val


def test_cron_next_runs():
    utc = timezone.utc
    assert Cron("30 3 * * 1").next_after(datetime(2026, 10, 24, 12, 0, tzinfo=utc)).isoformat() == "2026-10-26T03:30:00+01:00"
    assert Cron("*/15 * * * *").next_after(datetime(2026, 10, 24, 12, 7, tzinfo=utc)).minute == 15
    assert Cron("0 2 1 * *").next_after(datetime(2026, 10, 24, 12, 7, tzinfo=utc)).day == 1
    assert cron_for("weekly", "04:15", weekday=3) == "15 4 * * 3"
    assert cron_for("monthly", "01:00", day=28) == "0 1 28 * *"
    for bad in ("* * *", "61 * * * *", "a b c d e"):
        with pytest.raises(CronError):
            Cron(bad)


def test_ok_requires_completed_run():
    r = MethodResult(Method.FULL, Outcome.ABORTED, [])
    assert r.status() == "incomplete"
    r = MethodResult(Method.FULL, Outcome.COMPLETED, [])
    assert r.status() == "ok"
    r = MethodResult(Method.FULL, Outcome.PARTIAL, [Finding("unsupported_codec", Severity.INCOMPLETE, "x", "t")])
    assert r.status() == "incomplete"
    # Erkannte Defekte bleiben auch bei abgebrochener Prüfung sichtbar
    r = MethodResult(Method.FULL, Outcome.ABORTED, [Finding("corrupt_frame", Severity.ERROR, "x", "t")])
    assert r.status() == "error"


def test_finding_without_time_has_no_position():
    f = Finding("x", Severity.ERROR, "d", "t", time_end=5.0, time_precision="exact")
    assert f.time_precision.value == "unknown" and f.time_end is None


def test_csv_formula_injection_guard():
    assert _csv_val("=cmd|' /C calc'!A0") == "'=cmd|' /C calc'!A0"
    assert _csv_val("-12") == "-12"
    assert _csv_val("Größe Ü") == "Größe Ü"


def test_library_path_validation(app_env, tmp_path):
    from app.services.pathsec import PathSecurityError, validate_library_path
    (app_env["media"] / "Filme").mkdir()
    assert validate_library_path(str(app_env["media"] / "Filme")).endswith("Filme")
    for bad in ("relative/path", "/etc", str(app_env["media"] / ".." / "config"), str(app_env["media"] / "fehlt")):
        with pytest.raises(PathSecurityError):
            validate_library_path(bad)


async def test_stall_detected_despite_output():
    """Ein Werkzeug, das ständig Fortschrittszeilen schreibt, aber nicht vorankommt, gilt als hängend."""
    import sys
    import time as _t

    from app.scanners.process import ResourceLimits, ScanControl, run_process

    script = "import time\nwhile True:\n    print('progress=continue out_time_us=5000000', flush=True)\n    time.sleep(0.2)\n"
    value = [5.0]
    t0 = _t.monotonic()
    res = await run_process([sys.executable, "-c", script], ScanControl(), ResourceLimits(io_class="none", stall_timeout=1),
                            progress_value=lambda: value[0])
    assert res.stalled and _t.monotonic() - t0 < 30


async def test_advancing_progress_is_not_a_stall():
    import sys

    from app.scanners.process import ResourceLimits, ScanControl, run_process

    script = "import time\nfor i in range(40):\n    print(i, flush=True)\n    time.sleep(0.25)\n"
    pos = [0.0]
    res = await run_process([sys.executable, "-c", script], ScanControl(), ResourceLimits(io_class="none", stall_timeout=3),
                            on_stdout=lambda line: pos.__setitem__(0, float(line)), progress_value=lambda: pos[0])
    assert not res.stalled and res.returncode == 0


async def test_very_long_output_line_is_delivered_whole():
    """Die Ergebniszeile des EBML-Prüfers enthält bei großen Filmen die Clusterliste (> 1 MB)."""
    import json
    import sys
    from app.scanners.process import ResourceLimits, ScanControl, run_process
    lines: list[str] = []
    code = "import json; print('a'); print(json.dumps({'t': 'summary', 'x': 'y' * 5_000_000})); print('b')"
    res = await run_process([sys.executable, "-c", code], ScanControl(), ResourceLimits(), on_stdout=lines.append)
    assert res.returncode == 0
    assert lines[0] == "a" and lines[-1] == "b" and len(lines) == 3
    assert len(json.loads(lines[1])["x"]) == 5_000_000
