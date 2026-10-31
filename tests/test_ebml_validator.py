"""EBML/Matroska-Strukturprüfer gegen programmatisch beschädigte Dateien."""

import media_factory as mf
from app.scanners.ebml import validate


def types(res, *sev):
    return {(f["error_type"]) for f in res["findings"] if not sev or f["severity"] in sev}


def test_valid_files_have_no_findings(base_media):
    for key in ("ok", "ok_mkvmerge", "long"):
        res = validate(str(base_media[key]))
        bad = [f for f in res["findings"] if f["severity"] in ("warning", "error", "critical")]
        assert bad == [], (key, bad)
        assert res["bytes_read"] == res["file_size"]  # jedes Byte gelesen
        assert res["clusters"] > 0


def test_crc_detected_when_present(base_media):
    res = validate(str(base_media["ok"]))
    assert res["crc_checked"] > 0 and res["crc_failed"] == 0
    res = validate(str(base_media["flip"]))
    assert "cluster_crc" in types(res, "error")
    f = next(f for f in res["findings"] if f["error_type"] == "cluster_crc")
    assert f["time_precision"] == "range" and f["time_start"] is not None


def test_damaged_cluster_header(base_media):
    res = validate(str(base_media["cluster_header"]))
    assert "matroska_structure" in types(res, "error")
    f = next(f for f in res["findings"] if f["error_type"] == "matroska_structure")
    # Zeitbereich aus echten Clusterzeitstempeln, keine Schätzung
    assert f["time_precision"] == "range" and f["time_start"] < f["time_end"]
    assert f["byte_offset"] is not None and f["byte_end"] > f["byte_offset"]


def test_damaged_block_payload(base_media):
    res = validate(str(base_media["payload"]))
    assert types(res, "error") & {"cluster_damaged", "cluster_crc", "block_invalid"}


def test_truncated(base_media):
    res = validate(str(base_media["truncated"]))
    assert "truncated" in types(res, "critical")
    assert "cluster_crc" not in types(res)  # unvollständiger Cluster ist kein CRC-Befund


def test_broken_ebml_header(base_media):
    res = validate(str(base_media["ebml_header"]))
    assert "ebml_header" in types(res, "critical")


def test_timestamp_jump(base_media):
    res = validate(str(base_media["ts_jump"]))
    jumps = [f for f in res["findings"] if f["error_type"] == "timestamp_jump"]
    assert jumps and jumps[0]["severity"] == "error" and jumps[0]["time_precision"] == "exact"
    assert "timestamp_backwards" in types(res, "error")


def test_unknown_codec_is_structurally_fine(base_media):
    res = validate(str(base_media["unknown_codec"]))
    assert not types(res, "error", "critical")


def test_stray_subtitle_cluster_is_only_a_warning(base_media, tmp_path):
    """Ein einzelner Untertitel-Cluster hinter der Zeitleiste (PGS verspätet geschrieben) ist kein Inhaltssprung."""
    src = mf.stray_subtitle_cluster(base_media["ok"], tmp_path / "stray.mkv")
    res = validate(str(src))
    errs = [f for f in res["findings"] if f["error_type"].startswith("timestamp") and f["severity"] in ("error", "critical")]
    assert not errs, errs
    warn = [f for f in res["findings"] if f["error_type"] == "timestamp_backwards" and f["severity"] == "warning"]
    assert warn and "Untertitelpaket" in warn[0]["description"]

    from app.services.repairability import assess
    # (Index-Hinweise entstehen hier nur durch das Einfügen des Clusters)
    ts_findings = [f for f in res["findings"] if f["error_type"] != "index_invalid"]
    assert assess("warning", ts_findings)["category"] == "optional"
