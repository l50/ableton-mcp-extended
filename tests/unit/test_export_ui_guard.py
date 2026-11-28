"""The export tools and the exporter refuse in an unsafe UI state, and clean up after.

Issue #20: with Key Map mode on, a dialog open or playback running, export
must refuse instead of clicking. Issue #12: after any export the arrangement
has no time selection and the transport is as it was. These run the real
LiveExporter and tool functions against the FakeLive of test_live_export.py.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from MCP_Server import connection, live_export as le, tools  # noqa: E402
from tests.unit.test_live_export import (  # noqa: E402
    WAV_24, FakeClock, afinfo_for, fake_window, make, request)

CTX = MagicMock()
ARRANGEMENT = {
    "transport": {"tempo": 120.0, "loop_enabled": False, "loop_start": 0.0, "loop_length": 8.0,
                  "back_to_arranger": False},
    "tracks": [{"index": 0, "arrangement_clips": [{"start_time": 0.0, "end_time": 6.0}]}],
}

UNSAFE = {
    "key_map": ({"key_map": True}, "Key Map mode is on"),
    "midi_map": ({"midi_map": True}, "MIDI Map mode is on"),
    "playing": ({"playing": True}, "Live is playing"),
    "recording": ({"recording": True}, "Live is recording"),
}
UNSAFE_IDS = list(UNSAFE)


@pytest.fixture
def clock():
    return FakeClock()


class FakeAbleton:
    def __init__(self):
        self.calls = []

    def send_command(self, command_type, params=None):
        self.calls.append((command_type, params))
        if command_type == "get_arrangement_info":
            return ARRANGEMENT
        if command_type == "get_recording_status":
            return {"armed_tracks": [{"index": 0, "name": "Guitar"}]}
        return {}


def run_tool(tool, clock, tmp_path, monkeypatch, **behaviour):
    """Call an export tool with a real LiveExporter driving a FakeLive."""
    monkeypatch.setattr(sys, "platform", "darwin")
    live, exporter = make(clock, afinfo=afinfo_for(48000, WAV_24), **behaviour)
    fake = FakeAbleton()
    with patch.object(connection, "get_ableton_connection", return_value=fake), \
            patch.object(le, "LiveExporter", return_value=exporter):
        out = json.loads(tool(CTX, output_path=str(tmp_path / "mix.wav"), sample_rate=48000))
    return out, live, fake


# ── the tools refuse before any click ────────────────────────────


@pytest.mark.parametrize("tool_name", ["export_audio", "export_video"])
@pytest.mark.parametrize("behaviour, reason", list(UNSAFE.values()), ids=UNSAFE_IDS)
def test_tool_refuses_before_any_click_in_an_unsafe_state(
        tool_name, behaviour, reason, clock, tmp_path, monkeypatch):
    tool = getattr(tools.export, tool_name)
    out, live, fake = run_tool(tool, clock, tmp_path, monkeypatch, **behaviour)
    assert out["ok"] is False
    assert "Live's UI is not safe to drive" in out["error"] and reason in out["error"]
    assert "Nothing was clicked" in out["error"]
    # nothing was sent to Live's UI, and the loop brace was not touched
    assert live.driven("activate", "menu", "keystroke", "press", "set_toggle", "set_choice",
                       "save_panel", "cancel", "stop_transport") == []
    assert [c for c in fake.calls if c[0] == "set_arrangement_loop"] == []


@pytest.mark.parametrize("dialog", [
    fake_window("Some Dialog", "AXDialog", modal=True, texts=["Please wait"]),
    fake_window("Export Audio/Video", "AXDialog"),
    fake_window("Save", "AXDialog", modal=True),
], ids=["modal", "export_dialog", "save_panel"])
def test_tool_refuses_with_a_dialog_open_and_clicks_nothing(dialog, clock, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    live, exporter = make(clock)
    live.windows.insert(0, dialog)
    with patch.object(connection, "get_ableton_connection", return_value=FakeAbleton()), \
            patch.object(le, "LiveExporter", return_value=exporter):
        out = json.loads(tools.export.export_audio(CTX, output_path=str(tmp_path / "mix.wav")))
    assert out["ok"] is False and "dialog" in out["error"]
    assert live.driven("activate", "menu", "keystroke", "press", "set_toggle", "set_choice",
                       "save_panel", "cancel") == []


# ── the exporter itself refuses, so no caller can skip the guard ─


@pytest.mark.parametrize("behaviour, reason", list(UNSAFE.values()), ids=UNSAFE_IDS)
def test_preflight_refuses_in_an_unsafe_state(behaviour, reason, clock):
    live, exporter = make(clock, **behaviour)
    with pytest.raises(le.ExportError) as caught:
        exporter.preflight()
    assert reason in str(caught.value) and "Nothing was clicked" in str(caught.value)
    assert live.driven("activate", "menu", "keystroke", "press", "stop_transport") == []


@pytest.mark.parametrize("behaviour, reason", list(UNSAFE.values()), ids=UNSAFE_IDS)
def test_preflight_via_run_refuses_and_touches_nothing(behaviour, reason, clock, tmp_path):
    live, exporter = make(clock, **behaviour)
    result = exporter.run(request(tmp_path))
    assert result["ok"] is False and reason in result["error"]
    assert live.driven("activate", "menu", "keystroke", "press", "stop_transport") == []
    assert "selection_cleared" not in result  # nothing was done, nothing to clean up


def test_preflight_passes_and_remembers_the_transport_in_a_safe_state(clock):
    live, exporter = make(clock, loop_on=True)
    assert exporter.preflight() == []
    assert exporter.transport_before["loop"] is True and exporter.transport_before["playing"] is False


def test_preflight_names_armed_tracks_as_a_warning():
    clock = FakeClock()
    live, exporter = make(clock)
    exporter.armed_reader = lambda: [{"name": "Guitar"}, {"name": "Bass"}]
    assert exporter.preflight() == ["tracks are armed in Live: Guitar, Bass"]


def test_preflight_unreadable_ui_refuses_instead_of_assuming_safe(clock):
    live, exporter = make(clock, ui_state_fails=True)
    with pytest.raises(le.ExportError, match="could not read Live's UI through accessibility"):
        exporter.preflight()


def test_preflight_no_audio_device_leaves_nothing_to_clean_up(clock, tmp_path):
    live, exporter = make(clock, audio_device="No Device", time_selection=True)
    result = exporter.run(request(tmp_path))
    assert result["ok"] is False and "no audio output device" in result["error"]
    assert live.time_selection is True  # refused before touching anything


# ── the selection and the transport after an export ──────────────


def test_export_leaves_no_selection_success_path(clock, tmp_path):
    live, exporter = make(clock)
    result = exporter.run(request(tmp_path, select_loop=True))
    assert result["ok"] is True, result
    assert live.time_selection is False
    assert result["selection_cleared"] is True and result["transport_unchanged"] is True
    assert [m["path"] for m in live.driven("menu")][-1] == ["Playback", "Move Insert Marker To Playhead"]


def test_export_leaves_no_selection_success_path_even_for_a_selection_made_by_the_user(clock, tmp_path):
    live, exporter = make(clock, time_selection=True)
    result = exporter.run(request(tmp_path, select_loop=False))
    assert result["ok"] is True, result
    assert live.time_selection is False and result["selection_cleared"] is True


def test_export_leaves_no_selection_success_path_and_playback_a_click_started_is_stopped(clock, tmp_path):
    live, exporter = make(clock, marker_starts_playback=True)
    result = exporter.run(request(tmp_path, select_loop=True))
    assert result["ok"] is True
    assert live.playing is False and result["transport_unchanged"] is True
    assert "playback started during the export and was stopped" in result["warnings"]


def test_export_leaves_no_selection_failure_path(clock, tmp_path):
    live, exporter = make(clock, dialog_appears=False)
    result = exporter.run(request(tmp_path, select_loop=True))
    assert result["ok"] is False and result["export_pending"] is False
    assert live.time_selection is False
    assert result["selection_cleared"] is True and result["transport_unchanged"] is True


@pytest.mark.parametrize(
    "behaviour",
    [
        {"save_panel_times_out": True, "uncancellable": (le.SAVE_PANEL_TITLE,)},
        {"window_stays": True},
    ],
    ids=["save_panel_still_open", "render_progress_still_open"],
)
def test_export_leaves_no_selection_pending_path_reports_it_could_not_clean_up(
    behaviour, clock, tmp_path
):
    live, exporter = make(clock, **behaviour)
    result = exporter.run(request(tmp_path, select_loop=True))
    assert result["export_pending"] is True
    assert live.time_selection is True
    assert result["selection_cleared"] is False
    assert result["transport_unchanged"] is None
    assert [m["path"] for m in live.driven("menu") if m["path"][0] == "Playback"] == []
    assert any("Live was not left clean" in w and "not safe to touch" in w for w in result["warnings"])


def test_export_leaves_no_selection_failure_path_when_the_cleanup_cannot_run(clock, tmp_path):
    live, exporter = make(clock)
    original = live.op_ui_state
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) > 1:
            return {"ok": False, "error": "System Events got an error: AppleEvent timed out. (-1712)",
                    "code": -1712}
        return original()

    live.op_ui_state = flaky
    result = exporter.run(request(tmp_path, select_loop=True))
    assert result["ok"] is True  # the file is what decides the render
    assert result["selection_cleared"] is False
    assert any("Live was not left clean" in w for w in result["warnings"])


def test_export_transport_change_during_the_export_is_reported_unchanged_false(clock, tmp_path):
    live, exporter = make(clock)
    original = live.op_menu

    def menu(path):
        reply = original(path)
        if path[0] == "File":
            live.loop_on = True  # something toggled the loop while the export ran
        return reply

    live.op_menu = menu
    result = exporter.run(request(tmp_path, select_loop=True))
    assert result["transport_unchanged"] is False
    assert any("loop changed from False to True" in w for w in result["warnings"])


def test_export_tool_reply_carries_the_cleanup_outcome(clock, tmp_path, monkeypatch):
    out, live, fake = run_tool(tools.export.export_audio, clock, tmp_path, monkeypatch)
    assert out["ok"] is True, out
    assert out["selection_cleared"] is True and out["transport_unchanged"] is True
    assert live.time_selection is False
