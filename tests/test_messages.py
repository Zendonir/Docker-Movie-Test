"""Interpretation der Meldungen von FFmpeg und MKVToolNix."""

from app.scanners.base import Severity
from app.scanners.matroska import MkvmergeParser
from app.scanners.messages import classify_ffmpeg, parse_ffmpeg_line

DECODERS = {"h264", "aac", "ac3", "hevc", "mp3"}


def c(line):
    p = parse_ffmpeg_line(line, DECODERS)
    return classify_ffmpeg(p) if p else None


def test_benign_warnings_are_ignored():
    for line in [
        "[aac @ 0x1] [warning] Guessed Channel Layout for Input Stream #0.1 : stereo",
        "[matroska,webm @ 0x1] [warning] Could not update timestamps for skipped samples.",
        "[h264 @ 0x1] [warning] Increasing reorder buffer to 1",
        "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1] [warning] Discarding ID3 tags because more suitable tags were found.",
        "[info] Stream mapping:",
    ]:
        assert c(line) is None, line


def test_decoder_errors_are_errors_with_stream():
    r = c("[h264 @ 0x55] [error] error while decoding MB 4 17, bytestream 862")
    assert r.error_type == "video_decode_error" and r.severity == Severity.ERROR and r.codec == "h264"
    r = c("[vist#0:0/h264 @ 0x55] [warning] corrupt decoded frame")
    assert r.error_type == "corrupt_frame" and r.severity == Severity.ERROR and r.stream_index == 0
    r = c("[aac @ 0x55] [error] Input buffer exhausted before END element found")
    assert r.error_type == "audio_decode_error"
    r = c("[aist#0:1/aac @ 0x55] [error] Error submitting packet to decoder: Invalid data found when processing input")
    assert r.error_type == "audio_decode_error" and r.stream_index == 1


def test_packet_corrupt_has_dts_and_stream():
    r = c("[mpegts @ 0x55] [warning] Packet corrupt (stream = 0, dts = 550800).")
    assert r.error_type == "packet_corrupt" and r.stream_index == 0 and r.dts == 550800


def test_structure_and_truncation():
    r = c("[matroska,webm @ 0x55] [error] Element at 0xd01c6 ending at 0xbeacfe7 exceeds containing master element ending at 0x20958a")
    assert r.error_type == "matroska_structure" and r.byte_offset == 0xD01C6
    r = c("[matroska,webm @ 0x55] [error] File ended prematurely")
    assert r.error_type == "truncated" and r.severity == Severity.CRITICAL
    r = c("[mov,mp4,m4a,3gp,3g2,mj2 @ 0x55] [error] moov atom not found")
    assert r.error_type == "file_unreadable" and r.severity == Severity.CRITICAL


def test_unsupported_codec_is_not_a_defect():
    r = c("[vist#0:0/none @ 0x55] [error] Decoding requested, but no decoder found for: none")
    assert r.error_type == "unsupported_codec" and r.severity == Severity.INCOMPLETE
    r = c("[matroska,webm @ 0x55] [warning] Could not find codec parameters for stream 0 (Video: none, none(progressive), 640x360): unknown codec")
    assert r.error_type == "unsupported_codec" and r.severity == Severity.INCOMPLETE


def test_unknown_messages_are_never_proof_of_damage():
    r = c("[null @ 0x55] [warning] something nobody has ever seen")
    assert r is None
    r = c("[h264 @ 0x55] [warning] some new decoder warning")
    assert r.severity == Severity.WARNING and r.heuristic
    r = c("[out#0/null @ 0x55] [error] something odd")
    assert r.severity == Severity.WARNING and r.heuristic


def test_timestamp_warnings_are_heuristic_warnings():
    r = c("[null @ 0x55] [warning] Application provided invalid, non monotonically increasing dts to muxer in stream 1: 3240912 >= 668640")
    assert r.error_type == "timestamp_invalid" and r.severity == Severity.WARNING and r.heuristic


def test_mkvmerge_structure_sequence_combined():
    p = MkvmergeParser([[100, 0.0], [852153, 7.9], [1056228, 9.92]])
    for line in [
        "#GUI#warning x.mkv: Error in the Matroska file structure at position 852153. Resyncing to the next level 1 element.",
        "#GUI#warning Resyncing successful at position 1056228.",
        "#GUI#warning The first cluster timestamp after the resync is 00:00:09.920000000.",
        "#GUI#warning x.mkv: Error in the Matroska file structure at position 852153. Resyncing to the next level 1 element.",
        "#GUI#warning The last timestamp processed before the error was encountered was 00:00:07.914000000.",
        "#GUI#warning Updating the 'document type version' or 'document type read version' header fields failed. No 'EBML head' element was found.",
        "#GUI#progress 100%",
    ]:
        p.on_line(line)
    f = p.finish()
    assert len(f) == 1, [x.description for x in f]
    f = f[0]
    assert f.byte_offset == 852153 and f.byte_end == 1056228
    assert abs(f.time_start - 7.914) < 1e-6 and abs(f.time_end - 9.92) < 1e-6
    assert f.time_precision.value == "range"


def test_mkvmerge_dev_null_artifact_ignored():
    p = MkvmergeParser(None)
    p.on_line("#GUI#warning Updating the 'document type version' or 'document type read version' header fields failed. No 'EBML head' element was found.")
    assert p.finish() == []


def test_mkvmerge_resync_failure_is_critical():
    p = MkvmergeParser(None)
    p.on_line("#GUI#warning a.mkv: Error in the Matroska file structure at position 5000. Resyncing to the next level 1 element.")
    p.on_line("#GUI#warning Resync failed: no valid Matroska level 1 element found.")
    assert p.finish()[0].severity == Severity.CRITICAL


def test_mkvmerge_unclassified_warning_is_heuristic():
    p = MkvmergeParser(None)
    p.on_line("#GUI#warning Something unusual happened")
    f = p.finish()[0]
    assert f.severity == Severity.WARNING and f.heuristic
