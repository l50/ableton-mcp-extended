"""Tests for clear_selection and finish_ui_tool (issue #12).

A time selection left behind by Edit > Select Loop makes stray keys nudge or
delete clips, and clearing it by clicking a lane once started playback. So
the selection is cleared with a menu command only, read back, and the
transport compared with how it was.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from MCP_Server import live_ui_state as u  # noqa: E402
from tests.unit.ui_fakes import FakeUI, raw_window  # noqa: E402

MENUS = [["Playback", "Move Insert Marker To Playhead"]]


def menus_clicked(live):
    return [c["path"] for c in live.calls if c["op"] == "menu"]


# ── clearing ─────────────────────────────────────────────────────


def test_clears_a_time_selection_and_reads_it_back():
    live = FakeUI(selection=True)
    result = u.clear_selection(live)
    assert result["ok"] is True, result
    assert result["time_selection_before"]["present"] is True
    assert result["time_selection_after"] == {"present": False}
    assert menus_clicked(live) == MENUS
    assert live.selection is False


def test_clears_does_nothing_when_there_is_no_selection():
    live = FakeUI(selection=False)
    result = u.clear_selection(live)
    assert result["ok"] is True
    assert menus_clicked(live) == []
    assert live.clicks() == []


def test_clears_moves_focus_to_the_arrangement_when_selection_cannot_be_told():
    live = FakeUI(selection=True, focus_group="Mixer")
    result = u.clear_selection(live)
    assert result["ok"] is True, result
    assert menus_clicked(live) == [["Navigate", "Arrangement View"], *MENUS]
    assert result["time_selection_before"]["present"] is None
    assert live.selection is False


def test_clears_with_another_view_focused_and_no_selection_only_moves_focus():
    live = FakeUI(selection=False, focus_group="Mixer")
    result = u.clear_selection(live)
    assert result["ok"] is True
    assert menus_clicked(live) == [["Navigate", "Arrangement View"]]


def test_selection_still_present_after_the_command_is_a_failure():
    live = FakeUI(selection=True, marker_keeps_selection=True)
    result = u.clear_selection(live)
    assert result["ok"] is False
    assert result["error"] == "a time selection is still present"


def test_selection_that_cannot_be_confirmed_gone_is_a_failure():
    live = FakeUI(selection=True, edit_unreadable=True)
    result = u.clear_selection(live)
    assert result["ok"] is False and "could not confirm" in result["error"]


def test_a_menu_command_that_fails_is_reported_with_its_error():
    live = FakeUI(selection=True,
                  menu_errors={("Playback", "Move Insert Marker To Playhead"): "menu item is disabled"})
    result = u.clear_selection(live)
    assert result["ok"] is False
    assert "Playback > Move Insert Marker To Playhead: menu item is disabled" in result["error"]


def test_clears_leaves_clip_selection_alone_and_reports_it():
    live = FakeUI(selection=True, items_selected=True)
    result = u.clear_selection(live)
    assert result["ok"] is True and result["items_selected_after"] is True


# ── menu only ────────────────────────────────────────────────────


def test_menu_only_no_click_in_a_lane_and_no_keystroke():
    live = FakeUI(selection=True, focus_group="Mixer")
    u.clear_selection(live)
    assert set(live.ops()) <= {"ui_state", "menu"}
    assert "keystroke" not in live.ops() and "press" not in live.ops()


def test_menu_only_refuses_in_key_map_mode_without_clicking():
    live = FakeUI(selection=True, key_map=True)
    with pytest.raises(u.UnsafeUIState, match="Key Map mode"):
        u.clear_selection(live)
    assert live.clicks() == []


def test_menu_only_refuses_in_midi_map_mode_without_clicking():
    live = FakeUI(selection=True, midi_map=True)
    with pytest.raises(u.UnsafeUIState, match="MIDI Map mode"):
        u.clear_selection(live)
    assert live.clicks() == []


def test_menu_only_refuses_with_a_dialog_open_without_clicking():
    live = FakeUI(selection=True, dialogs=[raw_window("Export Audio/Video")])
    with pytest.raises(u.UnsafeUIState, match="Export Audio/Video"):
        u.clear_selection(live)
    assert live.clicks() == []


# ── transport ────────────────────────────────────────────────────


def test_transport_playback_started_by_the_clear_is_stopped():
    live = FakeUI(selection=True, marker_starts_playback=True)
    result = u.clear_selection(live)
    assert result["ok"] is True, result
    assert result["stopped_playback"] is True
    assert "stop_transport" in live.ops() and live.playing is False
    assert result["transport_changed"] == []
    assert any("playback started" in w for w in result["warnings"])


def test_transport_that_cannot_be_stopped_is_a_failure_that_says_so():
    live = FakeUI(selection=True, marker_starts_playback=True, stop_fails=True)
    # the fake's Stop button fails: the call reports ok false and playback stays on
    result = u.clear_selection(live)
    assert result["ok"] is False
    assert "playing changed from False to True" in result["error"]


def test_transport_already_playing_is_left_playing_by_the_cleanup_after_a_tool():
    live = FakeUI(selection=True, playing=True)
    result = u.clear_selection(live, ignore_transport=True)
    assert result["ok"] is True and live.playing is True
    assert "stop_transport" not in live.ops()


@pytest.mark.parametrize("over, reason", [({"playing": True}, "Live is playing"),
                                          ({"recording": True}, "Live is recording")],
                         ids=["playing", "recording"])
def test_transport_running_makes_the_clear_selection_tool_refuse_without_clicking(over, reason):
    live = FakeUI(selection=True, **over)
    with pytest.raises(u.UnsafeUIState, match=reason):
        u.clear_selection(live)
    assert live.clicks() == []


def test_transport_flags_are_compared_before_and_after():
    live = FakeUI(selection=True, loop=True)
    result = u.clear_selection(live)
    assert result["transport_before"] == result["transport_after"]
    assert result["transport_before"]["loop"] is True


# ── finish_ui_tool: what every UI tool calls when it is done ─────


def finish(live, before=None):
    before = before or u.transport_snapshot(u.read_ui_state(FakeUI()))
    return u.finish_ui_tool(live, before)


def test_unchanged_transport_and_cleared_selection_are_reported():
    live = FakeUI(selection=True)
    outcome = finish(live)
    assert outcome == {"selection_cleared": True, "transport_unchanged": True, "transport_changed": []}


def test_unchanged_is_false_when_the_tool_left_playback_on():
    live = FakeUI(selection=True, playing=True)
    outcome = finish(live)
    assert outcome["transport_unchanged"] is False
    assert outcome["transport_changed"] == ["playing changed from False to True"]
    assert "playing changed" in outcome["error"]


def test_unchanged_is_false_when_loop_was_toggled_during_the_tool():
    live = FakeUI(selection=False, loop=True)
    outcome = finish(live)
    assert outcome["transport_unchanged"] is False
    assert outcome["transport_changed"] == ["loop changed from False to True"]


def test_unchanged_reports_playback_started_by_a_click_and_stopped():
    live = FakeUI(selection=True, marker_starts_playback=True)
    outcome = finish(live)
    assert outcome["stopped_playback"] is True and outcome["transport_unchanged"] is True


def test_unchanged_does_not_raise_when_a_dialog_blocks_the_cleanup():
    live = FakeUI(selection=True, dialogs=[raw_window("Export Audio/Video")])
    outcome = finish(live)
    assert outcome["selection_cleared"] is False and outcome["transport_unchanged"] is None
    assert "not safe to touch" in outcome["error"] and live.clicks() == []


def test_unchanged_does_not_raise_when_the_driver_fails():
    class Broken:
        def call(self, op, **params):
            raise RuntimeError("osascript timed out")

    outcome = u.finish_ui_tool(Broken(), {})
    assert outcome["selection_cleared"] is False
    assert outcome["error"] == "could not clear the selection: osascript timed out"
