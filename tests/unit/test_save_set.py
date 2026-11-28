"""Tests for save_set and save_set_as (issue #21).

Live's UI is faked (FakeUI) and the .als file is a real file in tmp_path whose
modification time the fake bumps the way Live's save does, so the checks that
read the path and the time after the save run for real.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

if "MCP_Server.server" not in sys.modules and "mcp" not in sys.modules:
    _mock_fastmcp = MagicMock()
    _mock_fastmcp.FastMCP.return_value.tool.return_value = lambda fn: fn
    sys.modules["mcp"] = MagicMock()
    sys.modules["mcp.server"] = MagicMock()
    sys.modules["mcp.server.fastmcp"] = _mock_fastmcp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from MCP_Server import connection, live_export, live_save as ls, live_ui_state, tools  # noqa: E402
from tests.unit.test_live_export import WAV_24, FakeClock, afinfo_for, make  # noqa: E402
from tests.unit.ui_fakes import FakeUI, raw_window  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def timing(clock):
    return {"monotonic": clock.monotonic, "sleep": clock.sleep, "timeout": 5.0}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def als(tmp_path):
    path = tmp_path / "Song.als"
    path.write_bytes(b"old")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    return path


def saving(path, content=b"saved by live"):
    def on_save():
        path.write_bytes(content)  # a new modification time, as Live's save gives
    return on_save


def open_set(path, **over):
    return FakeUI(title=path.stem, document=path.as_uri(), **over)


# ── save_set ─────────────────────────────────────────────────────


def test_save_set_returns_path_and_mtime_read_after_the_save(als, clock):
    live = open_set(als, on_save=saving(als))
    result = ls.save_set(live, **timing(clock))
    assert result["ok"] is True and result["saved"] is True, result
    assert result["path"] == str(als)
    assert result["mtime"] == als.stat().st_mtime
    assert result["mtime_before"] == 1_700_000_000
    assert result["mtime"] > result["mtime_before"]
    assert result["size_bytes"] == len(b"saved by live")
    assert result["modified"].startswith("20")
    assert [c["path"] for c in live.calls if c["op"] == "menu"] == [["File", "Save Live Set"]]


@pytest.mark.parametrize("over, reason", [({"playing": True}, "Live is playing"),
                                          ({"recording": True}, "Live is recording")],
                         ids=["playing", "recording"])
def test_save_set_refuses_while_the_transport_runs_without_clicking(als, clock, over, reason):
    live = open_set(als, on_save=saving(als), **over)
    result = ls.save_set(live, **timing(clock))
    assert result["ok"] is False and reason in result["error"]
    assert live.clicks() == [] and als.read_bytes() == b"old"


def test_save_as_refuses_while_playing_without_clicking(tmp_path, clock):
    live = FakeUI(playing=True)
    result = ls.save_set_as(str(tmp_path / "A.als"), driver=live, **timing(clock))
    assert result["ok"] is False and "Live is playing" in result["error"]
    assert live.clicks() == []


def test_save_set_fails_when_the_modification_time_did_not_change(als, clock):
    live = open_set(als)  # File > Save does nothing here
    result = ls.save_set(live, **timing(clock))
    assert result["ok"] is False and result["saved"] is False
    assert "modification time" in result["error"] and "did not change" in result["error"]
    assert result["path"] == str(als)


def test_save_set_unchanged_mtime_names_a_dialog_live_is_showing(als, clock):
    live = open_set(als)
    original = live.op_menu

    def menu(path):
        reply = original(path)
        live.dialogs.append(raw_window("", modal=True, texts=["Could not save: disk is full"],
                                       buttons=["OK"]))
        return reply

    live.op_menu = menu
    result = ls.save_set(live, **timing(clock))
    assert result["ok"] is False and "disk is full" in result["error"]
    assert result["dialogs"]


def test_save_set_untitled_set_is_refused_and_points_to_save_set_as(clock):
    live = FakeUI(title="Untitled", document=None)
    result = ls.save_set(live, **timing(clock))
    assert result["ok"] is False
    assert "save_set_as(path)" in result["error"] and "'Untitled'" in result["error"]
    assert live.clicks() == []


def test_save_set_whose_file_is_missing_is_reported_not_saved(tmp_path, clock):
    gone = tmp_path / "Gone.als"
    live = open_set(gone)
    result = ls.save_set(live, **timing(clock))
    assert result["ok"] is False and "does not exist" in result["error"]
    assert live.clicks() == []


def test_save_set_a_menu_item_that_fails_is_reported(als, clock):
    live = open_set(als, menu_errors={("File", "Save Live Set"): "menu item 'File > Save Live Set' is disabled"})
    result = ls.save_set(live, **timing(clock))
    assert result["ok"] is False and "disabled" in result["error"]


def test_save_set_unmodified_set_is_reported_because_live_disables_save(als, clock):
    live = open_set(als, menu_errors={("File", "Save Live Set"): "menu item 'File > Save Live Set' is disabled"})
    original = live.op_menu
    live.op_menu = lambda path: dict(original(path), disabled=True) if path[1] == "Save Live Set" else original(path)
    result = ls.save_set(live, **timing(clock))
    assert result["ok"] is False and result["unmodified"] is True
    assert "unsaved changes" in result["error"] and als.read_bytes() == b"old"


def test_save_set_file_that_disappears_after_the_save_is_an_error(als, clock):
    def on_save():
        als.write_bytes(b"x")
        Path(als).unlink()

    live = open_set(als, on_save=on_save)
    result = ls.save_set(live, **timing(clock))
    assert result["ok"] is False


# ── refusals ─────────────────────────────────────────────────────


@pytest.mark.parametrize("over, reason", [
    ({"dialogs": [raw_window("Export Audio/Video")]}, "Export Audio/Video"),
    ({"dialogs": [raw_window("Save", modal=True)]}, "'Save'"),
    ({"main_sheets": ["Replace it?"]}, "Replace it?"),
    ({"key_map": True}, "Key Map mode"),
    ({"midi_map": True}, "MIDI Map mode"),
], ids=["export_dialog", "save_panel", "sheet", "key_map", "midi_map"])
def test_save_set_refuses_before_any_menu_click(als, clock, over, reason):
    live = open_set(als, on_save=saving(als), **over)
    result = ls.save_set(live, **timing(clock))
    assert result["ok"] is False and reason in result["error"]
    assert "nothing was clicked" in result["error"]
    assert result["unsafe_reasons"]
    assert live.clicks() == []
    assert als.read_bytes() == b"old"


def test_save_as_refuses_with_a_dialog_open_before_any_menu_click(tmp_path, clock):
    live = FakeUI(dialogs=[raw_window("Export Audio/Video")])
    result = ls.save_set_as(str(tmp_path / "New.als"), driver=live, **timing(clock))
    assert result["ok"] is False and "Export Audio/Video" in result["error"]
    assert live.clicks() == []


def test_save_as_refuses_in_key_map_mode_before_any_menu_click(tmp_path, clock):
    live = FakeUI(key_map=True)
    result = ls.save_set_as(str(tmp_path / "New.als"), driver=live, **timing(clock))
    assert result["ok"] is False and "Key Map mode" in result["error"]
    assert live.clicks() == []


# ── save_set_as ──────────────────────────────────────────────────


def test_save_as_writes_the_file_and_returns_path_and_mtime(tmp_path, clock):
    live = FakeUI(title="Untitled")
    target = tmp_path / "New Song.als"
    result = ls.save_set_as(str(target), driver=live, **timing(clock))
    assert result["ok"] is True and result["saved"] is True, result
    assert result["path"] == str(target)
    assert result["open_set_path"] == str(target) and result["set_title"] == "New Song"
    assert result["mtime"] == target.stat().st_mtime and result["size_bytes"] > 0
    panel = [c for c in live.calls if c["op"] == "save_panel"][0]
    assert panel["filename"] == "New Song" and panel["directory"] == str(tmp_path)
    assert panel["replace"] is False


def test_save_as_follows_live_into_the_project_folder_it_makes(tmp_path, clock):
    live = FakeUI(title="Untitled", project_folders=True)
    target = tmp_path / "New Song.als"
    result = ls.save_set_as(str(target), driver=live, **timing(clock))
    real = tmp_path / "New Song Project" / "New Song.als"
    assert result["ok"] is True, result
    assert result["path"] == str(real) and result["requested_path"] == str(target)
    assert result["in_project_folder"] is True and result["open_set_path"] == str(real)
    assert result["mtime"] == real.stat().st_mtime


def test_save_as_exact_path_is_not_reported_as_a_project_folder(tmp_path, clock):
    result = ls.save_set_as(str(tmp_path / "A.als"), driver=FakeUI(), **timing(clock))
    assert result["in_project_folder"] is False and result["requested_path"] == result["path"]


def test_save_as_existing_project_folder_file_is_not_overwritten_silently(tmp_path, clock):
    project = tmp_path / "A Project"
    project.mkdir()
    (project / "A.als").write_bytes(b"keep")
    live = FakeUI(project_folders=True)
    result = ls.save_set_as(str(tmp_path / "A.als"), driver=live, **timing(clock))
    assert result["ok"] is False and "A Project" in result["error"] and live.calls == []
    assert (project / "A.als").read_bytes() == b"keep"


def test_save_as_relative_path_is_rejected_without_touching_live(clock):
    live = FakeUI()
    result = ls.save_set_as("New.als", driver=live, **timing(clock))
    assert result["ok"] is False and "must be absolute" in result["error"] and live.calls == []


@pytest.mark.parametrize("name, message", [
    ("", "path is required"),
    ("song.wav", "must end in .als"),
    (".als", "must end in .als"),
], ids=["empty", "wrong_extension", "no_stem"])
def test_save_as_bad_path_is_rejected_without_touching_live(tmp_path, clock, name, message):
    live = FakeUI()
    path = str(tmp_path / name) if name else ""
    result = ls.save_set_as(path, driver=live, **timing(clock))
    assert result["ok"] is False and message in result["error"] and live.calls == []


def test_save_as_missing_folder_and_folder_path_are_rejected(tmp_path, clock):
    live = FakeUI()
    missing = ls.save_set_as(str(tmp_path / "nope" / "A.als"), driver=live, **timing(clock))
    assert "folder does not exist" in missing["error"]
    folder = ls.save_set_as(str(tmp_path) + os.sep, driver=live, **timing(clock))
    assert "must name a file" in folder["error"]
    assert live.calls == []


def test_save_as_existing_file_is_not_overwritten_silently(als, clock):
    live = FakeUI()
    result = ls.save_set_as(str(als), driver=live, **timing(clock))
    assert result["ok"] is False and "already exists" in result["error"] and live.calls == []
    assert als.read_bytes() == b"old"


def test_save_as_overwrite_true_passes_replace_to_the_panel(als, clock):
    live = FakeUI()
    result = ls.save_set_as(str(als), overwrite=True, driver=live, **timing(clock))
    assert result["ok"] is True, result
    assert [c for c in live.calls if c["op"] == "save_panel"][0]["replace"] is True


def test_save_as_overwrite_true_requires_mtime_to_advance_beyond_existing_file(tmp_path, clock):
    fresh = tmp_path / "Fresh.als"
    fresh.write_bytes(b"just written")
    live = FakeUI()
    live.op_save_panel = lambda **kw: {"ok": True}
    result = ls.save_set_as(str(fresh), overwrite=True, driver=live, **timing(clock))
    assert result["ok"] is False
    assert "was written within 5s" in result["error"]


def test_save_as_unwritable_folder_is_rejected(tmp_path, clock):
    folder = tmp_path / "ro"
    folder.mkdir()
    folder.chmod(0o500)
    try:
        if os.access(folder, os.W_OK):
            pytest.skip("running as a user that can write anywhere")
        result = ls.save_set_as(str(folder / "A.als"), driver=FakeUI(), **timing(clock))
        assert "not writable" in result["error"]
    finally:
        folder.chmod(0o700)


def test_save_as_panel_that_never_opens_fails_and_cancels(tmp_path, clock):
    live = FakeUI(save_panel_never_opens=True)
    result = ls.save_set_as(str(tmp_path / "A.als"), driver=live, **timing(clock))
    assert result["ok"] is False and "Save panel did not appear" in result["error"]
    assert "cancel" in live.ops()


def test_save_as_panel_error_is_reported_and_the_panel_cancelled(tmp_path, clock):
    live = FakeUI(save_panel_error="the Save panel could not open folder")
    result = ls.save_set_as(str(tmp_path / "A.als"), driver=live, **timing(clock))
    assert result["ok"] is False and "could not fill in the Save panel" in result["error"]
    assert "cancel" in live.ops() and not (tmp_path / "A.als").exists()


def test_save_as_menu_item_that_fails_is_reported(tmp_path, clock):
    live = FakeUI(menu_errors={("File", "Save Live Set As"): "menu item is disabled"})
    result = ls.save_set_as(str(tmp_path / "A.als"), driver=live, **timing(clock))
    assert result["ok"] is False and "disabled" in result["error"]


def test_save_as_fails_when_live_does_not_report_the_new_path_as_open(tmp_path, clock):
    live = FakeUI()
    original = live.op_save_panel

    def panel(**kw):
        reply = original(**kw)
        live.document = "file:///somewhere/else.als"
        return reply

    live.op_save_panel = panel
    result = ls.save_set_as(str(tmp_path / "A.als"), driver=live, **timing(clock))
    assert result["ok"] is False and "Live reports the open Set as /somewhere/else.als" in result["error"]


def test_save_as_fails_when_a_dialog_is_still_up_afterwards(tmp_path, clock):
    live = FakeUI()
    original = live.op_save_panel

    def panel(**kw):
        reply = original(**kw)
        live.dialogs.append(raw_window("", modal=True, texts=["Collect files?"], buttons=["No"]))
        return reply

    live.op_save_panel = panel
    result = ls.save_set_as(str(tmp_path / "A.als"), driver=live, **timing(clock))
    assert result["ok"] is False and "still showing a dialog" in result["error"]


def test_save_as_file_that_is_never_written_fails(tmp_path, clock):
    live = FakeUI()
    live.op_save_panel = lambda **kw: {"ok": True}
    result = ls.save_set_as(str(tmp_path / "A.als"), driver=live, **timing(clock))
    assert result["ok"] is False
    assert "neither" in result["error"] and "was written within 5s" in result["error"]


def test_save_as_unexpected_driver_error_is_a_failure_and_cancels(tmp_path, clock):
    live = FakeUI()

    def boom(**kw):
        raise RuntimeError("osascript timed out")

    live.op_save_panel = boom
    result = ls.save_set_as(str(tmp_path / "A.als"), driver=live, **timing(clock))
    assert result["ok"] is False and "unexpected save error: osascript timed out" in result["error"]
    assert "cancel" in live.ops()


# ── never called implicitly ──────────────────────────────────────


def test_never_called_by_other_ui_tools(tmp_path, monkeypatch):
    ctx = MagicMock()
    monkeypatch.setattr(sys, "platform", "darwin")
    fake_ui = FakeUI(selection=True)
    monkeypatch.setattr(live_ui_state, "_default_driver", lambda: fake_ui)
    ableton = MagicMock()
    ableton.send_command.side_effect = lambda cmd, params=None: (
        {
            "transport": {
                "tempo": 120.0,
                "loop_enabled": False,
                "loop_start": 0.0,
                "loop_length": 4.0,
                "back_to_arranger": False,
            },
            "tracks": [{"index": 0, "arrangement_clips": [{"start_time": 0.0, "end_time": 6.0}]}],
        }
        if cmd == "get_arrangement_info"
        else {"armed_tracks": []}
    )
    with patch.object(connection, "get_ableton_connection", return_value=ableton):
        assert json.loads(tools.ui_state.get_ui_state(ctx))["ok"] is True
        assert json.loads(tools.ui_state.clear_selection(ctx))["ok"] is True

        export_clock = FakeClock()
        fake_live_audio, audio_exporter = make(export_clock, afinfo=afinfo_for(48000, WAV_24))
        with patch.object(live_export, "LiveExporter", return_value=audio_exporter):
            audio_out = json.loads(
                tools.export.export_audio(
                    ctx, output_path=str(tmp_path / "audio.wav"), sample_rate=48000
                )
            )
        assert audio_out["ok"] is True

        fake_live_video, video_exporter = make(export_clock, afinfo=afinfo_for(48000, WAV_24))
        fake_live_video.controls[live_export.CREATE_VIDEO]["enabled"] = True
        fake_live_video.extra_render_files = [(str(tmp_path / "video.mp4"), b"\0" * 32)]
        with patch.object(live_export, "LiveExporter", return_value=video_exporter):
            video_out = json.loads(
                tools.export.export_video(
                    ctx, output_path=str(tmp_path / "video.wav"), sample_rate=48000
                )
            )
        assert video_out["ok"] is True

    menu_clicks = [
        c["path"]
        for driver in (fake_ui, fake_live_audio, fake_live_video)
        for c in driver.calls
        if c["op"] == "menu"
    ]
    assert not any(
        path[0] == ls.FILE_MENU and path[1] in (ls.SAVE_MENU_ITEM, ls.SAVE_AS_MENU_ITEM)
        for path in menu_clicks
    )


def test_never_called_the_save_functions_are_only_reachable_through_the_save_tool_module():
    assert tools.save.live_save is ls
    assert callable(tools.save.save_set) and callable(tools.save.save_set_as)
