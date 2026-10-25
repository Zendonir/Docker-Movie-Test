"""Reparaturbewertung anhand typischer Befunde aus echten 4K-Remuxen."""

from app.services.exports import _csv_val
from app.services.repairability import assess, classify_finding

PGS = {"error_type": "metadata_inconsistent", "severity": "warning", "description": "Codecparameter …",
       "raw_message": "[matroska,webm @ 0x1] [warning] Could not find codec parameters for stream 2 "
                      "(Subtitle: hdmv_pgs_subtitle (pgssub)): unspecified size", "stream_type": "subtitle"}
DV = {"error_type": "tool_warning", "severity": "warning", "description": "",
      "raw_message": "Invalid Block Addition value 0x0 for unknown Block Addition Mapping type 68766345"}
SMALL_BACK = {"error_type": "timestamp_backwards", "severity": "error", "occurrences": 46,
              "description": "Clusterzeitstempel springt rückwärts von 696.654 s auf 696.613 s."}
TRUEHD = {"error_type": "timestamp_invalid", "severity": "warning", "heuristic": True, "description": "",
          "raw_message": "Application provided invalid, non monotonically increasing dts to muxer in stream 1: 4 >= 4\n"
                         "Application provided invalid, non monotonically increasing dts to muxer in stream 1: 9 >= 9"}
CLUSTER = {"error_type": "cluster_damaged", "severity": "error", "description": "Unbekanntes Element …"}
MPEG2 = {"error_type": "video_decode_error", "severity": "error", "description": "",
         "raw_message": "[mpeg2video @ 0x1] [error] ignoring pic cod ext after 0"}


def test_typical_remux_is_ok():
    a = assess("error", [PGS, DV, SMALL_BACK, TRUEHD, MPEG2])
    assert a["category"] == "ok", a


def test_damaged_cluster_is_unrepairable():
    assert assess("error", [PGS, CLUSTER])["category"] == "replace"


def test_missing_end_is_unrepairable():
    f = {"error_type": "duration_mismatch", "severity": "warning",
         "description": "Laufzeit laut Segment-Info 5620.0 s, letzte Blockdaten bei 5482.3 s (Abweichung +137.7 s)."}
    assert classify_finding(f)[0] == "loss"


def test_index_and_trailing_data_are_repairable():
    idx = {"error_type": "index_invalid", "severity": "warning", "description": "Cues …"}
    trail = {"error_type": "trailing_data", "severity": "warning", "description": "1 Byte nach dem Ende"}
    assert assess("warning", [idx, trail])["category"] == "repair"


def test_large_backwards_jump_is_loss():
    f = dict(SMALL_BACK, description="Clusterzeitstempel springt rückwärts von 7040.867 s auf 5.000 s.")
    assert classify_finding(f)[0] == "loss"


def test_csv_decimal_comma():
    assert _csv_val(3.67, True) == "3,67"
    assert _csv_val(5482.252333334, True) == "5482,252"
    assert _csv_val(10.01, False) == "10.01"
