"""Abnahmekriterien A (1.000 Dateien, unverändert) und D (Neustart mitten im Scan)."""

import hashlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import media_factory as mf

BACKEND = Path(__file__).resolve().parent.parent / "backend"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(env: dict, port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
        cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    end = time.time() + 30
    while time.time() < end:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    proc.kill()
    raise AssertionError("Server startet nicht")


def kill_group(proc: subprocess.Popen, sig=signal.SIGKILL) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
    proc.wait(timeout=30)


@pytest.mark.slow
def test_resume_after_hard_restart(tmp_path, base_media):
    media, config = tmp_path / "media", tmp_path / "config"
    for i in range(3):
        mf.copy(base_media["long"], media / f"film{i}.mkv")
    env = dict(os.environ, CONFIG_DIR=str(config), MEDIA_ROOT=str(media), STATIC_DIR=str(tmp_path / "x"),
               NICE_LEVEL="0", PYTHONPATH=str(BACKEND))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = start_server(env, port)
    try:
        httpx.put(f"{base}/api/settings", json={"read_rate_limit": 8.0})  # 40 s Film ≈ 5 s
        job = httpx.post(f"{base}/api/jobs", json={"task": "incremental", "methods": ["container", "full"]}).json()
        end = time.time() + 60
        while time.time() < end:
            j = httpx.get(f"{base}/api/jobs/{job['id']}").json()
            live = httpx.get(f"{base}/api/scan/live").json()
            if j["done_files"] >= 1 and live["running"] >= 1:
                break
            time.sleep(0.3)
        assert j["done_files"] >= 1
    finally:
        kill_group(proc)  # wie Stromausfall / docker kill: kein geordnetes Herunterfahren

    proc = start_server(env, port)
    try:
        httpx.put(f"{base}/api/settings", json={"read_rate_limit": 0})
        end = time.time() + 120
        while time.time() < end:
            j = httpx.get(f"{base}/api/jobs/{job['id']}").json()
            if j["state"] == "completed":
                break
            time.sleep(0.5)
        assert j["state"] == "completed", j
        assert j["resume_count"] >= 1 and j["done_files"] == 3
        msgs = [e["message"] for e in j["events"]]
        assert any("Neustart erkannt" in m for m in msgs)
        assert any("durch einen Neustart unterbrochen" in m for m in msgs)
        files = httpx.get(f"{base}/api/files").json()["items"]
        assert len(files) == 3 and all(f["status"] == "ok" and f["check_level"] == "container_full" for f in files)
        # Kein doppelter Auftrag nach dem Neustart
        assert httpx.get(f"{base}/api/jobs").json()["total"] == 1
    finally:
        kill_group(proc, signal.SIGTERM)


@pytest.mark.slow
def test_graceful_shutdown_keeps_job_resumable(tmp_path, base_media):
    media, config = tmp_path / "media", tmp_path / "config"
    mf.copy(base_media["long"], media / "a.mkv")
    env = dict(os.environ, CONFIG_DIR=str(config), MEDIA_ROOT=str(media), STATIC_DIR=str(tmp_path / "x"),
               NICE_LEVEL="0", PYTHONPATH=str(BACKEND))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = start_server(env, port)
    try:
        httpx.put(f"{base}/api/settings", json={"read_rate_limit": 1.0})
        job = httpx.post(f"{base}/api/jobs", json={"task": "incremental", "methods": ["full"]}).json()
        end = time.time() + 30
        while time.time() < end and not httpx.get(f"{base}/api/scan/live").json()["running"]:
            time.sleep(0.3)
    finally:
        kill_group(proc, signal.SIGTERM)  # docker stop
    proc = start_server(env, port)
    try:
        httpx.put(f"{base}/api/settings", json={"read_rate_limit": 0})
        end = time.time() + 90
        while time.time() < end:
            j = httpx.get(f"{base}/api/jobs/{job['id']}").json()
            if j["state"] == "completed":
                break
            time.sleep(0.5)
        assert j["state"] == "completed" and j["done_files"] == 1
        f = httpx.get(f"{base}/api/files").json()["items"][0]
        assert f["status"] == "ok"
        d = httpx.get(f"{base}/api/files/{f['id']}").json()
        assert any(r["outcome"] == "interrupted" for r in d["history"])  # nachvollziehbar protokolliert
    finally:
        kill_group(proc, signal.SIGTERM)


@pytest.mark.slow
def test_1000_mkv_files_inventory_and_quick_scan(client, app_env, tmp_path):
    src = mf.make_mkv(tmp_path / "klein.mkv", 2, size="160x120")
    media = app_env["media"]
    for i in range(1000):
        d = media / f"Ordner {i // 100:02d}"
        d.mkdir(exist_ok=True)
        os.link(src, d / f"Film Nr. {i:04d} – Ä.mkv") if i % 2 else mf.copy(src, d / f"Film Nr. {i:04d} – Ä.mkv")
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in media.rglob("*.mkv")}
    digest = hashlib.sha256(src.read_bytes()).hexdigest()

    t0 = time.time()
    inv = client.post("/api/libraries/1/inventory").json()
    t_inv = time.time() - t0
    assert inv["files_found"] == 1000 and inv["new"] == 1000
    assert t_inv < 60, t_inv

    client.put("/api/settings", json={"max_concurrent_scans": 4})
    job = client.post("/api/jobs", json={"task": "incremental", "methods": ["quick"]}).json()
    end = time.time() + 900
    while time.time() < end:
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["state"] == "completed":
            break
        time.sleep(1)
    assert j["state"] == "completed" and j["done_files"] == 1000, {k: j[k] for k in ("done_files", "skipped_files", "failed_files", "total_files", "item_counts", "events") if k in j}
    s = client.get("/api/stats").json()
    assert s["total_files"] == 1000 and s["status"]["ok"] == 1000

    # Paginierung/Filter/Sortierung mit vielen Dateien
    r = client.get("/api/files", params={"q": "nr. 0999", "page_size": 10}).json()
    assert r["total"] == 1
    r = client.get("/api/files", params={"sort": "name", "order": "desc", "page": 20, "page_size": 50}).json()
    assert len(r["items"]) == 50

    # Originale unverändert (Zeitstempel, Größe, Inhalt)
    after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in media.rglob("*.mkv")}
    assert before == after
    for p in list(media.rglob("*.mkv"))[::97]:
        assert hashlib.sha256(p.read_bytes()).hexdigest() == digest
