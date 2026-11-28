"""The MCP tools of the UI-state work: registration, replies and failures.

The accessibility layer is faked at the driver; nothing reaches osascript.
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

from MCP_Server import connection, live_save, live_ui_state, tools  # noqa: E402
from tests.unit.ui_fakes import FakeUI, raw_window  # noqa: E402

CTX = MagicMock()


class FakeAbleton:
    def __init__(self, armed=None, fail=None):
        self.armed = armed if armed is not None else []
        self.fail = fail
        self.calls = []

    def send_command(self, command_type, params=None):
        self.calls.append(command_type)
        if self.fail:
            raise self.fail
        return {"armed_tracks": self.armed}


@pytest.fixture
def live(monkeypatch):
    fake = FakeUI()
    monkeypatch.setattr(live_ui_state, "_default_driver", lambda: fake)
    return fake


def test_registered_tools_exist_with_their_documented_parameters():
    import inspect

    assert list(inspect.signature(tools.ui_state.get_ui_state).parameters) == ["ctx"]
    assert list(inspect.signature(tools.ui_state.clear_selection).parameters) == ["ctx"]
    assert list(inspect.signature(tools.save.save_set).parameters) == ["ctx"]
    params = inspect.signature(tools.save.save_set_as).parameters
    assert list(params) == ["ctx", "path", "overwrite"] and params["overwrite"].default is False
    for module in (tools.ui_state, tools.save):
        assert module in tools.TOOL_MODULES


def test_registered_tools_return_json_and_refuse_unsafe_state(monkeypatch, tmp_path):
    unsafe = FakeUI(key_map=True)
    monkeypatch.setattr(live_ui_state, "_default_driver", lambda: unsafe)
    with patch.object(connection, "get_ableton_connection", return_value=FakeAbleton()):
        state = json.loads(tools.ui_state.get_ui_state(CTX))
        cleared = json.loads(tools.ui_state.clear_selection(CTX))
        saved = json.loads(tools.save.save_set(CTX))
        saved_as = json.loads(
            tools.save.save_set_as(CTX, str(tmp_path / "Out.als"), overwrite=False)
        )
    assert state["ok"] is True and state["safe_to_drive"] is False
    for refused in (cleared, saved, saved_as):
        assert refused["ok"] is False and refused["unsafe_reasons"]
    assert unsafe.clicks() == []


def test_get_ui_state_returns_the_state_with_armed_tracks_one_based(live):
    fake = FakeAbleton(armed=[{"index": 0, "name": "Guitar", "arm": True}])
    with patch.object(connection, "get_ableton_connection", return_value=fake):
        out = json.loads(tools.ui_state.get_ui_state(CTX))
    assert out["ok"] is True and out["safe_to_drive"] is True
    assert out["armed_tracks"] == [{"index": 1, "name": "Guitar", "arm": True}]
    assert fake.calls == ["get_recording_status"]


def test_get_ui_state_reports_without_refusing_in_key_map_mode(monkeypatch):
    fake = FakeUI(key_map=True)
    monkeypatch.setattr(live_ui_state, "_default_driver", lambda: fake)
    with patch.object(connection, "get_ableton_connection", return_value=FakeAbleton()):
        out = json.loads(tools.ui_state.get_ui_state(CTX))
    assert out["ok"] is True and out["key_map_mode"] is True and out["safe_to_drive"] is False
    assert fake.clicks() == []


def test_get_ui_state_armed_tracks_null_when_the_socket_is_down(live):
    with patch.object(connection, "get_ableton_connection", side_effect=ConnectionError("no Live")):
        out = json.loads(tools.ui_state.get_ui_state(CTX))
    assert out["armed_tracks"] is None and "no Live" in out["armed_tracks_error"]


def test_get_ui_state_driver_failure_is_a_json_error(monkeypatch):
    class Broken:
        def call(self, op, **params):
            raise RuntimeError("osascript timed out")

    monkeypatch.setattr(live_ui_state, "_default_driver", lambda: Broken())
    out = json.loads(tools.ui_state.get_ui_state(CTX))
    assert out["ok"] is False and "osascript timed out" in out["error"]


def test_clear_selection_tool_clears_and_reports(monkeypatch):
    fake = FakeUI(selection=True)
    monkeypatch.setattr(live_ui_state, "_default_driver", lambda: fake)
    with patch.object(connection, "get_ableton_connection", return_value=FakeAbleton()):
        out = json.loads(tools.ui_state.clear_selection(CTX))
    assert out["ok"] is True and out["time_selection_after"] == {"present": False}


def test_clear_selection_tool_refuses_in_an_unsafe_state_with_the_reasons(monkeypatch):
    fake = FakeUI(selection=True, dialogs=[raw_window("Export Audio/Video")])
    monkeypatch.setattr(live_ui_state, "_default_driver", lambda: fake)
    with patch.object(connection, "get_ableton_connection", return_value=FakeAbleton()):
        out = json.loads(tools.ui_state.clear_selection(CTX))
    assert out["ok"] is False and out["unsafe_reasons"]
    assert "nothing was clicked" in out["error"] and fake.clicks() == []


def test_clear_selection_tool_unexpected_error_is_a_json_error(monkeypatch):
    class Broken:
        def call(self, op, **params):
            raise RuntimeError("boom")

    monkeypatch.setattr(live_ui_state, "_default_driver", lambda: Broken())
    out = json.loads(tools.ui_state.clear_selection(CTX))
    assert out["ok"] is False and "boom" in out["error"]


def test_save_set_tool_returns_the_save_result(monkeypatch, tmp_path):
    als = tmp_path / "S.als"
    als.write_bytes(b"a")
    os.utime(als, (1_700_000_000, 1_700_000_000))
    fake = FakeUI(document=als.as_uri(), title="S", on_save=lambda: als.write_bytes(b"bb"))
    monkeypatch.setattr(live_ui_state, "_default_driver", lambda: fake)
    with patch.object(connection, "get_ableton_connection", return_value=FakeAbleton()):
        out = json.loads(tools.save.save_set(CTX))
    assert out["ok"] is True and out["path"] == str(als) and out["mtime"] > out["mtime_before"]


def test_save_set_tool_refuses_with_a_dialog_open(monkeypatch):
    fake = FakeUI(dialogs=[raw_window("Save", modal=True)])
    monkeypatch.setattr(live_ui_state, "_default_driver", lambda: fake)
    with patch.object(connection, "get_ableton_connection", return_value=FakeAbleton()):
        out = json.loads(tools.save.save_set(CTX))
    assert out["ok"] is False and "'Save'" in out["error"] and fake.clicks() == []


def test_save_set_tool_unexpected_error_is_a_json_error(monkeypatch):
    monkeypatch.setattr(live_save, "save_set", MagicMock(side_effect=RuntimeError("boom")))
    out = json.loads(tools.save.save_set(CTX))
    assert out["ok"] is False and out["saved"] is False and "boom" in out["error"]


def test_save_set_as_tool_passes_path_and_overwrite(monkeypatch):
    seen = {}

    def fake_save_as(path, overwrite=False, **kw):
        seen.update(path=path, overwrite=overwrite)
        return {"ok": True, "saved": True, "path": path}

    monkeypatch.setattr(live_save, "save_set_as", fake_save_as)
    out = json.loads(tools.save.save_set_as(CTX, "/tmp/x.als", overwrite=True))
    assert out["ok"] is True and seen == {"path": "/tmp/x.als", "overwrite": True}


def test_save_set_as_tool_unexpected_error_is_a_json_error(monkeypatch):
    monkeypatch.setattr(live_save, "save_set_as", MagicMock(side_effect=RuntimeError("boom")))
    out = json.loads(tools.save.save_set_as(CTX, "/tmp/x.als"))
    assert out["ok"] is False and "boom" in out["error"]
