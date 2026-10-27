"""MKV-Optimizer: Regeln, Remux, Audiokonvertierung, Dateioperationen, Sicherheit."""

import hashlib
import os
import time

import pytest

from conftest import files_by_name
import media_factory as mf


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def multi_src(tmp_path_factory):
    return mf.make_multi_mkv(tmp_path_factory.mktemp("multi") / "multi.mkv")


@pytest.fixture
def opt_env(app_env, monkeypatch):
    out = app_env["tmp"] / "output"
    out.mkdir()
    monkeypatch.setenv("OPTIMIZER_OUTPUT_DIR", str(out))
    from app import config
    config.reset_config()
    return {**app_env, "output": out}


@pytest.fixture
def oclient(opt_env):
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c


def identify_sync(path):
    import asyncio
    from app.optimizer.tracks import identify
    return asyncio.run(identify(str(path)))


def sig(path):
    a = identify_sync(path)
    return [(t.type, t.codec_family, t.language) for t in a.tracks], a


def wait_opt_job(client, job_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/optimizer/jobs/{job_id}").json()
        if j["state"] in ("completed", "cancelled", "failed"):
            return j
        time.sleep(0.3)
    raise AssertionError(j)


def analyze(client, directory=""):
    r = client.post("/api/optimizer/analyze", json={"library_id": 1, "directory": directory, "recursive": True})
    assert r.status_code == 202, r.text
    end = time.time() + 60
    while time.time() < end:
        st = client.get("/api/optimizer/analyze").json()
        if not st["running"]:
            return st
        time.sleep(0.2)
    raise AssertionError("Analyse hängt")


RULES_DTS = {"audio": {"mode": "language", "languages": ["de"], "codecs": ["DTS"]},
             "subtitles": {"mode": "language", "languages": ["ger"]}}


# ---------------------------------------------------------------- reine Logik
def test_plan_language_codec_filter_and_default(multi_src):
    from app.optimizer.rules import FileOps, Rules, compute_plan, mkvmerge_args
    _, a = sig(multi_src)
    assert [t.codec_family for t in a.audio] == ["AC3", "DTS", "AAC"]
    plan = compute_plan(a, Rules(**RULES_DTS), FileOps(protected_languages=["ger"]))
    kept = {d.track_id for d in plan.decisions if d.keep}
    assert kept == {0, 2, 4}  # Video, GER DTS, GER-Untertitel
    assert plan.default_track == 2  # Standardspur (AC3) entfernt -> DTS wird Standard
    args = mkvmerge_args(plan, a, FileOps())
    assert args[:4] == ["--audio-tracks", "2", "--subtitle-tracks", "4"]


def test_plan_never_removes_all_audio(multi_src):
    from app.optimizer.rules import FileOps, Rules, compute_plan
    _, a = sig(multi_src)
    plan = compute_plan(a, Rules(audio={"languages": ["fre"]}, subtitles={"languages": []}),
                        FileOps(protected_languages=[]))
    assert plan.skip_reason and "keine Audiospur" in plan.skip_reason


def test_protected_language_survives_track_id_mode(multi_src):
    from app.optimizer.rules import FileOps, Rules, compute_plan
    _, a = sig(multi_src)
    plan = compute_plan(a, Rules(audio={"mode": "track_id", "track_ids": [3]}, subtitles={"languages": ["ger"]}),
                        FileOps(protected_languages=["ger"]))
    assert {d.track_id for d in plan.decisions if d.keep and d.type == "audio"} == {1, 2, 3}
    assert plan.warnings


def test_codec_filter_without_match_keeps_language(multi_src):
    from app.optimizer.rules import FileOps, Rules, compute_plan
    _, a = sig(multi_src)
    plan = compute_plan(a, Rules(audio={"languages": ["ger"], "codecs": ["FLAC"]}, subtitles={"languages": ["ger"]}),
                        FileOps(protected_languages=[]))
    assert {d.track_id for d in plan.decisions if d.keep and d.type == "audio"} == {1, 2}
    assert any("Codecfilter" in w for w in plan.warnings)


def test_nothing_to_do(multi_src):
    from app.optimizer.rules import FileOps, Rules, compute_plan
    _, a = sig(multi_src)
    plan = compute_plan(a, Rules(audio={"languages": ["ger", "eng"]}, subtitles={"languages": ["ger", "eng"]}),
                        FileOps())
    assert not plan.changed and plan.skip_reason == "Keine Spur zu entfernen"


# ---------------------------------------------------------------- Ende-zu-Ende
def test_remux_to_output_dir_keeps_original(oclient, opt_env, multi_src):
    src = mf.copy(multi_src, opt_env["media"] / "Filme" / "Der Film – Ä.mkv")
    mf.copy(multi_src, opt_env["media"] / "Filme" / "ohne Änderung.mkv")
    before = sha(src)
    st = analyze(oclient)
    assert st["analyzed"] == 2 and st["errors"] == 0
    pv = oclient.post("/api/optimizer/preview", json={"library_id": 1, "kind": "remux", "rules": RULES_DTS}).json()
    assert pv["total"] == 2 and len(pv["changed_ids"]) == 2
    langs = {c["value"] for c in pv["choices"]["audio_languages"]}
    assert langs == {"ger", "eng"}
    fid = files_by_name(oclient)["Der Film – Ä.mkv"]["id"]
    cmd = oclient.post("/api/optimizer/command", json={"file_id": fid, "kind": "remux", "rules": RULES_DTS}).json()
    assert "--audio-tracks 2" in cmd["commands"][0]

    job = oclient.post("/api/optimizer/jobs", json={"kind": "remux", "file_ids": [fid], "rules": RULES_DTS})
    assert job.status_code == 201, job.text
    j = wait_opt_job(oclient, job.json()["id"])
    assert j["state"] == "completed" and j["done_files"] == 1, j
    out = opt_env["output"] / "Filme" / "Der Film – Ä.mkv"
    assert out.exists()
    s, a = sig(out)
    assert [(x[0], x[2]) for x in s] == [("video", "und"), ("audio", "ger"), ("subtitles", "ger")]
    assert s[1][1] == "DTS" and s[2][1] == "SRT"
    assert a.audio[0].is_default
    assert sha(src) == before  # Original unverändert
    assert not [p for p in out.parent.iterdir() if ".mis-tmp-" in p.name]
    items = oclient.get(f"/api/optimizer/jobs/{j['id']}/items").json()["items"]
    detail = oclient.get(f"/api/optimizer/items/{items[0]['id']}").json()
    assert "mkvmerge" in detail["log"] and "Strukturprüfung der neuen Datei: ohne Fehler" in detail["log"]
    # Regeln werden gemerkt
    assert oclient.get("/api/optimizer/status").json()["last_rules"]["audio"]["codecs"] == ["DTS"]


def test_replace_requires_explicit_permission(oclient, opt_env, multi_src):
    src = mf.copy(multi_src, opt_env["media"] / "f.mkv")
    analyze(oclient)
    fid = files_by_name(oclient)["f.mkv"]["id"]
    oclient.put("/api/settings", json={"optimizer_output_mode": "replace_backup"})
    r = oclient.post("/api/optimizer/jobs", json={"kind": "remux", "file_ids": [fid], "rules": RULES_DTS})
    assert r.status_code == 400 and "nicht freigegeben" in r.json()["detail"]
    oclient.put("/api/settings", json={"optimizer_allow_modify_originals": True})
    before = sha(src)
    r = oclient.post("/api/optimizer/jobs", json={"kind": "remux", "file_ids": [fid], "rules": RULES_DTS})
    j = wait_opt_job(oclient, r.json()["id"])
    assert j["state"] == "completed" and j["done_files"] == 1, j
    backup = opt_env["media"] / ".mis-originale" / "f.mkv"
    assert backup.exists() and sha(backup) == before  # Original 1:1 gesichert
    assert len(identify_sync(src).audio) == 1
    f = files_by_name(oclient)["f.mkv"]
    assert f["status"] == "unchecked" and "MKV-Optimizer" in (f["status_note"] or "")
    assert "f.mkv" in files_by_name(oclient) and len(files_by_name(oclient)) == 1  # Sicherung nicht in der Bibliothek


def test_replace_delete(oclient, opt_env, multi_src):
    src = mf.copy(multi_src, opt_env["media"] / "g.mkv")
    oclient.put("/api/settings", json={"optimizer_output_mode": "replace_delete", "optimizer_allow_modify_originals": True})
    analyze(oclient)
    fid = files_by_name(oclient)["g.mkv"]["id"]
    j = wait_opt_job(oclient, oclient.post("/api/optimizer/jobs", json={"kind": "remux", "file_ids": [fid],
                                                                        "rules": RULES_DTS}).json()["id"])
    assert j["state"] == "completed"
    assert len(identify_sync(src).audio) == 1 and not (opt_env["media"] / ".mis-originale").exists()
    assert j["saved_bytes"] > 0


def test_beside_mode_with_suffix(oclient, opt_env, multi_src):
    src = mf.copy(multi_src, opt_env["media"] / "h.mkv")
    before = sha(src)
    oclient.put("/api/settings", json={"optimizer_output_mode": "beside", "optimizer_suffix": ".nur-deutsch"})
    analyze(oclient)
    fid = files_by_name(oclient)["h.mkv"]["id"]
    j = wait_opt_job(oclient, oclient.post("/api/optimizer/jobs", json={"kind": "remux", "file_ids": [fid],
                                                                        "rules": RULES_DTS}).json()["id"])
    assert j["state"] == "completed"
    assert (opt_env["media"] / "h.nur-deutsch.mkv").exists() and sha(src) == before
    # neue Datei wurde nach Abschluss in die Bibliothek aufgenommen
    assert "h.nur-deutsch.mkv" in files_by_name(oclient)


def test_audio_conversion_replace_and_add(oclient, opt_env, multi_src):
    mf.copy(multi_src, opt_env["media"] / "a.mkv")
    analyze(oclient)
    profiles = oclient.get("/api/optimizer/profiles").json()
    rep = next(p for p in profiles if p["builtin"] and p["mode"] == "replace" and p["codec"] == "ac3"
               and "DTS" in p["source_codecs"])
    add = next(p for p in profiles if p["builtin"] and p["mode"] == "add" and p["codec"] == "ac3")
    fid = files_by_name(oclient)["a.mkv"]["id"]
    pv = oclient.post("/api/optimizer/preview", json={"library_id": 1, "kind": "audio", "profile_id": rep["id"]}).json()
    assert pv["changed_ids"] == [fid]
    assert [c["track_id"] for c in pv["items"][0]["plan"]["conversions"]] == [2]

    j = wait_opt_job(oclient, oclient.post("/api/optimizer/jobs", json={"kind": "audio", "file_ids": [fid],
                                                                        "profile_id": rep["id"]}).json()["id"])
    assert j["state"] == "completed", oclient.get(f"/api/optimizer/jobs/{j['id']}").json()
    out = opt_env["output"] / "a.mkv"
    s, a = sig(out)
    assert [x[1] for x in s if x[0] == "audio"] == ["AC3", "AC3", "AAC"]  # DTS an gleicher Stelle durch AC3 ersetzt
    assert a.audio[1].language == "ger" and a.audio[1].channels == 6 and a.audio[1].name == "AC3 5.1"
    assert len(a.subtitles) == 2

    # Profil mit eigenem Dateinamensmuster, Modus "zusätzlich"
    custom = oclient.post("/api/optimizer/profiles", json={**{k: add[k] for k in (
        "codec", "bitrate_kbps", "max_channels", "source_codecs", "mode", "track_name")}, "name": "Test zusätzlich",
        "filename_pattern": "{name} [{codec}]"})
    assert custom.status_code == 201, custom.text
    j = wait_opt_job(oclient, oclient.post("/api/optimizer/jobs", json={"kind": "audio", "file_ids": [fid],
                                                                        "profile_id": custom.json()["id"]}).json()["id"])
    assert j["state"] == "completed"
    s, a = sig(opt_env["output"] / "a [AC3].mkv")
    assert [x[1] for x in s if x[0] == "audio"] == ["AC3", "DTS", "AC3", "AAC"]
    assert not a.audio[2].is_default
    # vordefinierte Profile sind schreibgeschützt; ungültige Muster werden abgelehnt
    assert oclient.delete(f"/api/optimizer/profiles/{rep['id']}").status_code == 400
    bad = oclient.post("/api/optimizer/profiles", json={"name": "x", "codec": "ac3", "filename_pattern": "../{name}"})
    assert bad.status_code == 422


def test_verification_failure_keeps_original(oclient, opt_env, multi_src, monkeypatch):
    src = mf.copy(multi_src, opt_env["media"] / "v.mkv")
    oclient.put("/api/settings", json={"optimizer_output_mode": "replace_delete", "optimizer_allow_modify_originals": True})
    analyze(oclient)
    before = sha(src)
    from app.optimizer import runner
    from app.optimizer.tracks import Analysis
    real_identify = runner.identify

    async def broken(path, *a, **kw):
        if ".mis-tmp-" in str(path):
            return Analysis(True, None, 1.0, None, [])  # simuliert eine kaputte Ausgabedatei
        return await real_identify(path, *a, **kw)

    monkeypatch.setattr(runner, "identify", broken)
    fid = files_by_name(oclient)["v.mkv"]["id"]
    j = wait_opt_job(oclient, oclient.post("/api/optimizer/jobs", json={"kind": "remux", "file_ids": [fid],
                                                                        "rules": RULES_DTS}).json()["id"])
    assert j["failed_files"] == 1
    assert sha(src) == before
    assert not [p for p in opt_env["media"].iterdir() if ".mis-tmp-" in p.name]


def test_damaged_files_are_skipped(oclient, opt_env, multi_src, base_media):
    mf.copy(base_media["truncated"], opt_env["media"] / "kaputt.mkv")
    j = oclient.post("/api/jobs", json={"task": "incremental", "methods": ["container"]}).json()
    from conftest import wait_job
    wait_job(oclient, j["id"])
    analyze(oclient)
    fid = files_by_name(oclient)["kaputt.mkv"]["id"]
    j = wait_opt_job(oclient, oclient.post("/api/optimizer/jobs", json={"kind": "remux", "file_ids": [fid],
                                                                        "rules": RULES_DTS}).json()["id"])
    assert j["skipped_files"] == 1


def test_unavailable_output_and_conflicts(oclient, opt_env, multi_src):
    mf.copy(multi_src, opt_env["media"] / "c.mkv")
    analyze(oclient)
    fid = files_by_name(oclient)["c.mkv"]["id"]
    os.rmdir(opt_env["output"])
    r = oclient.post("/api/optimizer/jobs", json={"kind": "remux", "file_ids": [fid], "rules": RULES_DTS})
    assert r.status_code == 400 and "Ausgabeverzeichnis" in r.json()["detail"]
    opt_env["output"].mkdir()
    oclient.put("/api/settings", json={"scanner_paused": True})  # Auftrag bleibt wartend
    r1 = oclient.post("/api/optimizer/jobs", json={"kind": "remux", "file_ids": [fid], "rules": RULES_DTS})
    r2 = oclient.post("/api/optimizer/jobs", json={"kind": "remux", "file_ids": [fid], "rules": RULES_DTS})
    assert r1.status_code == 201 and r2.status_code == 409
    assert oclient.post(f"/api/optimizer/jobs/{r1.json()['id']}/cancel").status_code == 200
    j = wait_opt_job(oclient, r1.json()["id"])
    assert j["state"] == "cancelled"
    assert oclient.put("/api/settings", json={"optimizer_suffix": "../x"}).status_code == 422
    assert oclient.put("/api/settings", json={"optimizer_backup_dir": "../raus"}).status_code == 422


def test_restart_recovery_cleans_temp_files(opt_env, multi_src):
    from app.database import init_engine, session_scope
    from app.database.migrate import upgrade
    from app.config import get_config
    from app.models import Library, MediaFile, OptimizerItem, OptimizerJob
    from app.optimizer.worker import recover_after_restart
    cfg = get_config()
    upgrade(cfg.db_path)
    init_engine(cfg.db_path)
    tmp = opt_env["output"] / ".x.mis-tmp-1.mkv"
    tmp.write_bytes(b"halb fertig")
    with session_scope() as db:
        lib = Library(name="L", path=str(opt_env["media"]))
        db.add(lib)
        db.flush()
        f = MediaFile(library_id=lib.id, rel_path="x.mkv", path=str(opt_env["media"] / "x.mkv"), filename="x.mkv",
                      name_lower="x.mkv", extension="mkv", size=1, mtime_ns=1)
        db.add(f)
        db.flush()
        job = OptimizerJob(kind="remux", name="t", state="running", params={}, events=[])
        db.add(job)
        db.flush()
        db.add(OptimizerItem(job_id=job.id, file_id=f.id, state="running", source_path=f.path, temp_paths=[str(tmp)]))
    recover_after_restart()
    assert not tmp.exists()
    with session_scope() as db:
        job = db.query(OptimizerJob).one()
        assert job.state == "queued" and job.resume_count == 1
        assert db.query(OptimizerItem).one().state == "pending"
        assert any("Neustart" in e["message"] for e in job.events)


def _slow_encoder(monkeypatch):
    from app.optimizer import profiles, runner
    real = profiles.ffmpeg_encode_cmd

    def slow(*a, **kw):
        cmd = real(*a, **kw)
        i = cmd.index("-i")
        return cmd[:i] + ["-readrate", "0.5"] + cmd[i:]  # 8-s-Datei braucht ~16 s

    monkeypatch.setattr(runner, "ffmpeg_encode_cmd", slow)


def _wait_running(client, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        live = client.get("/api/optimizer/live").json()["jobs"]
        if live and live[0]["current"] and live[0]["current"]["fraction"] > 0.03:
            return live[0]
        time.sleep(0.2)
    raise AssertionError("keine laufende Bearbeitung")


def test_cancel_running_conversion_keeps_original(oclient, opt_env, multi_src, monkeypatch):
    _slow_encoder(monkeypatch)
    src = mf.copy(multi_src, opt_env["media"] / "lang.mkv")
    oclient.put("/api/settings", json={"optimizer_output_mode": "replace_delete", "optimizer_allow_modify_originals": True})
    analyze(oclient)
    before = sha(src)
    prof = next(p for p in oclient.get("/api/optimizer/profiles").json() if p["mode"] == "replace" and p["codec"] == "ac3")
    fid = files_by_name(oclient)["lang.mkv"]["id"]
    job = oclient.post("/api/optimizer/jobs", json={"kind": "audio", "file_ids": [fid], "profile_id": prof["id"]}).json()
    live = _wait_running(oclient)
    assert live["current"]["phase"].startswith("Codierung") and live["current"]["eta_seconds"] is not None
    assert oclient.post(f"/api/optimizer/jobs/{job['id']}/cancel").status_code == 200
    j = wait_opt_job(oclient, job["id"], timeout=30)
    assert j["state"] == "cancelled"
    items = oclient.get(f"/api/optimizer/jobs/{job['id']}/items").json()["items"]
    assert items[0]["state"] == "cancelled"
    assert sha(src) == before
    assert not [p for p in opt_env["media"].iterdir() if ".mis-tmp-" in p.name]


def test_pause_and_resume_conversion(oclient, opt_env, multi_src, monkeypatch):
    from test_job_control import ffmpeg_children_states
    _slow_encoder(monkeypatch)
    mf.copy(multi_src, opt_env["media"] / "p.mkv")
    analyze(oclient)
    prof = next(p for p in oclient.get("/api/optimizer/profiles").json() if p["mode"] == "replace" and p["codec"] == "ac3")
    fid = files_by_name(oclient)["p.mkv"]["id"]
    job = oclient.post("/api/optimizer/jobs", json={"kind": "audio", "file_ids": [fid], "profile_id": prof["id"]}).json()
    _wait_running(oclient)
    oclient.post(f"/api/optimizer/jobs/{job['id']}/pause")
    time.sleep(2.5)
    states = ffmpeg_children_states()
    assert states and all(s == "T" for s in states), states
    f1 = oclient.get("/api/optimizer/live").json()["jobs"][0]["current"]["fraction"]
    time.sleep(1.5)
    assert oclient.get("/api/optimizer/live").json()["jobs"][0]["current"]["fraction"] == f1
    oclient.post(f"/api/optimizer/jobs/{job['id']}/resume")
    j = wait_opt_job(oclient, job["id"], timeout=60)
    assert j["state"] == "completed" and j["done_files"] == 1
    assert (opt_env["output"] / "p.mkv").exists()


# ---------------------------------------------------------------- Vorhersage der Ersparnis
def test_size_sources(multi_src, tmp_path):
    _, a = sig(multi_src)  # mit FFmpeg erzeugt: keine Statistik-Tags
    by = {t.track_id: t for t in a.tracks}
    assert by[1].size_source == "bitrate" and by[2].size_source == "bitrate"  # AC3/DTS: konstante Bitrate
    assert by[3].size_bytes is None  # AAC ohne Tags (variable Bitrate): nicht geschätzt
    mm = mf.make_mkv_mkvmerge(multi_src, tmp_path / "mm.mkv")
    _, a2 = sig(mm)  # mkvmerge schreibt Statistik-Tags
    assert all(t.size_source == "tags" and t.size_bytes is not None for t in a2.tracks)


def test_prediction_partial_when_sizes_unknown(multi_src):
    from app.optimizer.rules import FileOps, Rules, compute_plan
    _, a = sig(multi_src)
    plan = compute_plan(a, Rules(**RULES_DTS), FileOps())
    ac3 = next(t for t in a.audio if t.codec_family == "AC3")
    assert plan.predicted_saved == ac3.size_bytes  # AC3 bekannt, AAC/SRT unbekannt
    assert plan.predicted_unknown == 2 and not plan.predicted_exact


def test_prediction_matches_actual_saving(oclient, opt_env, multi_src, tmp_path):
    src = mf.make_mkv_mkvmerge(multi_src, opt_env["media"] / "tags.mkv")
    analyze(oclient)
    pv = oclient.post("/api/optimizer/preview", json={"library_id": 1, "kind": "remux", "rules": RULES_DTS}).json()
    item = pv["items"][0]
    assert item["plan"]["predicted_exact"] and item["plan"]["predicted_unknown"] == 0
    predicted = item["plan"]["predicted_saved"]
    assert pv["prediction"]["saved"] == predicted and pv["predictions"][str(item["file_id"])] == predicted
    job = oclient.post("/api/optimizer/jobs", json={"kind": "remux", "file_ids": [item["file_id"]], "rules": RULES_DTS}).json()
    assert job["predicted_saved"] == predicted
    j = wait_opt_job(oclient, job["id"])
    actual = src.stat().st_size - (opt_env["output"] / "tags.mkv").stat().st_size
    # Abweichung nur durch Container-Overhead (Blockköpfe, Index)
    assert abs(actual - predicted) <= max(0.03 * predicted, 64 * 1024), (actual, predicted)
    assert j["saved_bytes"] == actual
    it = oclient.get(f"/api/optimizer/jobs/{job['id']}/items").json()["items"][0]
    assert it["predicted_saved"] == predicted


def test_audio_prediction(oclient, opt_env, multi_src):
    mf.copy(multi_src, opt_env["media"] / "a.mkv")
    analyze(oclient)
    profiles = oclient.get("/api/optimizer/profiles").json()
    rep = next(p for p in profiles if p["builtin"] and p["mode"] == "replace" and p["codec"] == "ac3" and "DTS" in p["source_codecs"])
    add = next(p for p in profiles if p["builtin"] and p["mode"] == "add" and p["codec"] == "aac")
    pv = oclient.post("/api/optimizer/preview", json={"library_id": 1, "kind": "audio", "profile_id": rep["id"]}).json()
    assert pv["items"][0]["plan"]["predicted_saved"] > 0  # DTS 1411 kbit/s -> AC3 640 kbit/s
    pv = oclient.post("/api/optimizer/preview", json={"library_id": 1, "kind": "audio", "profile_id": add["id"]}).json()
    assert pv["items"][0]["plan"]["predicted_saved"] < 0  # zusätzliche Spur: Datei wird größer


def test_repair_jobs(oclient, opt_env, base_media):
    media = opt_env["media"]
    mf.copy(base_media["ok"], media / "gut.mkv")
    mf.copy(base_media["ok"], media / "restdaten.mkv")
    with open(media / "restdaten.mkv", "ab") as fh:
        fh.write(b"\x00" * 4096)  # Daten nach Segmentende – per Remux behebbar
    mf.copy(base_media["cluster_header"], media / "kaputt.mkv")
    orig = sha(media / "kaputt.mkv")
    from conftest import wait_job
    wait_job(oclient, oclient.post("/api/jobs", json={"task": "incremental", "methods": ["container"]}).json()["id"])
    files = files_by_name(oclient)
    assess = {n: oclient.get(f"/api/files/{files[n]['id']}").json()["assessment"]["category"]
              for n in ("gut.mkv", "restdaten.mkv", "kaputt.mkv")}
    assert assess == {"gut.mkv": "ok", "restdaten.mkv": "repair", "kaputt.mkv": "replace"}, assess

    # Liste „Sollten repariert werden“
    j = wait_opt_job(oclient, oclient.post("/api/optimizer/jobs", json={"kind": "repair", "category": "repair"}).json()["id"])
    assert j["done_files"] == 1 and j["state"] == "completed", j
    out = list(opt_env["output"].rglob("restdaten.mkv"))
    assert out and out[0].stat().st_size < (media / "restdaten.mkv").stat().st_size

    # Ohne Notreparatur werden verlorene Dateien und Dateien ohne Bedarf übersprungen
    ids = [files["gut.mkv"]["id"], files["kaputt.mkv"]["id"]]
    j = wait_opt_job(oclient, oclient.post("/api/optimizer/jobs", json={"kind": "repair", "file_ids": ids}).json()["id"])
    assert j["skipped_files"] == 2, j
    # Notreparatur birgt die beschädigte Datei in eine neue, strukturell saubere Datei
    j = wait_opt_job(oclient, oclient.post("/api/optimizer/jobs", json={
        "kind": "repair", "file_ids": [files["kaputt.mkv"]["id"]], "repair": {"salvage": True}}).json()["id"])
    assert j["done_files"] == 1, j
    assert list(opt_env["output"].rglob("kaputt.mkv"))
    assert sha(media / "kaputt.mkv") == orig  # Original unverändert


def test_repair_with_track_removal(oclient, opt_env, multi_src):
    media = opt_env["media"]
    mf.copy(multi_src, media / "rest.mkv")
    with open(media / "rest.mkv", "ab") as fh:
        fh.write(b"\x00" * 4096)  # per Remux behebbar
    from conftest import wait_job
    wait_job(oclient, oclient.post("/api/jobs", json={"task": "incremental", "methods": ["container"]}).json()["id"])
    fid = files_by_name(oclient)["rest.mkv"]["id"]
    assert oclient.get(f"/api/files/{fid}").json()["assessment"]["category"] == "repair"
    r = oclient.post("/api/optimizer/jobs", json={"kind": "repair", "category": "repair",
                                                  "repair": {"strip_tracks": True}})
    assert r.status_code == 400 and "Spurregeln" in r.json()["detail"]  # noch keine Regeln gespeichert
    r = oclient.post("/api/optimizer/jobs", json={"kind": "repair", "category": "repair", "rules": RULES_DTS,
                                                  "repair": {"strip_tracks": True}})
    j = wait_opt_job(oclient, r.json()["id"])
    assert j["state"] == "completed" and j["done_files"] == 1, j
    s, _ = sig(opt_env["output"] / "rest.mkv")
    assert [(x[0], x[2]) for x in s] == [("video", "und"), ("audio", "ger"), ("subtitles", "ger")]
    item = oclient.get(f"/api/optimizer/jobs/{j['id']}/items").json()["items"][0]
    assert "Spur(en) entfernt" in (item["message"] or "")
