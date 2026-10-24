from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import media_factory as mf  # noqa: E402


@pytest.fixture(scope="session")
def base_media(tmp_path_factory) -> dict[str, Path]:
    """Einmal erzeugte Grundmedien; Tests kopieren sie in eigene Bibliotheken."""
    d = tmp_path_factory.mktemp("basemedia")
    ok = mf.make_mkv(d / "ok.mkv", 20)
    long = mf.make_mkv(d / "long.mkv", 40, size="320x240")
    return {
        "ok": ok,
        "long": long,
        "ok_mkvmerge": mf.make_mkv_mkvmerge(ok, d / "ok_mkvmerge.mkv"),
        "mp4": mf.make_mp4(ok, d / "ok.mp4"),
        "ts": mf.make_ts(ok, d / "ok.ts"),
        "cluster_header": mf.corrupt_cluster_header(ok, d / "cluster_header.mkv"),
        "payload": mf.corrupt_payload(ok, d / "payload.mkv"),
        "flip": mf.flip_bytes_in_frame(ok, d / "flip.mkv"),
        "truncated": mf.truncate(ok, d / "truncated.mkv"),
        "ebml_header": mf.corrupt_ebml_header(ok, d / "ebml_header.mkv"),
        "unknown_codec": mf.unknown_codec(ok, d / "unknown_codec.mkv"),
        "ts_jump": mf.timestamp_jump(ok, d / "ts_jump.mkv"),
        "ts_damaged": mf.damage_ts(d / "ok.ts", d / "damaged.ts"),
    }


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Isolierte Konfiguration und Medienwurzel je Test."""
    media = tmp_path / "media"
    config = tmp_path / "config"
    media.mkdir()
    config.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(config))
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("AUTO_CREATE_LIBRARY", "true")
    monkeypatch.setenv("NICE_LEVEL", "0")
    monkeypatch.setenv("STATIC_DIR", str(tmp_path / "nostatic"))
    for k in ("AUTH_ENABLED", "AUTH_USERNAME", "AUTH_PASSWORD", "MAX_CONCURRENT_SCANS", "REPAIR_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    from app import config as cfg
    cfg.reset_config()
    from app.services import settings as st
    st.invalidate_cache()
    yield {"media": media, "config": config, "tmp": tmp_path}
    cfg.reset_config()
    st.invalidate_cache()


@pytest.fixture
def client(app_env):
    from fastapi.testclient import TestClient
    from app.main import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c


def wait_job(client, job_id: int, states=("completed", "cancelled", "failed"), timeout: float = 180) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["state"] in states:
            return j
        time.sleep(0.3)
    raise AssertionError(f"Auftrag {job_id} nicht rechtzeitig fertig: {j}")


def files_by_name(client) -> dict[str, dict]:
    r = client.get("/api/files", params={"page_size": 500, "present": "all"}).json()
    return {f["filename"]: f for f in r["items"]}


def running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0
