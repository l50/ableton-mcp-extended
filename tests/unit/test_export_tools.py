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

from MCP_Server import live_export, server  # noqa: E402

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

    def _run(tool, fake=None, export_result=None, export_error=None, **kwargs):
        fake = fake or FakeAbleton({"get_arrangement_info": ARRANGEMENT})
        seen = {}

        def fake_export(request):
            seen["request"] = request
            seen["calls_at_export"] = list(fake.calls)
            if export_error:
                raise export_error
            return dict(export_result or {"ok": True, "status": "success",
                                          "output_path": request.output_file, "warnings": []})

        kwargs.setdefault("output_path", str(tmp_path / "mix.wav"))
        with patch.object(server, "get_ableton_connection", return_value=fake), \
                patch.object(live_export, "export_from_live", side_effect=fake_export):
            out = json.loads(tool(CTX, **kwargs))
        return out, fake, seen

    return _run


def with_transport(**changes):
    return dict(ARRANGEMENT, transport=dict(ARRANGEMENT["transport"], **changes))


def test_arrangement_range_selects_whole_arrangement_and_restores_loop(run, tmp_path):
    out, fake, seen = run(server.export_audio, output_path=str(tmp_path / "mix"), format="flac",
                          sample_rate=96000, bit_depth=16, normalize=True)
    assert out["ok"] is True
    assert out["render_range"] == "arrangement"
    assert seen["calls_at_export"] == [
        ("get_arrangement_info", {"track_index": -1}),
        ("set_arrangement_loop", {"enabled": True, "start": 0.0, "length": 64.5}),
        ("set_view", {"view_name": "Arranger"}),
    ]
    assert fake.calls[-1] == ("set_arrangement_loop", {"enabled": True, "start": 16.0, "length": 8.0})
    req = seen["request"]
    assert (req.format, req.sample_rate, req.bit_depth, req.normalize) == ("flac", 96000, 16, True)
    assert req.select_loop is True
    assert req.create_video is False
    assert req.output_file == str(tmp_path / "mix.flac")
    # 64.5 beats at 120 BPM: what the rendered file is checked against.
    assert req.expected_seconds == 32.25


def test_defaults(run):
    out, fake, seen = run(server.export_audio)
    req = seen["request"]
    assert (req.format, req.sample_rate, req.bit_depth) == ("wav", 44100, 24)
    assert (req.normalize, req.overwrite, req.render_timeout) == (False, False, 900.0)


def test_loop_range_reads_the_loop_and_leaves_the_brace_alone(run):
    out, fake, seen = run(server.export_audio, render_range="Loop")
    assert out["ok"] is True and out["render_range"] == "loop"
    assert fake.calls == [
        ("get_arrangement_info", {"track_index": -1}),
        ("set_view", {"view_name": "Arranger"}),
    ]
    assert seen["request"].select_loop is True
    # The 8-beat loop brace at 120 BPM.
    assert seen["request"].expected_seconds == 4.0


def test_expected_length_follows_the_tempo(run):
    fake = FakeAbleton({"get_arrangement_info": with_transport(tempo=90.0)})
    out, fake, seen = run(server.export_audio, fake=fake)
    assert seen["request"].expected_seconds == 43.0


def test_selection_range_touches_nothing_over_the_lom(run):
    out, fake, seen = run(server.export_audio, render_range="selection", overwrite=True,
                          render_timeout=60)
    assert out["ok"] is True
    assert fake.calls == []
    req = seen["request"]
    assert req.select_loop is False
    assert (req.overwrite, req.render_timeout) == (True, 60.0)
    assert req.expected_seconds is None


@pytest.mark.parametrize("render_range", ["arrangement", "loop"])
def test_session_clips_overriding_the_arrangement_stop_the_export(run, render_range):
    fake = FakeAbleton({"get_arrangement_info": with_transport(back_to_arranger=True)})
    out, fake, seen = run(server.export_audio, fake=fake, render_range=render_range)
    assert out["ok"] is False
    assert "Session clips are overriding the Arrangement" in out["error"]
    assert out["render_range"] == render_range
    assert fake.calls == [("get_arrangement_info", {"track_index": -1})]
    assert "request" not in seen


def test_missing_folder_fails_before_touching_live(run, tmp_path):
    out, fake, seen = run(server.export_audio, output_path=str(tmp_path / "nope" / "mix.wav"))
    assert out["ok"] is False
    assert "output folder does not exist" in out["error"]
    assert fake.calls == [] and "request" not in seen


def test_existing_file_without_overwrite_fails_before_touching_live(run, tmp_path):
    (tmp_path / "mix.wav").write_bytes(b"keep")
    out, fake, seen = run(server.export_audio)
    assert out["ok"] is False
    assert "output file already exists" in out["error"]
    assert fake.calls == [] and "request" not in seen
    assert (tmp_path / "mix.wav").read_bytes() == b"keep"


def test_not_macos_fails_before_touching_live(run, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    out, fake, seen = run(server.export_audio)
    assert out["ok"] is False
    assert "not supported on 'linux'" in out["error"]
    assert fake.calls == [] and "request" not in seen


def test_export_video_turns_on_create_video(run):
    out, fake, seen = run(server.export_video, render_range="selection")
    assert seen["request"].create_video is True
    assert seen["request"].render_timeout == 1800.0


def test_bad_render_range(run):
    out, fake, seen = run(server.export_audio, render_range="everything")
    assert out["ok"] is False
    assert "render_range must be one of arrangement, loop, selection" in out["error"]
    assert fake.calls == [] and "request" not in seen


def test_invalid_request_fails_before_touching_live(run):
    out, fake, seen = run(server.export_audio, output_path="relative.wav")
    assert out["ok"] is False
    assert "output_path must be absolute" in out["error"]
    assert fake.calls == [] and "request" not in seen
    assert out["render_range"] == "arrangement"


def test_empty_arrangement(run):
    fake = FakeAbleton({"get_arrangement_info": {"transport": {}, "tracks": []}})
    out, fake, seen = run(server.export_audio, fake=fake)
    assert out["ok"] is False
    assert "Arrangement has no clips" in out["error"]
    assert [c for c, _ in fake.calls] == ["get_arrangement_info"]
    assert "request" not in seen


def test_export_failure_result_passes_through_and_loop_is_restored(run):
    failure = {"ok": False, "status": "failed", "error": "no output file was produced", "warnings": []}
    out, fake, seen = run(server.export_audio, export_result=failure)
    assert out["ok"] is False
    assert out["error"] == "no output file was produced"
    assert fake.calls[-1][0] == "set_arrangement_loop"
    assert fake.calls[-1][1]["start"] == 16.0


def test_exporter_exception_becomes_failure_json(run):
    out, fake, seen = run(server.export_audio, export_error=RuntimeError("osascript died"))
    assert out["ok"] is False
    assert out["status"] == "failed"
    assert "osascript died" in out["error"]
    assert out["output_files"] == []
    assert fake.calls[-1][1] == {"enabled": True, "start": 16.0, "length": 8.0}


def test_lom_failure_becomes_failure_json(run):
    fake = FakeAbleton(fail_on={"get_arrangement_info": ConnectionError("Not connected to Ableton")})
    out, fake, seen = run(server.export_audio, fake=fake)
    assert out["ok"] is False
    assert "Not connected to Ableton" in out["error"]
    assert "request" not in seen


def test_loop_restore_failure_is_a_warning(run):
    fake = FakeAbleton({"get_arrangement_info": ARRANGEMENT},
                       fail_on={("set_arrangement_loop", 4): RuntimeError("socket closed")})
    out, fake, seen = run(server.export_audio, fake=fake)
    assert out["ok"] is True
    assert out["warnings"] == ["could not restore the loop brace: socket closed"]


def test_arrangement_end_beats():
    assert server._arrangement_end_beats(ARRANGEMENT) == 64.5
    assert server._arrangement_end_beats({}) == 0.0


def test_load_live_export_module_falls_back_to_script_import(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 1 and fromlist and "live_export" in fromlist:
            raise ImportError("no package")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setitem(sys.modules, "live_export", live_export)
    assert server._load_live_export_module() is live_export
