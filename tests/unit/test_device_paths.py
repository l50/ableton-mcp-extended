"""Unit tests for device paths (#16) and load_device_from_path (#17) on the MCP side."""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_mock_fastmcp = MagicMock()
_mock_fastmcp.FastMCP.return_value.tool.return_value = lambda fn: fn
sys.modules.setdefault("mcp", MagicMock())
sys.modules.setdefault("mcp.server", MagicMock())
sys.modules.setdefault("mcp.server.fastmcp", _mock_fastmcp)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from MCP_Server.device_paths import (  # noqa: E402
    UNSUPPORTED_PATH_GUARD_INDEX,
    parse_device_path,
    path_request,
)
from MCP_Server.tools.browser import load_device_from_path  # noqa: E402
from MCP_Server.tools.devices import (  # noqa: E402
    disable_device,
    enable_device,
    get_chain_info,
    get_device_parameters,
    set_device_parameter,
)

# "1/c2/1/c1/1": device 1, its chain 2, device 1, its chain 1, device 1.
ISSUE_PATH_WIRE = [["device", 0], ["chain", 1], ["device", 0], ["chain", 0], ["device", 0]]


class TestParseDevicePath:
    def test_string_form(self):
        assert parse_device_path("1/c2/1/c1/1") == ISSUE_PATH_WIRE

    def test_list_form_matches_the_string_form(self):
        listed = [1, ("chain", 2), 1, ("chain", 1), 1]
        assert parse_device_path(listed) == parse_device_path("1/c2/1/c1/1") == ISSUE_PATH_WIRE

    def test_json_list_form(self):
        assert parse_device_path([1, ["chain", 2], 1, ["chain", 1], 1]) == ISSUE_PATH_WIRE

    @pytest.mark.parametrize("path", [
        " 1 / C2 / 1 / c1 / 1 ", "d1/chain2/device1/c1/d1", "1/c 2/1/c1/1",
    ])
    def test_spelling_variants(self, path):
        assert parse_device_path(path) == ISSUE_PATH_WIRE

    def test_a_single_device(self):
        assert parse_device_path("3") == [["device", 2]]
        assert parse_device_path([3]) == [["device", 2]]

    def test_a_chain_end_is_allowed_only_when_asked(self):
        assert parse_device_path("1/c2", allow_chain_end=True) == [["device", 0], ["chain", 1]]
        assert parse_device_path([1, ["chain", 2]], allow_chain_end=True) == [
            ["device", 0], ["chain", 1]]
        with pytest.raises(ValueError, match="must end on a device, not a chain"):
            parse_device_path("1/c2")

    @pytest.mark.parametrize("path,match", [
        ("", "it is empty"),
        ([], "it is empty"),
        ("1//2", "empty segment"),
        ("1/", "empty segment"),
        ("c1", "segment 1 must be a device"),
        ("1/2", "segment 2 must be a chain"),
        ("1/c1/c1", "segment 3 must be a device"),
        ("1/x2/1", "cannot read segment 'x2'"),
        ("0/c1/1", "device numbers start at 1"),
        ("1/c0/1", "chain numbers start at 1"),
        ("-1", "cannot read segment '-1'"),
        ([1, ["chain", 0], 1], "chain numbers start at 1"),
        ([1, ["slot", 2], 1], "cannot read segment"),
        ([1, ["chain"], 1], "cannot read segment"),
        ([1, ["chain", "2"], 1], "cannot read segment"),
        ([1, ["chain", True], 1], "cannot read segment"),
        ([True], "cannot read segment"),
        ([1.5], "cannot read segment"),
        (7, "expected a string or a list"),
        (None, "expected a string or a list"),
    ])
    def test_rejects_malformed_paths(self, path, match):
        with pytest.raises(ValueError, match=match):
            parse_device_path(path)

    def test_the_error_shows_the_path_syntax(self):
        with pytest.raises(ValueError, match=r"Use numbers for devices and cN for chains"):
            parse_device_path("c1")

    def test_a_device_marker_in_a_list_is_a_device(self):
        assert parse_device_path([["device", 2], ["chain", 1], ["d", 3]]) == [
            ["device", 1], ["chain", 0], ["device", 2]]


class TestPathRequest:
    def test_carries_the_guard_index_and_no_chain(self):
        assert path_request(4, ISSUE_PATH_WIRE) == {
            "track_index": 4, "device_index": UNSUPPORTED_PATH_GUARD_INDEX, "chain_index": None,
            "device_path": ISSUE_PATH_WIRE,
        }

    def test_the_guard_index_is_out_of_range_for_any_track(self):
        assert UNSUPPORTED_PATH_GUARD_INDEX < 0


@pytest.fixture
def ableton():
    with patch("MCP_Server.connection.get_ableton_connection") as get:
        connection = MagicMock()
        get.return_value = connection
        yield connection


PARAMS_REPLY = {
    "device_name": "Wavetable", "device_class": "InstrumentVector", "parameter_count": 1,
    "parameters": [{"index": 0, "name": "Osc 1 Detune", "value": 0.5, "min": -1.0, "max": 1.0,
                    "display_value": "0.00 st", "is_enabled": True, "is_quantized": False,
                    "value_items": []}],
}


class TestDevicePathTools:
    def test_get_device_parameters_sends_the_path(self, ableton):
        ableton.send_command.return_value = PARAMS_REPLY
        out = get_device_parameters(
            MagicMock(), track_index=2, device_path="1/c2/1/c1/1", show_all=True)
        ableton.send_command.assert_called_once_with("get_device_parameters", {
            "track_index": 1, "device_index": -1, "chain_index": None,
            "device_path": ISSUE_PATH_WIRE, "show_all": True,
        })
        assert "Osc 1 Detune: 0.00 st (normalized 0.5)" in out

    def test_get_device_parameters_accepts_the_list_form(self, ableton):
        ableton.send_command.return_value = PARAMS_REPLY
        get_device_parameters(MagicMock(), track_index=1,
                              device_path=[1, ["chain", 2], 1, ["chain", 1], 1])
        assert ableton.send_command.call_args[0][1]["device_path"] == ISSUE_PATH_WIRE

    def test_set_device_parameter_sends_the_path_and_reports_the_new_value(self, ableton):
        ableton.send_command.side_effect = [
            {"device_name": "Wavetable", "parameters": []},
            {"parameter_name": "Osc 1 Detune", "display_value": "0.50 st", "new_value": 0.75},
        ]
        out = set_device_parameter(MagicMock(), track_index=1, device_path="1/c2/1/c1/1",
                                   parameter_name="Osc 1 Detune", value=0.75)
        lookup, write = ableton.send_command.call_args_list
        assert lookup.args == ("get_device_parameters", {
            "track_index": 0, "device_index": -1, "chain_index": None,
            "device_path": ISSUE_PATH_WIRE, "show_all": False,
        })
        assert write.args == ("set_device_parameter", {
            "track_index": 0, "device_index": -1, "chain_index": None,
            "device_path": ISSUE_PATH_WIRE, "parameter_name": "Osc 1 Detune",
            "parameter_index": None, "value": 0.75,
        })
        assert out == "Set Osc 1 Detune to 0.50 st (normalized 0.75)"

    def test_set_device_parameter_by_index_skips_the_name_lookup(self, ableton):
        ableton.send_command.return_value = {
            "parameter_name": "Osc 1 Detune", "display_value": "1.00 st", "new_value": 1.0}
        set_device_parameter(MagicMock(), track_index=1, device_path="1/c1/1",
                             parameter_index=5, value=1.0)
        ableton.send_command.assert_called_once()
        assert ableton.send_command.call_args[0][1]["parameter_index"] == 4

    @pytest.mark.parametrize("tool,enabled,word", [
        (enable_device, True, "enabled"), (disable_device, False, "disabled")])
    def test_enable_and_disable_send_the_path(self, ableton, tool, enabled, word):
        ableton.send_command.return_value = {"device_name": "Wavetable", "is_active": enabled}
        out = tool(MagicMock(), track_index=3, device_path="1/c2/1/c1/1")
        ableton.send_command.assert_called_once_with("set_device_enabled", {
            "track_index": 2, "device_index": -1, "chain_index": None,
            "device_path": ISSUE_PATH_WIRE, "enabled": enabled,
        })
        assert out == f"Wavetable {word}"

    def test_get_chain_info_for_a_rack_path_lists_chains(self, ableton):
        ableton.send_command.return_value = {
            "device_name": "Middle Rack", "chain_count": 1,
            "chains": [{"index": 0, "name": "Mid", "mute": False, "solo": False,
                        "device_count": 1, "devices": [{"name": "Innermost Rack"}]}],
        }
        out = get_chain_info(MagicMock(), track_index=1, device_path="1/c2/2")
        ableton.send_command.assert_called_once_with("get_chain_info", {
            "track_index": 0, "device_index": -1, "chain_index": None,
            "device_path": [["device", 0], ["chain", 1], ["device", 1]],
        })
        assert "Middle Rack — 1 chains" in out
        assert "1. Mid: 1 devices (Innermost Rack)" in out

    def test_get_chain_info_for_a_chain_path_lists_devices(self, ableton):
        ableton.send_command.return_value = {
            "chain_name": "Inner",
            "devices": [{"index": 0, "name": "Wavetable", "type": "instrument",
                         "parameter_count": 90, "is_active": True}],
        }
        out = get_chain_info(MagicMock(), track_index=1, device_path="1/c2/2/c1")
        assert ableton.send_command.call_args[0][1]["device_path"] == [
            ["device", 0], ["chain", 1], ["device", 1], ["chain", 0]]
        assert "Chain 'Inner' — 1 devices" in out
        assert "1. Wavetable (instrument, 90 params)" in out

    def test_the_old_forms_send_what_they_always_sent(self, ableton):
        ableton.send_command.return_value = PARAMS_REPLY
        get_device_parameters(MagicMock(), track_index=1, device_index=2, chain_index=3)
        assert ableton.send_command.call_args[0][1] == {
            "track_index": 0, "device_index": 1, "chain_index": 2, "show_all": True}
        ableton.send_command.return_value = {"device_name": "D", "chains": [], "chain_count": 0}
        get_chain_info(MagicMock(), track_index=1, device_index=2)
        assert ableton.send_command.call_args[0][1] == {
            "track_index": 0, "device_index": 1, "chain_index": None}
        assert "device_path" not in ableton.send_command.call_args[0][1]

    @pytest.mark.parametrize("call", [
        lambda: get_device_parameters(MagicMock(), track_index=1, device_path="1/c1/1",
                                      chain_index=1),
        lambda: set_device_parameter(MagicMock(), track_index=1, device_path="1/c1/1",
                                     chain_index=1, parameter_index=1),
        lambda: get_chain_info(MagicMock(), track_index=1, device_path="1/c1", chain_index=1),
        lambda: enable_device(MagicMock(), track_index=1, device_path="1/c1/1", chain_index=1),
    ])
    def test_a_path_and_a_chain_index_together_are_rejected(self, ableton, call):
        assert "Give device_path or chain_index, not both" in call()
        ableton.send_command.assert_not_called()

    @pytest.mark.parametrize("tool", [enable_device, disable_device])
    def test_a_path_and_a_device_index_or_name_together_are_rejected(self, ableton, tool):
        for extra in ({"device_index": 2}, {"device_name": "Wavetable"}):
            out = tool(MagicMock(), track_index=1, device_path="1/c1/1", **extra)
            assert "Give device_path or device_index/device_name, not both" in out
        ableton.send_command.assert_not_called()

    @pytest.mark.parametrize("call", [
        lambda: get_device_parameters(MagicMock(), track_index=1, device_path="1/2"),
        lambda: set_device_parameter(MagicMock(), track_index=1, device_path="c1",
                                     parameter_index=1),
        lambda: get_chain_info(MagicMock(), track_index=1, device_path="1/c0"),
        lambda: enable_device(MagicMock(), track_index=1, device_path="1/c2"),
        lambda: disable_device(MagicMock(), track_index=1, device_path=[1, ["chain", 2]]),
    ])
    def test_a_malformed_path_is_reported_without_asking_live(self, ableton, call):
        out = call()
        assert out.startswith("Error ") and "Invalid device_path" in out
        ableton.send_command.assert_not_called()

    def test_path_requests_fail_on_a_script_without_path_support(self, ableton):
        """A Remote Script that predates paths ignores device_path; the guard
        device_index makes it reject the request instead of acting on device 1."""
        ableton.send_command.return_value = {"device_name": "x", "is_active": True,
                                             "parameters": [], "chains": []}
        get_device_parameters(MagicMock(), track_index=1, device_path="2/c1/1")
        set_device_parameter(MagicMock(), track_index=1, device_path="2/c1/1", parameter_index=1)
        get_chain_info(MagicMock(), track_index=1, device_path="2/c1/1")
        enable_device(MagicMock(), track_index=1, device_path="2/c1/1")
        disable_device(MagicMock(), track_index=1, device_path="2/c1/1")
        sent = [c.args[1] for c in ableton.send_command.call_args_list]
        assert len(sent) == 5
        assert all(params["device_index"] < 0 and params["chain_index"] is None
                   for params in sent)
        assert all(params["device_path"][0] == ["device", 1] for params in sent)


class TestLoadDeviceFromPathTool:
    PATH = "Presets/MIDI Effects/Max MIDI Effect/Chord Gen.amxd"

    def test_sends_the_command_with_a_zero_based_track(self, ableton):
        ableton.send_command.return_value = {
            "loaded": True, "item_name": "Chord Gen.amxd", "device_name": "Chord Gen",
            "device_index": 0}
        out = load_device_from_path(MagicMock(), track_index=3, path=self.PATH)
        ableton.send_command.assert_called_once_with(
            "load_device_from_path", {"track_index": 2, "path": self.PATH})
        assert out == "Loaded 'Chord Gen.amxd' onto track 3 as device 'Chord Gen' (device 1)."

    def test_reports_the_one_based_device_index(self, ableton):
        ableton.send_command.return_value = {
            "loaded": True, "item_name": "Bass Pad.adv", "device_name": "Bass Pad",
            "device_index": 2}
        out = load_device_from_path(MagicMock(), track_index=1, path="x/Bass Pad.adv")
        assert "as device 'Bass Pad' (device 3)" in out

    def test_reports_a_load_that_did_not_happen(self, ableton):
        ableton.send_command.return_value = {"loaded": False}
        out = load_device_from_path(MagicMock(), track_index=1, path="x/y.adv")
        assert out == "Failed to load 'x/y.adv' onto track 1."

    @pytest.mark.parametrize("path", ["", "   "])
    def test_requires_a_path(self, ableton, path):
        assert load_device_from_path(MagicMock(), track_index=1, path=path) == (
            "Error: path is required.")
        ableton.send_command.assert_not_called()

    def test_a_bad_track_is_reported(self, ableton):
        out = load_device_from_path(MagicMock(), track_index=0, path="x.adv")
        assert out.startswith("Error loading device from path: track_index must be >= 1")
        ableton.send_command.assert_not_called()

    def test_live_errors_are_passed_on(self, ableton):
        ableton.send_command.side_effect = Exception(
            "Path part 'Nope' not found in 'Presets'. It holds: A, B")
        out = load_device_from_path(MagicMock(), track_index=1, path="Presets/Nope")
        assert out == ("Error loading device from path: "
                       "Path part 'Nope' not found in 'Presets'. It holds: A, B")
