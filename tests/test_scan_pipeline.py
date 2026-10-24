"""Ende-zu-Ende: Bibliothek einlesen, alle drei Prüfmodi, Klassifizierung, Export.

Abnahmekriterien B, C, E, F, H (und Teile von A/G).
"""

import csv
import hashlib
import io
import json

from conftest import files_by_name, wait_job
import media_factory as mf

UMLAUT = "Kikis kleiner Lieferservice (1989) – Größe Ü.mkv"


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def setup_library(app_env, base_media):
    m = app_env["media"]
    files = {
        UMLAUT: base_media["ok"],
        "ok_mkvmerge.mkv": base_media["ok_mkvmerge"],
        "Unterordner/ok.mp4": base_media["mp4"],
        "Unterordner/ok.ts": base_media["ts"],
        "defekt/cluster_header.mkv": base_media["cluster_header"],
        "defekt/decoderfehler.mkv": base_media["payload"],
        "defekt/abgeschnitten.mkv": base_media["truncated"],
        "defekt/kein_header.mkv": base_media["ebml_header"],
        "defekt/zeitsprung.mkv": base_media["ts_jump"],
        "defekt/aufnahme.ts": base_media["ts_damaged"],
        "codec/unbekannter_codec.mkv": base_media["unknown_codec"],
        "@eaDir/thumb.mkv": base_media["ok"],  # muss ausgeschlossen werden
        "notizen.txt": base_media["ok"],  # keine Mediendatei
    }
    for rel, src in files.items():
        mf.copy(src, m / rel)
    return {rel: sha(m / rel) for rel in files}


def test_full_pipeline(client, app_env, base_media):
    hashes = setup_library(app_env, base_media)
    r = client.post("/api/jobs", json={"task": "incremental", "methods": ["quick", "container", "full"]})
    assert r.status_code == 201, r.text
    job = wait_job(client, r.json()["id"])
    assert job["state"] == "completed"
    assert job["total_files"] == 11, job

    f = files_by_name(client)
    assert "thumb.mkv" not in f and "notizen.txt" not in f

    # Fehlerfreie Dateien: nur "ok", wenn alle Prüfungen vollständig liefen
    for name in (UMLAUT, "ok_mkvmerge.mkv", "ok.mp4", "ok.ts"):
        assert f[name]["status"] == "ok", (name, f[name])
        assert f[name]["check_level"] == "container_full"

    # B: beschädigte Cluster durch die Containerprüfung erkannt
    d = client.get(f"/api/files/{f['cluster_header.mkv']['id']}").json()
    assert d["status"] in ("error", "critical")
    container = [e for e in d["errors"] if e["method"] == "container"]
    assert any(e["error_type"] == "matroska_structure" and e["severity"] == "error" for e in container)
    assert d["current_results"]["container"]["status"] == "error"

    # C: Decoderfehler erkannt und Meldungen gespeichert
    d = client.get(f"/api/files/{f['decoderfehler.mkv']['id']}").json()
    full = [e for e in d["errors"] if e["method"] == "full"]
    assert any(e["error_type"] in ("video_decode_error", "corrupt_frame") for e in full)
    assert all(e["raw_message"] for e in full if e["severity"] == "error")
    assert any("decoding" in (e["raw_message"] or "").lower() or "corrupt" in (e["raw_message"] or "").lower() for e in full)
    # Decoderfehler haben nie eine "exakte" Position, sondern höchstens ungefähr/Bereich
    assert all(e["time_precision"] in ("approx", "range", "unknown") for e in full if e["error_type"] in ("video_decode_error", "corrupt_frame"))
    log = client.get(f"/api/files/{d['id']}/results/{d['current_results']['full']['id']}/log")
    assert log.status_code == 200 and "error" in log.text

    assert f["abgeschnitten.mkv"]["status"] == "critical"
    assert f["kein_header.mkv"]["status"] == "critical"
    assert f["zeitsprung.mkv"]["status"] == "error"
    d = client.get(f"/api/files/{f['zeitsprung.mkv']['id']}").json()
    jump = next(e for e in d["errors"] if e["error_type"] == "timestamp_jump")
    assert jump["time_precision"] == "exact" and abs(jump["time_start"] - 12.0) < 0.5

    d = client.get(f"/api/files/{f['aufnahme.ts']['id']}").json()
    assert d["status"] == "error"
    pc = [e for e in d["errors"] if e["error_type"] == "packet_corrupt" and e["time_precision"] in ("exact", "range")]
    assert pc, "Paketfehler mit DTS müssen eine exakte Zeit erhalten"

    # H: nicht unterstützter Codec ist kein Defekt
    u = f["unbekannter_codec.mkv"]
    assert u["status"] == "incomplete", u
    d = client.get(f"/api/files/{u['id']}").json()
    assert not [e for e in d["errors"] if e["severity"] in ("error", "critical")]
    assert any(e["error_type"] == "unsupported_codec" for e in d["errors"])

    # A/G: Originaldateien unverändert
    for rel, h in hashes.items():
        assert sha(app_env["media"] / rel) == h, rel

    # E: zweiter inkrementeller Scan überspringt alles Unveränderte
    r = client.post("/api/jobs", json={"task": "incremental", "methods": ["quick", "container", "full"]})
    job2 = wait_job(client, r.json()["id"])
    assert job2["total_files"] == 0 and job2["done_files"] == 0
    assert "11 unveränderte" in (job2["message"] or "")
    # andere Methode allein: ebenfalls bereits erledigt
    r = client.post("/api/jobs", json={"task": "incremental", "methods": ["container"]})
    assert wait_job(client, r.json()["id"])["total_files"] == 0

    # Datei ändern -> nur diese wird erneut geprüft
    p = app_env["media"] / "ok_mkvmerge.mkv"
    p.write_bytes(p.read_bytes() + b"\x00" * 16)
    r = client.post("/api/jobs", json={"task": "incremental", "methods": ["quick"]})
    job3 = wait_job(client, r.json()["id"])
    assert job3["total_files"] == 1

    # F: beschädigte filtern und als CSV exportieren
    dmg = client.get("/api/files", params={"only_damaged": True, "page_size": 100}).json()
    names = {x["filename"] for x in dmg["items"]}
    assert {"cluster_header.mkv", "decoderfehler.mkv", "abgeschnitten.mkv", "kein_header.mkv", "zeitsprung.mkv",
            "aufnahme.ts"} <= names
    assert UMLAUT not in names and "unbekannter_codec.mkv" not in names
    r = client.get("/api/export", params={"format": "csv", "scope": "filter", "only_damaged": True})
    assert r.status_code == 200
    raw = r.content
    assert raw.startswith("﻿".encode())
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    assert rows[0][0] == "Status" and "Zeitpunkt" in rows[0]
    assert {r_[2] for r_ in rows[1:]} >= {"cluster_header.mkv", "decoderfehler.mkv"}

    r = client.get("/api/export", params={"format": "csv", "scope": "library"})
    assert UMLAUT in r.content.decode("utf-8-sig")
    r = client.get("/api/export", params={"format": "json", "scope": "damaged"})
    data = json.loads(r.content.decode("utf-8"))
    assert len(data["files"]) >= 6 and all(x["findings"] for x in data["files"])
    r = client.get("/api/export", params={"format": "html", "scope": "file", "file_id": f[UMLAUT]["id"]})
    assert UMLAUT.replace("–", "&ndash;") in r.text or UMLAUT in r.text
    r = client.get("/api/export", params={"format": "html", "scope": "job", "job_id": job["id"]})
    assert "Scanlauf" in r.text and "Fehlerdetails" in r.text

    # Dashboard-Kennzahlen
    s = client.get("/api/stats").json()
    assert s["total_files"] == 11 and s["status"]["critical"] >= 2 and s["status"]["incomplete"] >= 1
    assert client.get("/api/stats/error-types").json()
    assert client.get("/api/stats/history").json()


def test_quick_only_is_not_full_check(client, app_env, base_media):
    mf.copy(base_media["ok"], app_env["media"] / "a.mkv")
    r = client.post("/api/jobs", json={"task": "incremental", "methods": ["quick"]})
    wait_job(client, r.json()["id"])
    f = files_by_name(client)["a.mkv"]
    assert f["status"] == "ok" and f["check_level"] == "quick"
    assert "noch keine vollständige Decodierprüfung" in (f["status_note"] or "")
    s = client.get("/api/stats").json()
    assert s["fully_checked"] == 0
    # Vollprüfung ist danach weiterhin nötig
    r = client.post("/api/jobs", json={"task": "incremental", "methods": ["full"]})
    assert wait_job(client, r.json()["id"])["total_files"] == 1
