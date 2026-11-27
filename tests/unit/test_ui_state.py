"""Tests for live_ui_state: reading Live's UI state and the safe-to-drive guard.

Accessibility is faked (FakeUI). The guard tests say what each unsafe state
must do: be reported by read_ui_state and make require_safe_to_drive raise
before any caller clicks.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from MCP_Server import live_ui_state as u  # noqa: E402
from tests.unit.ui_fakes import FakeUI, raw_window  # noqa: E402

ARMED = [{"index": 1, "name": "Guitar", "arm": True}]


def state_of(**over):
    return u.read_ui_state(FakeUI(**over), armed_reader=lambda: ARMED)


def refusal(**over):
    with pytest.raises(u.UnsafeUIState) as caught:
        u.require_safe_to_drive(FakeUI(**over))
    return caught.value


EXPORT_DIALOG = raw_window("Export Audio/Video", texts=["Rendered Track", "Render Start"],
                           buttons=["Export", "Cancel"])
SAVE_PANEL = raw_window("Save", modal=True, texts=["Save As:"], buttons=["Cancel", "Save"])
CRASH_RECOVERY = raw_window("", modal=True, buttons=["Recover", "Discard"],
                            texts=["Live quit unexpectedly. Recover the Set?"])
SAVE_CHANGES = raw_window("", modal=True, buttons=["Don’t Save", "Cancel", "Save"],
                          texts=['Save changes to "Untitled" before closing?'])


# ── shape ────────────────────────────────────────────────────────


def test_state_shape_has_every_field_the_issue_names():
    state = state_of(document="file:///Users/me/Set.als", title="Set")
    assert state["ok"] is True and state["running"] is True
    assert state["key_map_mode"] is False and state["midi_map_mode"] is False
    assert state["dialogs"] == []
    assert state["focus"] == {"view": "arrangement", "element": "Timeline"}
    assert set(state["transport"]) >= {"playing", "recording", "loop"}
    assert state["transport"] == {
        "playing": False, "recording": False, "arrangement_record": False,
        "session_record": False, "overdub": False, "loop": False,
        "punch_in": False, "punch_out": False}
    assert state["armed_tracks"] == ARMED
    assert state["time_selection"] == {"present": False}
    assert state["set"] == {"window_title": "Set", "path": "/Users/me/Set.als"}
    assert state["safe_to_drive"] is True and state["unsafe_reasons"] == []


def test_state_shape_when_live_is_not_running():
    state = state_of(running=False)
    assert state["running"] is False and state["safe_to_drive"] is False
    assert state["unsafe_reasons"] == ["Ableton Live is not running"]
    assert state["dialogs"] == [] and state["armed_tracks"] is None
    assert state["transport"]["playing"] is None and state["transport"]["recording"] is None


def test_state_shape_decodes_a_file_url_with_spaces():
    state = state_of(document="file:///Users/me/My%20Music/Song%20v2.als")
    assert state["set"]["path"] == "/Users/me/My Music/Song v2.als"


def test_state_shape_untitled_set_has_no_path():
    assert state_of()["set"]["path"] is None


# ── Key Map and MIDI Map mode ────────────────────────────────────


def test_key_map_mode_on_is_reported_and_refused():
    state = state_of(key_map=True)
    assert state["key_map_mode"] is True
    assert state["safe_to_drive"] is False
    assert any("Key Map mode is on" in r for r in state["unsafe_reasons"])
    error = refusal(key_map=True)
    assert "Key Map mode is on" in str(error) and error.state["key_map_mode"] is True


def test_key_map_mode_unreadable_is_refused_not_assumed_off():
    state = state_of(switch_values={"Key Map Mode": None})
    assert state["key_map_mode"] is None
    assert any("could not read whether Key Map mode is on" in r for r in state["unsafe_reasons"])


def test_midi_map_mode_on_is_reported_and_refused():
    state = state_of(midi_map=True)
    assert state["midi_map_mode"] is True and state["safe_to_drive"] is False
    assert any("MIDI Map mode is on" in r for r in state["unsafe_reasons"])
    assert "MIDI Map mode is on" in str(refusal(midi_map=True))


def test_midi_map_mode_unreadable_is_refused_not_assumed_off():
    state = state_of(switch_values={"MIDI Map Mode": None})
    assert state["midi_map_mode"] is None
    assert any("could not read whether MIDI Map mode is on" in r for r in state["unsafe_reasons"])


def test_computer_midi_keyboard_on_is_refused_because_letters_play_notes():
    state = state_of(computer_keyboard=True)
    assert state["computer_midi_keyboard"] is True
    assert any("Computer MIDI Keyboard is on" in r for r in state["unsafe_reasons"])


# ── dialogs ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "dialog, name, text",
    [
        (EXPORT_DIALOG, "Export Audio/Video", "Rendered Track"),
        (SAVE_PANEL, "Save", "Save As:"),
        (CRASH_RECOVERY, "(untitled)", "Live quit unexpectedly"),
        (SAVE_CHANGES, "(untitled)", "Save changes to"),
    ],
    ids=["export", "save_panel", "crash_recovery", "save_changes"],
)
def test_dialog_open_is_reported_with_its_text_and_refused(dialog, name, text):
    state = state_of(dialogs=[dialog])
    assert [d["name"] for d in state["dialogs"]] == [name]
    assert any(text in line for line in state["dialogs"][0]["text"])
    assert state["dialogs"][0]["buttons"] == dialog["buttons"]
    assert state["safe_to_drive"] is False
    error = refusal(dialogs=[dialog])
    assert "Live is showing a dialog" in str(error) and text in str(error)


def test_dialog_sheet_on_the_set_window_is_reported():
    state = state_of(main_sheets=["Replace it?"])
    assert state["dialogs"][0]["text"] == ["Replace it?"]
    assert state["dialogs"][0]["sheets"] == 1 and state["safe_to_drive"] is False


def test_dialog_untitled_empty_window_is_a_menu_not_a_dialog():
    menu = raw_window("", subrole="AXUnknown")
    state = state_of(dialogs=[menu])
    assert state["dialogs"] == [] and state["safe_to_drive"] is True
    assert state["other_windows"] == [{"name": "(untitled)", "subrole": "AXUnknown"}]


def test_dialog_modal_window_by_any_name_is_a_dialog():
    odd = raw_window("Something Else", subrole="AXStandardWindow", modal=True)
    assert [d["name"] for d in state_of(dialogs=[odd])["dialogs"]] == ["Something Else"]


def test_dialog_focus_is_reported_as_dialog_not_a_view():
    # the chain ends in a window that is not the set window
    chain = [{"role": "AXButton", "description": "Save", "name": "Save"},
             {"role": "AXWindow", "description": "dialog", "name": ""}]
    assert u.classify_focus(chain, "Song") == {"view": "dialog", "element": "Save"}


# ── transport ────────────────────────────────────────────────────


def test_playing_is_reported_and_refused():
    state = state_of(playing=True)
    assert state["transport"]["playing"] is True
    assert "Live is playing; stop playback first" in state["unsafe_reasons"]
    assert "Live is playing" in str(refusal(playing=True))


@pytest.mark.parametrize("over", [{"recording": True}, {"session_record": True}],
                         ids=["arrangement_record", "session_record"])
def test_recording_is_reported_and_refused(over):
    state = state_of(**over)
    assert state["transport"]["recording"] is True
    assert any("Live is recording" in r for r in state["unsafe_reasons"])


def test_recording_unreadable_is_refused():
    state = state_of(switch_values={"Arrangement Record": None})
    assert state["transport"]["recording"] is None
    assert "could not read whether Live is recording" in state["unsafe_reasons"]


def test_playing_unreadable_is_refused_unless_transport_is_ignored():
    state = state_of(switch_values={"Play": None})
    assert "could not read whether Live is playing" in state["unsafe_reasons"]
    assert u.unsafe_reasons(state, ignore_transport=True) == []


def test_playing_is_allowed_when_the_tool_ignores_the_transport():
    state = u.require_safe_to_drive(FakeUI(playing=True), ignore_transport=True)
    assert state["transport"]["playing"] is True


def test_loop_on_is_reported_but_not_unsafe():
    state = state_of(loop=True)
    assert state["transport"]["loop"] is True and state["safe_to_drive"] is True


def test_transport_snapshot_and_compare_name_what_changed():
    before = u.transport_snapshot(state_of())
    after = u.transport_snapshot(state_of(playing=True, loop=True))
    assert u.compare_transport(before, before) == []
    assert u.compare_transport(before, after) == [
        "playing changed from False to True", "loop changed from False to True"]


# ── focus ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "group, view",
    [("Arrangement", "arrangement"), ("Session", "session"), ("Mixer", "mixer"),
     ("Device Detail", "detail"),
     ("Clip Detail", "detail"), ("Browser", "browser"), ("Control", "control")],
)
def test_focus_view_comes_from_the_nearest_named_group(group, view):
    state = state_of(focus_group=group)
    assert state["focus"]["view"] == view


def test_focus_unknown_group_is_reported_unknown_not_guessed():
    state = state_of(focus_group="Something New")
    assert state["focus"]["view"] == "unknown"
    assert "not inside a view" in state["focus"]["reason"]


def test_focus_nothing_focused_is_unknown_with_a_reason():
    assert u.classify_focus([], "Song") == {
        "view": "unknown", "element": None, "reason": "no element has accessibility focus"}


# ── time selection ───────────────────────────────────────────────


def test_time_selection_present_when_edit_time_commands_are_enabled():
    state = state_of(selection=True)
    assert state["time_selection"]["present"] is True
    assert "Cut Time" in state["time_selection"]["commands_enabled"]


def test_time_selection_cannot_be_told_while_another_view_has_focus():
    state = state_of(focus_group="Mixer")
    assert state["time_selection"]["present"] is None
    assert "keyboard focus" in state["time_selection"]["reason"]


def test_time_selection_unreadable_edit_menu_is_null():
    state = state_of(edit_unreadable=True)
    assert state["time_selection"]["present"] is None and state["items_selected"] is None


def test_items_selected_reports_what_delete_would_remove():
    assert state_of(items_selected=True)["items_selected"] is True


# ── armed tracks ─────────────────────────────────────────────────


def test_armed_tracks_come_from_the_reader():
    assert state_of()["armed_tracks"] == ARMED


def test_armed_tracks_none_with_a_reason_when_the_socket_fails():
    def broken():
        raise ConnectionError("Live is not answering on port 9877")

    state = u.read_ui_state(FakeUI(), armed_reader=broken)
    assert state["armed_tracks"] is None
    assert "Live is not answering on port 9877" in state["armed_tracks_error"]
    assert state["safe_to_drive"] is True  # armed tracks are reported, they do not block clicks


def test_armed_tracks_none_when_no_reader_is_given():
    state = u.read_ui_state(FakeUI())
    assert state["armed_tracks"] is None and "Remote Script" in state["armed_tracks_error"]


def test_armed_tracks_empty_list_when_nothing_is_armed():
    assert u.read_ui_state(FakeUI(), armed_reader=lambda: [])["armed_tracks"] == []


# ── unreadable ───────────────────────────────────────────────────


def test_unreadable_set_window_is_refused():
    state = state_of(no_main=True)
    assert state["key_map_mode"] is None
    assert any("could not read the set window" in r or "accessibility could not read the set window" in r
               for r in state["unsafe_reasons"])


def test_unreadable_windows_are_refused():
    state = state_of(unreadable=["windows"])
    assert "accessibility could not read windows" in state["unsafe_reasons"]


def test_unreadable_guard_raises_instead_of_assuming_safe():
    error = refusal(no_main=True)
    assert error.reasons and "set window" in str(error)


def test_unreadable_driver_error_propagates_to_the_caller():
    class Broken:
        def call(self, op, **params):
            raise RuntimeError("osascript timed out")

    with pytest.raises(RuntimeError, match="osascript timed out"):
        u.require_safe_to_drive(Broken())


def test_unreadable_system_events_error_is_not_read_as_live_not_running():
    class Busy:
        def call(self, op, **params):
            return {"ok": False, "error": "System Events got an error: AppleEvent timed out. (-1712)"}

    state = u.read_ui_state(Busy())
    assert state["ok"] is False and state["running"] is None
    assert state["safe_to_drive"] is False
    assert state["unsafe_reasons"] == [
        "could not read Live's UI through accessibility: System Events got an error: "
        "AppleEvent timed out. (-1712)"]
    with pytest.raises(u.UnsafeUIState, match="could not read Live's UI"):
        u.require_safe_to_drive(Busy())
