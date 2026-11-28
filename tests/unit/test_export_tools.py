"""MCP-side tests for the export_audio and export_video tools.

The UI automation (live_export.export_from_live) and the Ableton socket are
both patched, so these check how the tools prepare the render range over the
LOM, what request they hand to the exporter, and that failures come back as
ok=false JSON.
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


if "MCP_Server.server" not in sys.modules:
    _mock_fastmcp = MagicMock()
    _mock_fastmcp.FastMCP.return_value.tool.return_value = lambda fn: fn
    sys.modules["mcp"] = MagicMock()
    sys.modules["mcp.server"] = MagicMock()
    sys.modules["mcp.server.fastmcp"] = _mock_fastmcp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from MCP_Server import live_export  # noqa: E402
from MCP_Server import connection, tools  # noqa: E402

CTX = MagicMock()

ARRANGEMENT = {
    "transport": {"tempo": 120.0, "loop_enabled": True, "loop_start": 16.0, "loop_length": 8.0,
                  "back_to_arranger": False},
    "tracks": [
        {"index": 0, "arrangement_clips": [{"start_time": 0.0, "end_time": 32.0}]},
        {"index": 1, "arrangement_clips": [{"start_time": 8.0, "end_time": 64.5},
                                           {"start_time": 2.0, "end_time": None}]},
        {"index": 2, "arrangement_clips": []},
    ],
}


class FakeAbleton:
    def __init__(self, replies=None, fail_on=None):
        self.replies = replies or {}
        self.fail_on = fail_on or {}
        self.calls = []

    def send_command(self, command_type, params=None):
        self.calls.append((command_type, params))
        failure = self.fail_on.get((command_type, len(self.calls)))
        if failure is None:
            failure = self.fail_on.get(command_type)
        if failure is not None:
            raise failure
        return self.replies.get(command_type, {})


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Call an export tool on macOS with a fake Ableton and a stubbed exporter."""
    monkeypatch.setattr(sys, "platform", "darwin")

    def _run(tool, fake=None, export_result=None, export_error=None, preflight_error=None, **kwargs):
        fake = fake or FakeAbleton({"get_arrangement_info": ARRANGEMENT})
        seen = {}
        exporter = MagicMock()
        if preflight_error:
            exporter.preflight.side_effect = preflight_error
        else:
            exporter.preflight.return_value = []

        def fake_export(request, given_exporter=None):
            seen["exporter"] = given_exporter
            seen["request"] = request
            seen["calls_at_export"] = list(fake.calls)
            if export_error:
                raise export_error
            return dict(export_result or {"ok": True, "status": "success",
                                          "output_path": request.output_file, "warnings": []})

        kwargs.setdefault("output_path", str(tmp_path / "mix.wav"))
        with patch.object(connection, "get_ableton_connection", return_value=fake), \
                patch.object(live_export, "LiveExporter", return_value=exporter), \
                patch.object(live_export, "export_from_live", side_effect=fake_export):
            out = json.loads(tool(CTX, **kwargs))
        seen["preflight_calls"] = exporter.preflight.call_count
        return out, fake, seen

    return _run


def with_transport(**changes):
    return dict(ARRANGEMENT, transport=dict(ARRANGEMENT["transport"], **changes))


def test_arrangement_range_is_computed_and_typed_without_touching_the_loop(run, tmp_path):
    out, fake, seen = run(tools.export.export_audio, output_path=str(tmp_path / "mix"), format="flac",
                          sample_rate=96000, bit_depth=16, normalize=True)
    assert out["ok"] is True
    assert out["render_range"] == "arrangement"
    # The set is only read: no loop move, no view change, nothing to restore.
    assert fake.calls == [("get_arrangement_info", {"track_index": -1})]
    assert seen["calls_at_export"] == fake.calls
    req = seen["request"]
    assert (req.format, req.sample_rate, req.bit_depth, req.normalize) == ("flac", 96000, 16, True)
    assert (req.start_beats, req.length_beats) == (0.0, 64.5)
    assert req.create_video is False
    assert req.output_file == str(tmp_path / "mix.flac")
    # 64.5 beats at 120 BPM: what the rendered file is checked against.
    assert req.expected_seconds == 32.25
    assert out["range_rounded"] is False


def test_defaults(run):
    out, fake, seen = run(tools.export.export_audio)
    req = seen["request"]
    assert (req.format, req.sample_rate, req.bit_depth) == ("wav", 44100, 24)
    assert (req.normalize, req.overwrite, req.render_timeout) == (False, False, 900.0)


def test_loop_range_reads_the_loop_and_leaves_the_brace_alone(run):
    out, fake, seen = run(tools.export.export_audio, render_range="Loop")
    assert out["ok"] is True and out["render_range"] == "loop"
    assert fake.calls == [("get_arrangement_info", {"track_index": -1})]
    # The 8-beat loop brace at 16, typed into the dialog, at 120 BPM.
    assert (seen["request"].start_beats, seen["request"].length_beats) == (16.0, 8.0)
    assert seen["request"].expected_seconds == 4.0


def test_expected_length_follows_the_tempo(run):
    fake = FakeAbleton({"get_arrangement_info": with_transport(tempo=90.0)})
    out, fake, seen = run(tools.export.export_audio, fake=fake)
    assert seen["request"].expected_seconds == 43.0


def test_selection_range_touches_nothing_over_the_lom(run):
    out, fake, seen = run(tools.export.export_audio, render_range="selection", overwrite=True,
                          render_timeout=60)
    assert out["ok"] is True
    assert fake.calls == []
    req = seen["request"]
    assert req.typed_range is False
    assert (req.overwrite, req.render_timeout) == (True, 60.0)
    assert req.expected_seconds is None


@pytest.mark.parametrize("render_range", ["arrangement", "loop"])
def test_session_clips_overriding_the_arrangement_stop_the_export(run, render_range):
    fake = FakeAbleton({"get_arrangement_info": with_transport(back_to_arranger=True)})
    out, fake, seen = run(tools.export.export_audio, fake=fake, render_range=render_range)
    assert out["ok"] is False
    assert "Session clips are overriding the Arrangement" in out["error"]
    assert out["render_range"] == render_range
    assert fake.calls == [("get_arrangement_info", {"track_index": -1})]
    assert "request" not in seen


def test_missing_folder_fails_before_touching_live(run, tmp_path):
    out, fake, seen = run(tools.export.export_audio, output_path=str(tmp_path / "nope" / "mix.wav"))
    assert out["ok"] is False
    assert "output folder does not exist" in out["error"]
    assert fake.calls == [] and "request" not in seen


def test_existing_file_without_overwrite_fails_before_touching_live(run, tmp_path):
    (tmp_path / "mix.wav").write_bytes(b"keep")
    out, fake, seen = run(tools.export.export_audio)
    assert out["ok"] is False
    assert "output file already exists" in out["error"]
    assert fake.calls == [] and "request" not in seen
    assert (tmp_path / "mix.wav").read_bytes() == b"keep"


def test_not_macos_fails_before_touching_live(run, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    out, fake, seen = run(tools.export.export_audio)
    assert out["ok"] is False
    assert "not supported on 'linux'" in out["error"]
    assert fake.calls == [] and "request" not in seen


def test_export_video_turns_on_create_video(run):
    out, fake, seen = run(tools.export.export_video, render_range="selection")
    assert seen["request"].create_video is True
    assert seen["request"].render_timeout == 1800.0


def test_bad_render_range(run):
    out, fake, seen = run(tools.export.export_audio, render_range="everything")
    assert out["ok"] is False
    assert "render_range must be one of arrangement, loop, selection" in out["error"]
    assert fake.calls == [] and "request" not in seen


def test_invalid_request_fails_before_touching_live(run):
    out, fake, seen = run(tools.export.export_audio, output_path="relative.wav")
    assert out["ok"] is False
    assert "output_path must be absolute" in out["error"]
    assert fake.calls == [] and "request" not in seen
    assert out["render_range"] == "arrangement"


def test_empty_arrangement(run):
    fake = FakeAbleton({"get_arrangement_info": {"transport": {}, "tracks": []}})
    out, fake, seen = run(tools.export.export_audio, fake=fake)
    assert out["ok"] is False
    assert "Arrangement has no clips" in out["error"]
    assert [c for c, _ in fake.calls] == ["get_arrangement_info"]
    assert "request" not in seen


def test_export_failure_result_passes_through_and_no_loop_command_is_sent(run):
    failure = {"ok": False, "status": "failed", "error": "no output file was produced", "warnings": []}
    out, fake, seen = run(tools.export.export_audio, export_result=failure)
    assert out["ok"] is False
    assert out["error"] == "no output file was produced"
    assert [c for c, _ in fake.calls] == ["get_arrangement_info"]


def test_exporter_exception_becomes_failure_json(run):
    out, fake, seen = run(tools.export.export_audio, export_error=RuntimeError("osascript died"))
    assert out["ok"] is False
    assert out["status"] == "failed"
    assert "osascript died" in out["error"]
    assert out["output_files"] == []
    assert [c for c, _ in fake.calls] == ["get_arrangement_info"]


def test_lom_failure_becomes_failure_json(run):
    fake = FakeAbleton(fail_on={"get_arrangement_info": ConnectionError("Not connected to Ableton")})
    out, fake, seen = run(tools.export.export_audio, fake=fake)
    assert out["ok"] is False
    assert "Not connected to Ableton" in out["error"]
    assert "request" not in seen


def test_the_exporter_is_built_with_the_armed_track_reader_and_reused(run):
    out, fake, seen = run(tools.export.export_audio)
    assert out["ok"] is True
    assert seen["exporter"] is not None


def test_pending_export_sends_nothing_to_the_set(run):
    pending = {"ok": False, "status": "failed", "warnings": [], "export_pending": True,
               "error": "UI scripting op 'save_panel' timed out after 45s. The export may still be pending"}
    out, fake, seen = run(tools.export.export_audio, export_result=pending)
    assert out["ok"] is False and out["export_pending"] is True
    # Only the read that worked the range out: nothing moved, so nothing to restore.
    assert [c for c, _ in fake.calls] == ["get_arrangement_info"]
    assert "loop_to_restore" not in out
    assert "loop brace" not in out["error"]


def test_arrangement_end_beats():
    assert tools.export._arrangement_end_beats(ARRANGEMENT) == 64.5
    assert tools.export._arrangement_end_beats({}) == 0.0



# ── nothing is moved or restored around a pending export ──


from tests.unit.test_live_export import WAV_AFINFO, FakeClock, FakeLive  # noqa: E402

SAVE_TIMEOUT = "UI scripting op 'save_panel' timed out after 45s"
READ_ONLY = {"get_arrangement_info", "list_tracks", "get_recording_status"}
UI_OPS = ("menu", "press", "set_choice", "set_toggle", "save_panel", "cancel", "set_time_field",
          "keystroke")


class WatchingAbleton(FakeAbleton):
    """FakeAbleton that notes which Live windows were open at each LOM command."""

    def __init__(self, live, **kw):
        super().__init__({"get_arrangement_info": ARRANGEMENT}, **kw)
        self.live = live
        self.windows_at = []

    def send_command(self, command_type, params=None):
        self.windows_at.append((command_type, list(self.live.names())))
        return super().send_command(command_type, params)


def _timeout_run(tmp_path, monkeypatch, cancel_closes=True, **behaviour):
    """export_audio over a FakeLive whose save_panel op times out with the panel still up."""
    monkeypatch.setattr(sys, "platform", "darwin")
    clock = FakeClock()
    live = FakeLive(clock, **behaviour)

    def timed_out(**params):
        live.calls.append(dict(params, op="save_panel"))
        raise live_export.UIScriptingError(SAVE_TIMEOUT)

    original_call = live.call

    def call(op, **params):
        if op == "save_panel":
            return timed_out(**params)
        if not cancel_closes and op == "cancel":
            live.calls.append(dict(params, op=op))
            raise live_export.UIScriptingError(f"UI scripting op '{op}' timed out after 45s")
        return original_call(op, **params)

    live.call = call
    exporter = live_export.LiveExporter(
        driver=live,
        clock=live_export._Clock(now=clock.now, monotonic=clock.monotonic, sleep=clock.sleep),
        afinfo=lambda path: WAV_AFINFO,
        platform="darwin",
        audio_device=lambda: "Fireface UCX II (20 In, 20 Out)",
    )
    fake = WatchingAbleton(live)
    with patch.object(connection, "get_ableton_connection", return_value=fake), \
            patch.object(live_export, "LiveExporter", return_value=exporter):
        out = json.loads(tools.export.export_audio(CTX, output_path=str(tmp_path / "mix.wav")))
    return out, fake, live


def test_save_dialog_timeout_sends_nothing_that_changes_the_set(tmp_path, monkeypatch):
    out, fake, live = _timeout_run(tmp_path, monkeypatch)
    assert out["ok"] is False
    assert SAVE_TIMEOUT in out["error"]
    assert {c for c, _ in fake.calls} <= READ_ONLY
    assert out["export_pending"] is False


def test_save_dialog_that_cannot_be_cancelled_leaves_everything_and_says_what_to_check(
        tmp_path, monkeypatch):
    out, fake, live = _timeout_run(tmp_path, monkeypatch, cancel_closes=False)
    assert out["ok"] is False
    assert live_export.SAVE_PANEL_TITLE in live.names()
    assert {c for c, _ in fake.calls} <= READ_ONLY
    assert out["export_pending"] is True
    assert "may still be pending" in out["error"]
    assert "Save" in out["error"]
    assert "loop_to_restore" not in out


def test_retry_while_a_pending_render_runs_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    clock = FakeClock()
    live = FakeLive(clock, window_stays=True)
    exporter = live_export.LiveExporter(
        driver=live,
        clock=live_export._Clock(now=clock.now, monotonic=clock.monotonic, sleep=clock.sleep),
        afinfo=lambda path: WAV_AFINFO,
        platform="darwin",
        audio_device=lambda: "Fireface UCX II (20 In, 20 Out)",
    )
    first, retry = WatchingAbleton(live), WatchingAbleton(live)
    with patch.object(live_export, "LiveExporter", return_value=exporter):
        with patch.object(connection, "get_ableton_connection", return_value=first):
            out = json.loads(tools.export.export_audio(
                CTX, output_path=str(tmp_path / "mix.wav"), render_timeout=6))
        assert out["export_pending"] is True
        driven_before = len(live.driven(*UI_OPS))
        with patch.object(connection, "get_ableton_connection", return_value=retry):
            again = json.loads(tools.export.export_audio(CTX, output_path=str(tmp_path / "again.wav")))
    assert again["ok"] is False
    assert "an earlier export did not finish cleanly" in again["error"]
    assert {c for c, _ in first.calls} <= READ_ONLY and {c for c, _ in retry.calls} <= READ_ONLY
    # The retry clicked and typed nothing in Live.
    assert len(live.driven(*UI_OPS)) == driven_before
