"""MCP-side tests for the recording-safety tools.

The Ableton connection is patched, so these check what each tool sends
(1-based in, 0-based on the wire) and how it shapes the reply, without a
running Live instance.
"""
import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

# Mock MCP dependencies before importing server
_mock_mcp_module = MagicMock()
_mock_fastmcp = MagicMock()
_mock_fastmcp.FastMCP.return_value.tool.return_value = lambda fn: fn
sys.modules['mcp'] = _mock_mcp_module
sys.modules['mcp.server'] = MagicMock()
sys.modules['mcp.server.fastmcp'] = _mock_fastmcp

_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, _ROOT)

from MCP_Server import server  # noqa: E402
from MCP_Server.server import (  # noqa: E402
    _one_based_track_indices,
    _ok_json,
    _parse_return_track,
    _track_ref_params,
    get_recording_status,
    get_track_info,
    get_track_routing_options,
    list_tracks,
    set_track_arm,
    set_track_input,
    set_track_monitoring,
    set_track_mute,
    set_track_solo,
    set_tracks_state,
)
from tests.unit.test_recording_safety_remote import (  # noqa: E402
    AUTO,
    _Track as _RemoteTrack,
    _dispatch as _dispatch_remote,
    _make_script as _make_remote_script,
)


@pytest.fixture
def ableton():
    with patch('MCP_Server.server.get_ableton_connection') as mock_conn:
        conn = MagicMock()
        mock_conn.return_value = conn
        yield conn


def _sent(conn):
    call = conn.send_command.call_args
    return call[0][0], call[0][1] if len(call[0]) > 1 else None


class TestHelpers:
    @pytest.mark.parametrize("value,expected", [
        ("A", 0), ("b", 1), (" C ", 2), ("1", 0), ("12", 11), (2, 1),
        ("", None), (None, None), ("  ", None),
    ])
    def test_parse_return_track(self, value, expected):
        assert _parse_return_track(value) == expected

    @pytest.mark.parametrize("value", ["0", "AB", "?", "-1"])
    def test_parse_return_track_rejects(self, value):
        with pytest.raises(ValueError) as excinfo:
            _parse_return_track(value)
        assert "letter (A, B, ...) or a 1-based number" in str(excinfo.value)

    def test_track_ref_by_index(self):
        assert _track_ref_params(3) == {"track_index": 2}

    def test_track_ref_by_name_with_expect_name(self):
        assert _track_ref_params(0, "Gtr", "", "Gtr") == {
            "track_name": "Gtr", "expect_name": "Gtr"}

    def test_track_ref_by_return(self):
        assert _track_ref_params(0, "", "B") == {"return_track_index": 1}

    @pytest.mark.parametrize("args", [(0, "", ""), (1, "Gtr", ""),
                                      (0, "Gtr", "A")])
    def test_track_ref_needs_exactly_one(self, args):
        with pytest.raises(ValueError) as excinfo:
            _track_ref_params(*args)
        assert "exactly one of track_index (1-based)" in str(excinfo.value)

    def test_track_ref_rejects_negative_index(self):
        with pytest.raises(ValueError):
            _track_ref_params(-2)

    def test_one_based_track_indices(self):
        value = {"index": 0, "flag": True, "nested": [{"index": 4},
                                                     {"index": None}],
                 "track": {"index": True}}
        assert _one_based_track_indices(value) == {
            "index": 1, "flag": True, "nested": [{"index": 5},
                                                 {"index": None}],
            "track": {"index": True}}

    def test_ok_json_is_compact(self):
        text = _ok_json({"track": {"index": 0, "name": "Gtr"}})
        assert text == ('{"ok":true,"track":{"index":1,"name":"Gtr"},'
                        '"warnings":[]}')

    def test_ok_json_keeps_warnings_and_handles_none(self):
        assert json.loads(_ok_json({"warnings": ["x"]}))["warnings"] == ["x"]
        assert json.loads(_ok_json(None)) == {"ok": True, "warnings": []}


class TestMuteSolo:
    def test_mute_by_return_track(self, ableton):
        ableton.send_command.return_value = {
            "track": {"return_track": "A", "name": "Verb"}, "mute": True}
        result = json.loads(set_track_mute(MagicMock(), return_track="a",
                                           mute=True, expect_name="Verb"))
        assert result["track"] == {"return_track": "A", "name": "Verb"}
        assert _sent(ableton) == ("set_track_mute", {
            "return_track_index": 0, "expect_name": "Verb", "mute": True})

    def test_mute_error_from_live(self, ableton):
        ableton.send_command.side_effect = Exception("expect_name mismatch")
        result = set_track_mute(MagicMock(), track_name="Gtr")
        assert result == "Error setting track mute: expect_name mismatch"

    def test_solo_exclusive_by_name(self, ableton):
        ableton.send_command.return_value = {
            "track": {"index": 1, "name": "Gtr"}, "solo": True,
            "unsoloed": ["Bass"]}
        result = json.loads(set_track_solo(MagicMock(), track_name="Gtr",
                                           exclusive=True))
        assert result["track"]["index"] == 2
        assert result["unsoloed"] == ["Bass"]
        assert _sent(ableton) == ("set_track_solo", {
            "track_name": "Gtr", "solo": True, "exclusive": True})

    def test_solo_validation_error(self, ableton):
        result = set_track_solo(MagicMock(), track_index=1, return_track="A")
        assert result.startswith("Error setting track solo: give exactly one")
        ableton.send_command.assert_not_called()


class TestArm:
    def test_arm_exclusive_with_guard(self, ableton):
        ableton.send_command.return_value = {
            "track": {"index": 4, "name": "Gtr DI"}, "arm": True,
            "disarmed": ["Bandmate"], "warnings": ["w"]}
        result = json.loads(set_track_arm(MagicMock(), track_index=5,
                                          exclusive=True,
                                          expect_name="Gtr DI"))
        assert result["ok"] is True
        assert result["track"]["index"] == 5
        assert result["disarmed"] == ["Bandmate"]
        assert result["warnings"] == ["w"]
        assert _sent(ableton) == ("set_track_arm", {
            "track_index": 4, "expect_name": "Gtr DI", "arm": True,
            "exclusive": True})

    def test_arm_error(self, ableton):
        ableton.send_command.side_effect = Exception("no Arm state")
        result = set_track_arm(MagicMock(), track_index=1, arm=False)
        assert result == "Error setting track arm: no Arm state"


class TestRouting:
    def test_routing_options(self, ableton):
        ableton.send_command.return_value = {
            "track": {"index": 0, "name": "Gtr"},
            "input": {"current": {"type": "Ext. In", "channel": "1"},
                      "types": ["Ext. In"], "channels": ["1"]},
            "output": None}
        result = json.loads(get_track_routing_options(MagicMock(),
                                                      track_index=1))
        assert result["track"]["index"] == 1
        assert result["input"]["types"] == ["Ext. In"]
        assert _sent(ableton) == ("get_track_routing_options",
                                  {"track_index": 0})

    def test_routing_options_error(self, ableton):
        result = get_track_routing_options(MagicMock())
        assert result.startswith("Error getting track routing options:")
        ableton.send_command.assert_not_called()

    def test_set_input(self, ableton):
        ableton.send_command.return_value = {
            "track": {"index": 2, "name": "Gtr"},
            "input": {"type": "Ext. In", "channel": "4"}}
        result = json.loads(set_track_input(MagicMock(), "Ext. In", "4",
                                            track_index=3,
                                            expect_name="Gtr"))
        assert result["input"] == {"type": "Ext. In", "channel": "4"}
        assert _sent(ableton) == ("set_track_input", {
            "track_index": 2, "expect_name": "Gtr", "input_type": "Ext. In",
            "input_channel": "4"})

    def test_set_input_requires_a_type(self, ableton):
        result = set_track_input(MagicMock(), "  ", track_index=1)
        assert result == "Error setting track input: input_type is required"
        ableton.send_command.assert_not_called()

    def test_set_input_error_from_live(self, ableton):
        ableton.send_command.side_effect = Exception("No input type named 'X'")
        result = set_track_input(MagicMock(), "X", track_name="Gtr")
        assert result == "Error setting track input: No input type named 'X'"


class TestMonitoring:
    def test_set_monitoring_normalizes_state(self, ableton):
        ableton.send_command.return_value = {
            "track": {"index": 0, "name": "Gtr"}, "monitoring": "in"}
        result = json.loads(set_track_monitoring(MagicMock(), " IN ",
                                                 track_name="Gtr"))
        assert result["monitoring"] == "in"
        assert _sent(ableton) == ("set_track_monitoring", {
            "track_name": "Gtr", "state": "in"})

    def test_invalid_state_never_reaches_live(self, ableton):
        result = set_track_monitoring(MagicMock(), "loud", track_index=1)
        assert result.startswith("Error setting track monitoring: "
                                 "monitoring state must be one of")
        ableton.send_command.assert_not_called()


class TestSetTracksState:
    def test_converts_each_change(self, ableton):
        ableton.send_command.return_value = {
            "dry_run": False,
            "changes": [{"track": {"index": 0, "name": "Guide"},
                         "before": {}, "after": {}}]}
        result = json.loads(set_tracks_state(MagicMock(), [
            {"track_name": "Guide", "input_type": "No Input", "arm": False},
            {"track_index": 3, "expect_name": "Gtr", "monitoring": "AUTO",
             "mute": None},
            {"return_track": "B", "solo": True},
        ]))
        assert result["changes"][0]["track"]["index"] == 1
        command, params = _sent(ableton)
        assert command == "set_tracks_state"
        assert params == {"dry_run": False, "changes": [
            {"track_name": "Guide", "input_type": "No Input", "arm": False},
            {"track_index": 2, "expect_name": "Gtr", "monitoring": "AUTO",
             "mute": None},
            {"return_track_index": 1, "solo": True},
        ]}

    def test_dry_run_flag_is_sent(self, ableton):
        ableton.send_command.return_value = {"dry_run": True, "changes": []}
        result = json.loads(set_tracks_state(
            MagicMock(), [{"track_index": 1, "mute": True}], dry_run=True))
        assert result["dry_run"] is True
        assert _sent(ableton)[1]["dry_run"] is True

    def test_every_problem_is_listed_before_the_socket(self, ableton):
        result = set_tracks_state(MagicMock(), [
            {"track_index": 1, "mute": True},
            "nope",
            {"track_index": 1, "track_name": "Gtr", "mute": True},
            {"track_index": "x", "mute": True},
            {"return_track": "AB", "mute": True},
        ])
        assert result.startswith("Error setting tracks state: rejected the "
                                 "whole batch; nothing was changed.")
        assert "change 2: each change must be an object" in result
        assert "change 3: give exactly one of" in result
        assert "change 4:" in result
        assert "change 5: return_track must be a letter" in result
        assert "change 1" not in result
        ableton.send_command.assert_not_called()

    def test_state_values_and_unknown_keys_reach_the_remote_script(
            self, ableton):
        ableton.send_command.return_value = {"dry_run": False, "changes": []}
        set_tracks_state(MagicMock(), [
            {"track_index": 1, "volume": 1},
            {"track_name": "Gtr", "monitoring": "loud", "arm": "yes"},
        ])
        assert _sent(ableton)[1]["changes"] == [
            {"track_index": 0, "volume": 1},
            {"track_name": "Gtr", "monitoring": "loud", "arm": "yes"},
        ]

    def test_remote_script_lists_every_problem_together(self, ableton):
        gtr = _RemoteTrack("Gtr")
        script = _make_remote_script([gtr])

        def send_command(command, params=None):
            response = _dispatch_remote(script, command, params)
            if response["status"] == "error":
                raise Exception(response["message"])
            return response["result"]

        ableton.send_command.side_effect = send_command
        result = set_tracks_state(MagicMock(), [
            {"track_index": 1, "monitoring": "inn"},
            {"track_name": "Gutiar", "arm": True},
        ])
        assert result.startswith("Error setting tracks state: Rejected the "
                                 "whole batch; nothing was changed.")
        assert ("change 1: monitoring must be one of in, auto, off "
                "(got 'inn')") in result
        assert "change 2: No track named 'Gutiar'" in result
        assert gtr.current_monitoring_state == AUTO and gtr.arm is False

    @pytest.mark.parametrize("changes", [[], None])
    def test_empty_batch(self, ableton, changes):
        result = set_tracks_state(MagicMock(), changes)
        assert result == ("Error setting tracks state: changes must be a "
                          "non-empty list")
        ableton.send_command.assert_not_called()

    def test_error_from_live(self, ableton):
        ableton.send_command.side_effect = Exception("Rejected the whole batch")
        result = set_tracks_state(MagicMock(), [{"track_index": 1,
                                                 "mute": True}])
        assert result == "Error setting tracks state: Rejected the whole batch"


class TestReads:
    def test_recording_status(self, ableton):
        ableton.send_command.return_value = {
            "transport": {"record_mode": False},
            "armed_tracks": [{"index": 2, "name": "Gtr"}],
            "warnings": ["careful"]}
        result = json.loads(get_recording_status(MagicMock()))
        assert result["armed_tracks"][0]["index"] == 3
        assert result["warnings"] == ["careful"]
        ableton.send_command.assert_called_once_with("get_recording_status")

    def test_recording_status_error(self, ableton):
        ableton.send_command.side_effect = Exception("down")
        assert get_recording_status(MagicMock()) == (
            "Error getting recording status: down")

    def test_list_tracks(self, ableton):
        ableton.send_command.return_value = {"tracks": [
            {"index": 0, "name": "Gtr"},
            {"index": None, "name": "Verb", "return_track": "A"}]}
        result = json.loads(list_tracks(MagicMock(), include_master=False))
        assert [t["index"] for t in result["tracks"]] == [1, None]
        assert _sent(ableton) == ("list_tracks", {"include_returns": True,
                                                  "include_master": False})

    def test_list_tracks_error(self, ableton):
        ableton.send_command.side_effect = Exception("down")
        assert list_tracks(MagicMock()) == "Error listing tracks: down"

    def test_track_info_group_reference_is_one_based(self, ableton):
        ableton.send_command.return_value = {
            "index": 3, "name": "Gtr", "group_track": {"index": 0,
                                                       "name": "Gtrs"}}
        result = json.loads(get_track_info(MagicMock(), 4,
                                           include_clip_slots=False))
        assert result["group_track"] == {"track_index": 1, "name": "Gtrs"}
        assert _sent(ableton) == ("get_track_info", {
            "track_index": 3, "include_clip_slots": False})

    def test_track_info_without_group(self, ableton):
        ableton.send_command.return_value = {"name": "Gtr",
                                             "group_track": None}
        result = json.loads(get_track_info(MagicMock(), 1))
        assert result["group_track"] is None
        assert _sent(ableton)[1]["include_clip_slots"] is True


class TestCommandRegistration:
    """Each new write command is in all three lists; reads are not gated."""

    WRITES = ["set_track_mute", "set_track_solo", "set_track_arm",
              "set_track_input", "set_track_monitoring", "set_tracks_state"]
    READS = ["get_track_routing_options", "get_recording_status",
             "list_tracks"]

    @staticmethod
    def _source(*parts):
        with open(os.path.join(_ROOT, *parts)) as f:
            return f.read()

    def test_writes_are_registered_everywhere(self):
        rs = self._source("AbletonMCP_Remote_Script", "__init__.py")
        mcp_src = self._source("MCP_Server", "server.py")
        gate = re.search(r'elif command_type in \[(.*?)\]:', rs, re.S).group(1)
        modifying = re.search(r'is_modifying_command = command_type in \[(.*?)\]',
                              mcp_src, re.S).group(1)
        for name in self.WRITES:
            assert '"{0}"'.format(name) in gate, name
            assert 'command_type == "{0}"'.format(name) in rs, name
            assert '"{0}"'.format(name) in modifying, name

    def test_reads_are_not_gated(self):
        rs = self._source("AbletonMCP_Remote_Script", "__init__.py")
        mcp_src = self._source("MCP_Server", "server.py")
        gate = re.search(r'elif command_type in \[(.*?)\]:', rs, re.S).group(1)
        modifying = re.search(r'is_modifying_command = command_type in \[(.*?)\]',
                              mcp_src, re.S).group(1)
        for name in self.READS:
            assert '"{0}"'.format(name) not in gate, name
            assert 'command_type == "{0}"'.format(name) in rs, name
            assert '"{0}"'.format(name) not in modifying, name

    def test_server_module_exposes_every_tool(self):
        for name in self.WRITES + self.READS:
            assert callable(getattr(server, name)), name
