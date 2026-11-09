"""Live checks for the recording-safety commands.

These talk to a running Live through the Remote Script socket and create,
change and delete a scratch MIDI track. Run them only against a scratch Set:

    ABLETON_MCP_LIVE_TESTS=1 pytest -m integration tests/integration

They are excluded by default (the "integration" marker) and also skip unless
ABLETON_MCP_LIVE_TESTS=1 is set.
"""
import os
import uuid

import pytest

pytestmark = pytest.mark.integration

if os.environ.get("ABLETON_MCP_LIVE_TESTS") != "1":
    pytest.skip("set ABLETON_MCP_LIVE_TESTS=1 to run Live checks",
                allow_module_level=True)

from MCP_Server.server import AbletonConnection  # noqa: E402


@pytest.fixture
def live():
    conn = AbletonConnection(host="localhost", port=9877)
    if not conn.connect():
        pytest.skip("Ableton Live is not reachable on localhost:9877")
    yield conn
    conn.disconnect()


@pytest.fixture
def scratch(live):
    """A new MIDI track at the end of the Set, deleted afterwards."""
    created = live.send_command("create_midi_track", {"index": -1})
    index = created["index"]
    name = "mcp-it-" + uuid.uuid4().hex[:6]
    live.send_command("set_track_name", {"track_index": index, "name": name})
    try:
        yield index, name
    finally:
        try:
            live.send_command("set_track_arm", {"track_name": name,
                                                "arm": False})
        finally:
            live.send_command("delete_track", {"track_index": index})


def test_monitoring_round_trip(live, scratch):
    index, name = scratch
    for state in ("in", "auto", "off"):
        result = live.send_command("set_track_monitoring", {
            "track_index": index, "expect_name": name, "state": state})
        assert result["monitoring"] == state


def test_no_input_track_can_still_be_armed_and_is_flagged(live, scratch):
    index, name = scratch
    live.send_command("set_track_input", {"track_index": index,
                                          "input_type": "No Input"})
    armed = live.send_command("set_track_arm", {"track_index": index,
                                                "arm": True})
    assert armed["arm"] is True
    assert any("No Input" in w for w in armed["warnings"])

    status = live.send_command("get_recording_status")
    assert name in [t["name"] for t in status["armed_tracks"]]
    assert any(name in w and "No Input" in w for w in status["warnings"])


def test_routing_options_name_the_current_input(live, scratch):
    index, _ = scratch
    options = live.send_command("get_track_routing_options",
                                {"track_index": index})
    assert options["input"]["current"]["type"] in options["input"]["types"]


def test_expect_name_mismatch_changes_nothing(live, scratch):
    index, _ = scratch
    with pytest.raises(Exception) as excinfo:
        live.send_command("set_track_mute", {"track_index": index,
                                             "expect_name": "not-this-one",
                                             "mute": True})
    assert "expect_name mismatch" in str(excinfo.value)
    info = live.send_command("get_track_info", {"track_index": index,
                                                "include_clip_slots": False})
    assert info["mute"] is False


def test_batch_dry_run_then_apply(live, scratch):
    index, name = scratch
    change = [{"track_name": name, "mute": True, "monitoring": "off"}]
    preview = live.send_command("set_tracks_state", {"changes": change,
                                                     "dry_run": True})
    assert preview["dry_run"] is True
    assert live.send_command("get_track_info", {
        "track_index": index, "include_clip_slots": False})["mute"] is False

    applied = live.send_command("set_tracks_state", {"changes": change})
    assert applied["changes"][0]["after"]["mute"] is True
    assert applied["changes"][0]["after"]["monitoring"] == "off"


def test_list_tracks_includes_the_scratch_track(live, scratch):
    index, name = scratch
    rows = live.send_command("list_tracks", {"include_returns": True,
                                             "include_master": True})["tracks"]
    row = [r for r in rows if r["name"] == name][0]
    assert row["index"] == index and row["kind"] == "midi"
    assert rows[-1]["kind"] == "master"
