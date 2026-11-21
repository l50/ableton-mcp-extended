"""Live checks for arrangement clip region copy and move.

These talk to a running Live through the Remote Script socket. They place
ABLETON_MCP_CLIP_EDIT_FILE (any audio file of at least 2 seconds) on the
first audio track, far past the end of the Set, copy and move it there, and
delete every clip they made. Run them only with a throwaway Set open:

    ABLETON_MCP_LIVE_TESTS=1 ABLETON_MCP_CLIP_EDIT_FILE=/path/to/take.wav \\
        uv run --extra test python -m pytest -m integration \\
        tests/integration/test_arrangement_clip_edit_live.py -s

They are excluded by default (the "integration" marker) and also skip unless
both variables are set.
"""
import os

import pytest

pytestmark = pytest.mark.integration

AUDIO_FILE = os.environ.get("ABLETON_MCP_CLIP_EDIT_FILE", "")
if os.environ.get("ABLETON_MCP_LIVE_TESTS") != "1" or not AUDIO_FILE:
    pytest.skip("set ABLETON_MCP_LIVE_TESTS=1 and ABLETON_MCP_CLIP_EDIT_FILE to run "
                "clip edit checks", allow_module_level=True)

from MCP_Server.connection import AbletonConnection  # noqa: E402

# Far enough right to stay clear of anything in a scratch Set.
BASE = 4000.0
TOLERANCE = 1e-3


@pytest.fixture
def live():
    conn = AbletonConnection(host="localhost", port=9877)
    if not conn.connect():
        pytest.skip("Ableton Live is not reachable on localhost:9877")
    yield conn
    conn.disconnect()


def _clips(live, track_index):
    info = live.send_command("get_arrangement_info", {"track_index": track_index})
    return info["tracks"][0]["arrangement_clips"], info["transport"]["tempo"]


def _clip_at(live, track_index, start):
    clips, _ = _clips(live, track_index)
    found = [c for c in clips if abs(c["start_time"] - start) < TOLERANCE]
    return found[0] if found else None


@pytest.fixture
def take(live):
    """An unwarped clip of AUDIO_FILE at BASE on the first audio track."""
    tracks = live.send_command("get_arrangement_info", {"track_index": -1})["tracks"]
    audio = [t for t in tracks if not t["is_midi"]]
    if not audio:
        pytest.skip("the Set has no audio track")
    track_index = audio[0]["index"]
    live.send_command("create_arrangement_audio_clip", {
        "track_index": track_index, "position": BASE, "file_path": AUDIO_FILE})
    clip = _clip_at(live, track_index, BASE)
    live.send_command("set_arrangement_clip_property", {
        "track_index": track_index, "clip_index": clip["index"],
        "property": "warping", "value": False})
    try:
        yield track_index
    finally:
        while True:
            clips, _ = _clips(live, track_index)
            ours = [c for c in clips if c["start_time"] >= BASE - TOLERANCE]
            if not ours:
                break
            live.send_command("delete_arrangement_clip", {
                "track_index": track_index, "clip_index": ours[-1]["index"]})


def test_copy_sub_second_region_unwarped(live, take):
    source = _clip_at(live, take, BASE)
    destination = source["end_time"] + 8
    result = live.send_command("copy_arrangement_audio_region", {
        "track_index": take, "clip_index": source["index"], "region_start": 0.5,
        "region_end": 1.2, "destination_track_index": take,
        "destination_time": destination})
    copy = _clip_at(live, take, destination)
    _, tempo = _clips(live, take)

    assert result["file_path"] == source["file_path"] == copy["file_path"]
    assert copy["warping"] is False
    assert abs(copy["loop_start"] - 0.5) < TOLERANCE
    assert abs(copy["loop_end"] - 1.2) < TOLERANCE
    assert abs(copy["length"] - 0.7 * tempo / 60.0) < 0.01
    # The source is untouched and no staging copy is left behind.
    assert _clip_at(live, take, BASE)["loop_end"] == source["loop_end"]
    clips, _ = _clips(live, take)
    assert len([c for c in clips if c["start_time"] >= BASE - TOLERANCE]) == 2


def test_copy_onto_its_source_reports_the_overlap(live, take):
    source = _clip_at(live, take, BASE)
    result = live.send_command("copy_arrangement_audio_region", {
        "track_index": take, "clip_index": source["index"], "region_start": 0.5,
        "region_end": 1.2, "destination_track_index": take,
        "destination_time": BASE + 8})
    assert result["overlapped_clips"] == [source["name"]]
    assert abs(_clip_at(live, take, BASE + 8)["loop_start"] - 0.5) < TOLERANCE


def test_move_removes_original_and_keeps_copy(live, take):
    source = _clip_at(live, take, BASE)
    live.send_command("move_arrangement_clip", {
        "track_index": take, "clip_index": source["index"],
        "destination_track_index": take, "destination_time": source["end_time"] + 8})
    assert _clip_at(live, take, BASE) is None
    moved = _clip_at(live, take, source["end_time"] + 8)
    for key in ("file_path", "warping", "loop_start", "loop_end", "name"):
        assert moved[key] == source[key], key


def test_move_onto_itself(live, take):
    source = _clip_at(live, take, BASE)
    result = live.send_command("move_arrangement_clip", {
        "track_index": take, "clip_index": source["index"],
        "destination_track_index": take, "destination_time": BASE + 0.5})
    assert result["staged_at"] is not None
    clips, _ = _clips(live, take)
    ours = [c for c in clips if c["start_time"] >= BASE - TOLERANCE]
    assert [round(c["start_time"], 3) for c in ours] == [BASE + 0.5]
    assert abs(ours[0]["loop_end"] - source["loop_end"]) < TOLERANCE
