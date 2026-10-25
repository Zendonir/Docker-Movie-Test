"""Abbruch, Pause/Fortsetzen, Dateiänderung während der Prüfung, gleichzeitige Aufträge, Zeitpläne."""

import os
import signal
import time
from datetime import timedelta

import pytest

from conftest import files_by_name, running_as_root, wait_job
import media_factory as mf


def slow_settings(client, **extra):
    # -readrate 1 => Decodierung in Echtzeit: 40-s-Datei dauert ~40 s
    r = client.put("/api/settings", json={"read_rate_limit": 1.0, "nice_level": 0, **extra})
    assert r.status_code == 200, r.text


def wait_running_item(client, job_id, timeout=30, min_fraction=0.0):
    end = time.time() + timeout
    while time.time() < end:
        live = client.get("/api/scan/live").json()
        for j in live["jobs"]:
            if j["id"] == job_id and j["current"] and j["current"][0]["fraction"] >= min_fraction:
                return j
        time.sleep(0.2)
    raise AssertionError("keine laufende Prüfung")


def ffmpeg_children_states():
    states = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as fh:
                parts = fh.read().split()
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().split(b"\0")
        except OSError:
            continue
        if any(c.endswith(b"ffmpeg") for c in cmd[:6]) and b"-readrate" in cmd:
            states.append(parts[2])
    return states


def test_cancel_is_not_a_scan_error(client, app_env, base_media):
    mf.copy(base_media["long"], app_env["media"] / "lang.mkv")
    slow_settings(client)
    job = client.post("/api/jobs", json={"task": "incremental", "methods": ["full"]}).json()
    wait_running_item(client, job["id"], min_fraction=0.02)
    assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
    j = wait_job(client, job["id"], timeout=30)
    assert j["state"] == "cancelled"
    f = files_by_name(client)["lang.mkv"]
    assert f["status"] == "incomplete", f  # weder "ok" noch "Fehler"
    d = client.get(f"/api/files/{f['id']}").json()
    full = d["current_results"]["full"]
    assert full["outcome"] == "aborted" and full["status"] == "incomplete"
    assert not [e for e in d["errors"] if e["severity"] in ("error", "critical")]
    assert not ffmpeg_children_states()  # kein verwaister FFmpeg-Prozess


def test_manual_skip_continues_with_next_file(client, app_env, base_media):
    mf.copy(base_media["long"], app_env["media"] / "a_lang.mkv")
    mf.copy(base_media["long"], app_env["media"] / "b_lang.mkv")
    slow_settings(client)
    job = client.post("/api/jobs", json={"task": "incremental", "methods": ["full"]}).json()
    item = wait_running_item(client, job["id"], min_fraction=0.02)
    first = item["current"][0]["file_id"]
    r = client.post(f"/api/jobs/{job['id']}/skip", params={"file_id": first})
    assert r.status_code == 200 and r.json()["skipped"] == 1
    j = wait_job(client, job["id"], timeout=120)
    assert j["state"] == "completed" and j["skipped_files"] >= 1 and j["done_files"] >= 1, j
    assert not ffmpeg_children_states()


def test_pause_and_resume_immediately(client, app_env, base_media):
    mf.copy(base_media["long"], app_env["media"] / "lang.mkv")
    slow_settings(client, pause_mode="immediate")
    job = client.post("/api/jobs", json={"task": "incremental", "methods": ["full"]}).json()
    wait_running_item(client, job["id"], min_fraction=0.02)
    assert client.post(f"/api/jobs/{job['id']}/pause").json()["state"] == "paused"
    time.sleep(2.5)
    states = ffmpeg_children_states()
    assert states and all(s == "T" for s in states), states  # per SIGSTOP angehalten
    f1 = client.get("/api/scan/live").json()["jobs"][0]["current"][0]["fraction"]
    time.sleep(2)
    f2 = client.get("/api/scan/live").json()["jobs"][0]["current"][0]["fraction"]
    assert f1 == f2
    assert client.post(f"/api/jobs/{job['id']}/resume").json()["state"] == "queued"
    client.put("/api/settings", json={"read_rate_limit": 0})
    j = wait_job(client, job["id"], timeout=120)
    assert j["state"] == "completed"
    f = files_by_name(client)["lang.mkv"]
    assert f["status"] == "ok" and f["check_level"] == "full"
    events = [e["message"] for e in client.get(f"/api/jobs/{job['id']}").json()["events"]]
    assert any("Pause angefordert" in e for e in events) and any("fortgesetzt" in e for e in events)


def test_pause_after_file(client, app_env, base_media):
    for i in range(3):
        mf.copy(base_media["ok"], app_env["media"] / f"f{i}.mkv")
    client.put("/api/settings", json={"pause_mode": "after_file", "read_rate_limit": 4.0})
    job = client.post("/api/jobs", json={"task": "incremental", "methods": ["full"]}).json()
    wait_running_item(client, job["id"])
    client.post(f"/api/jobs/{job['id']}/pause")
    time.sleep(1.0)
    assert ffmpeg_children_states() and all(s != "T" for s in ffmpeg_children_states())
    end = time.time() + 30
    while time.time() < end and client.get("/api/scan/live").json()["running"]:
        time.sleep(0.3)
    j = client.get(f"/api/jobs/{job['id']}").json()
    assert j["state"] == "paused" and j["done_files"] == 1 and j["item_counts"].get("pending") == 2
    client.put("/api/settings", json={"read_rate_limit": 0})
    client.post(f"/api/jobs/{job['id']}/resume")
    assert wait_job(client, job["id"])["done_files"] == 3


def test_file_modified_during_scan(client, app_env, base_media):
    p = mf.copy(base_media["long"], app_env["media"] / "wachsend.mkv")
    slow_settings(client)
    job = client.post("/api/jobs", json={"task": "incremental", "methods": ["full"]}).json()
    wait_running_item(client, job["id"], min_fraction=0.02)
    with open(p, "ab") as fh:
        fh.write(b"\0" * 4096)
    client.put("/api/settings", json={"read_rate_limit": 0})
    wait_job(client, job["id"], timeout=120)
    f = files_by_name(client)["wachsend.mkv"]
    assert f["status"] == "incomplete"
    assert "verändert" in (f["status_note"] or "")
    d = client.get(f"/api/files/{f['id']}").json()
    assert any(r["outcome"] == "modified" for r in d["history"])
    # neue Dateiversion muss erneut geprüft werden
    j2 = client.post("/api/jobs", json={"task": "incremental", "methods": ["full"]}).json()
    assert wait_job(client, j2["id"])["done_files"] == 1
    assert files_by_name(client)["wachsend.mkv"]["status"] in ("ok", "warning", "error")


def test_concurrent_jobs_and_dedupe(client, app_env, base_media):
    for i in range(4):
        mf.copy(base_media["ok"], app_env["media"] / f"c{i}.mkv")
    client.put("/api/settings", json={"max_concurrent_scans": 2})
    inv = client.post("/api/libraries/1/inventory").json()
    assert inv["files_found"] == 4
    ids = {f["filename"]: f["id"] for f in files_by_name(client).values()}
    a = client.post("/api/jobs", json={"task": "files", "methods": ["quick", "container"], "file_ids": [ids["c0.mkv"], ids["c1.mkv"], ids["c2.mkv"]]})
    b = client.post("/api/jobs", json={"task": "files", "methods": ["quick", "container"], "file_ids": [ids["c2.mkv"], ids["c3.mkv"]], "priority": 5})
    assert a.status_code == 201 and b.status_code == 201
    dup = client.post("/api/jobs", json={"task": "files", "methods": ["container", "quick"], "file_ids": [ids["c3.mkv"], ids["c2.mkv"]]})
    assert dup.status_code == 409 and dup.json()["detail"]["job_id"] == b.json()["id"]
    peak = 0
    end = time.time() + 60
    while time.time() < end:
        live = client.get("/api/scan/live").json()
        peak = max(peak, live["running"])
        files_now = [c["file_id"] for j in live["jobs"] for c in j["current"]]
        assert len(files_now) == len(set(files_now))  # nie dieselbe Datei doppelt
        if not live["jobs"]:
            break
        time.sleep(0.05)
    ja, jb = wait_job(client, a.json()["id"]), wait_job(client, b.json()["id"])
    assert ja["state"] == jb["state"] == "completed"
    assert ja["done_files"] == 3 and jb["done_files"] == 2
    assert peak <= 2
    assert all(f["status"] == "ok" for f in files_by_name(client).values())


def test_schedule_runs_once_and_skips_duplicates(client, app_env, base_media):
    mf.copy(base_media["ok"], app_env["media"] / "s.mkv")
    r = client.post("/api/schedules", json={"name": "Nacht", "kind": "daily", "time": "03:00", "task": "new_changed",
                                            "methods": ["quick"], "library_ids": []})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["cron"] == "0 3 * * *" and r.json()["next_run_at"]
    from app.database import session_scope
    from app.models import ScanJob, ScanSchedule, utcnow
    from app.workers.scheduler import run_due_schedules
    with session_scope() as db:
        db.get(ScanSchedule, sid).next_run_at = utcnow() - timedelta(days=3)  # mehrere verpasste Termine
    created = run_due_schedules()
    assert len(created) == 1
    assert run_due_schedules() == []  # gleicher Termin wird nicht erneut ausgelöst
    with session_scope() as db:
        s = db.get(ScanSchedule, sid)
        assert s.next_run_at > utcnow()
        s.next_run_at = utcnow() - timedelta(minutes=1)
        active = db.get(ScanJob, created[0]).state
    if active in ("queued", "running", "paused"):
        assert run_due_schedules() == []  # identischer Auftrag aktiv -> übersprungen
        with session_scope() as db:
            assert "Übersprungen" in db.get(ScanSchedule, sid).last_message
    wait_job(client, created[0])
    assert client.put(f"/api/schedules/{sid}", json={"name": "x", "kind": "custom", "cron": "99 * * * *", "task": "full",
                                                      "methods": ["quick"]}).status_code == 400


def test_move_rename_and_delete_detection(client, app_env, base_media):
    m = app_env["media"]
    mf.copy(base_media["ok"], m / "alt" / "film.mkv")
    mf.copy(base_media["ok_mkvmerge"], m / "weg.mkv")
    j = client.post("/api/jobs", json={"task": "incremental", "methods": ["quick"]}).json()
    wait_job(client, j["id"])
    old = files_by_name(client)["film.mkv"]
    (m / "neu").mkdir()
    os.rename(m / "alt" / "film.mkv", m / "neu" / "Film umbenannt.mkv")
    (m / "weg.mkv").unlink()
    inv = client.post("/api/libraries/1/inventory").json()
    assert inv["moved"] == 1 and inv["missing"] == 1 and inv["new"] == 0
    f = files_by_name(client)
    moved = f["Film umbenannt.mkv"]
    assert moved["id"] == old["id"] and moved["status"] == "ok" and moved["moved_from"].endswith("alt/film.mkv")
    assert f["weg.mkv"]["present"] is False
    assert client.get("/api/files", params={"present": "yes"}).json()["total"] == 1


def test_excluded_patterns_and_unmounted_safety(client, app_env, base_media):
    m = app_env["media"]
    mf.copy(base_media["ok"], m / "Extras" / "trailer.mkv")
    mf.copy(base_media["ok"], m / "film.mkv")
    client.patch("/api/libraries/1", json={"exclude_patterns": ["Extras"]})
    inv = client.post("/api/libraries/1/inventory").json()
    assert inv["files_found"] == 1 and inv["excluded"] >= 1
    # Leeres Verzeichnis (z. B. Dataset nicht eingehängt): nichts als fehlend markieren
    (m / "film.mkv").unlink()
    inv = client.post("/api/libraries/1/inventory").json()
    assert inv["missing"] == 0 and inv["warnings"]


def test_symlink_escape_is_blocked(client, app_env, base_media, tmp_path):
    outside = mf.copy(base_media["ok"], tmp_path / "outside" / "geheim.mkv")
    m = app_env["media"]
    mf.copy(base_media["ok"], m / "drin.mkv")
    os.symlink(outside, m / "link.mkv")
    os.symlink(m / "drin.mkv", m / "intern.mkv")
    client.put("/api/settings", json={"follow_symlinks": True})
    inv = client.post("/api/libraries/1/inventory").json()
    names = set(files_by_name(client))
    assert "link.mkv" not in names and "intern.mkv" in names and inv["symlinks_skipped"] == 1
    # Bibliothekspfad außerhalb der Medienwurzel wird abgelehnt
    r = client.post("/api/libraries", json={"name": "x", "path": str(tmp_path / "outside")})
    assert r.status_code == 400
    r = client.post("/api/libraries", json={"name": "x", "path": str(m) + "/../outside"})
    assert r.status_code == 400
    r = client.get("/api/libraries/browse", params={"path": "/etc"})
    assert r.status_code == 400


@pytest.mark.skipif(running_as_root(), reason="root ignoriert Dateirechte – Test läuft im Container als Nicht-root")
def test_permission_denied_is_incomplete(client, app_env, base_media):
    p = mf.copy(base_media["ok"], app_env["media"] / "gesperrt.mkv")
    os.chmod(p, 0)
    try:
        j = client.post("/api/jobs", json={"task": "incremental", "methods": ["quick", "container", "full"]}).json()
        wait_job(client, j["id"])
        f = files_by_name(client)["gesperrt.mkv"]
        assert f["status"] == "incomplete"
        d = client.get(f"/api/files/{f['id']}").json()
        assert all(r["outcome"] == "permission_denied" for r in d["current_results"].values())
    finally:
        os.chmod(p, 0o644)


def test_permission_denied_simulated(client, app_env, base_media, monkeypatch):
    """Wie oben, aber unabhängig vom Benutzer: os.open liefert EACCES."""
    p = mf.copy(base_media["ok"], app_env["media"] / "gesperrt.mkv")
    real_open = os.open

    def fake_open(path, *a, **kw):
        if str(path) == str(p):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **kw)

    from app.services import scan_service
    monkeypatch.setattr(scan_service.os, "open", fake_open)
    j = client.post("/api/jobs", json={"task": "incremental", "methods": ["quick", "full"]}).json()
    wait_job(client, j["id"])
    f = files_by_name(client)["gesperrt.mkv"]
    assert f["status"] == "incomplete"


def test_auth(client, app_env):
    assert client.get("/api/stats").status_code == 200
    r = client.post("/api/settings/auth", json={"username": "admin", "password": "geheim123", "enable": True})
    assert r.status_code == 200
    client.cookies.clear()
    assert client.get("/api/stats").status_code == 401
    assert client.get("/health").status_code == 200
    assert client.post("/api/auth/login", json={"username": "admin", "password": "falsch!!"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "geheim123"}).status_code == 200
    assert client.get("/api/stats").status_code == 200


def test_api_input_validation(client):
    assert client.post("/api/jobs", json={"task": "rm -rf /", "methods": ["full"]}).status_code == 422
    assert client.post("/api/jobs", json={"task": "full", "methods": []}).status_code == 422
    assert client.post("/api/jobs", json={"task": "files", "methods": ["full"], "file_ids": [999999]}).status_code == 400
    assert client.put("/api/settings", json={"max_concurrent_scans": 0}).status_code == 422
    assert client.put("/api/settings", json={"evil": 1}).status_code == 400
    assert client.put("/api/settings", json={"auth_password_hash": "x"}).status_code == 400
    assert client.get("/api/export", params={"format": "exe"}).status_code == 422
    assert client.get("/api/files/1/results/1/log").status_code == 404
    assert client.post("/api/files/1/repair", json={"confirm": True}).status_code in (403, 404)


def test_repair_disabled_by_default(client, app_env, base_media):
    mf.copy(base_media["ok"], app_env["media"] / "r.mkv")
    client.post("/api/libraries/1/inventory")
    fid = next(iter(files_by_name(client).values()))["id"]
    r = client.post(f"/api/files/{fid}/repair", json={"action": "remux", "confirm": True, "confirm_filename": "r.mkv"})
    assert r.status_code == 403
