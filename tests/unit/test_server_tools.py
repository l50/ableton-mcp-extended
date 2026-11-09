"""Table-driven unit tests for the MCP tool layer in MCP_Server/server.py.

Every tool is called with ``get_ableton_connection`` patched to a fake that
records the command and params sent over the socket and replays scripted
results, so the assertions cover the 1-based to 0-based index conversion,
the exact wire payload and the text returned to the MCP client.
"""
import json
import os
import re
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

from MCP_Server import server  # noqa: E402

CTX = MagicMock()
_NO_PARAMS = object()


class FakeAbleton:
    """Records ``send_command`` calls; replies from a per-command table.

    A reply may be a dict, a list of dicts (consumed in order), an
    exception instance (raised) or a callable taking the params.
    """

    def __init__(self, replies=None):
        self.replies = dict(replies or {})
        self.calls = []

    def send_command(self, command_type, params=_NO_PARAMS):
        self.calls.append((command_type, params))
        reply = self.replies.get(command_type, {})
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return reply(params)
        return reply

    def commands(self):
        return [c for c, _ in self.calls]

    def sent(self, command_type):
        return [p for c, p in self.calls if c == command_type]


@pytest.fixture(autouse=True)
def _clean_plugin_cache():
    server._invalidate_external_plugin_cache()
    yield
    server._invalidate_external_plugin_cache()


def run_tool(fn, replies=None, **kwargs):
    fake = FakeAbleton(replies)
    with patch.object(server, "get_ableton_connection", return_value=fake):
        out = fn(CTX, **kwargs)
    return out, fake


SIG_4_4 = {"signature_numerator": 4, "signature_denominator": 4}


# ── simple request/response tools ────────────────────────────────────

SIMPLE_CASES = [
    # (id, tool, kwargs, replies, command, params, expected substrings)
    ("get_session_info", server.get_session_info, {},
     {"get_session_info": {"tempo": 120.0}}, "get_session_info", _NO_PARAMS,
     ['"tempo": 120.0']),
    ("get_track_info", server.get_track_info, {"track_index": 2},
     {"get_track_info": {"name": "Bass"}}, "get_track_info", {"track_index": 1},
     ['"name": "Bass"']),
    ("create_midi_track", server.create_midi_track, {"index": 3},
     {"create_midi_track": {"name": "4-MIDI"}}, "create_midi_track", {"index": 3},
     ["Created new MIDI track: 4-MIDI"]),
    ("create_midi_track_unnamed", server.create_midi_track, {},
     {}, "create_midi_track", {"index": -1}, ["Created new MIDI track: unknown"]),
    ("set_track_name", server.set_track_name, {"track_index": 1, "name": "Lead"},
     {"set_track_name": {"name": "Lead"}}, "set_track_name",
     {"track_index": 0, "name": "Lead"}, ["Renamed track to: Lead"]),
    ("create_clip", server.create_clip, {"track_index": 1, "clip_index": 2, "length": 8.0},
     {}, "create_clip", {"track_index": 0, "clip_index": 1, "length": 8.0},
     ["Created new clip at track 1, slot 2 with length 8.0 beats"]),
    ("add_notes_to_clip", server.add_notes_to_clip,
     {"track_index": 2, "clip_index": 1,
      "notes": [{"pitch": 60, "start_time": 0, "duration": 1, "velocity": 100, "mute": False},
                {"pitch": 64, "start_time": 1, "duration": 1, "velocity": 90, "mute": False}]},
     {}, "add_notes_to_clip",
     {"track_index": 1, "clip_index": 0,
      "notes": [{"pitch": 60, "start_time": 0, "duration": 1, "velocity": 100, "mute": False},
                {"pitch": 64, "start_time": 1, "duration": 1, "velocity": 90, "mute": False}]},
     ["Added 2 notes to clip at track 2, slot 1"]),
    ("set_clip_name", server.set_clip_name, {"track_index": 1, "clip_index": 3, "name": "Verse"},
     {}, "set_clip_name", {"track_index": 0, "clip_index": 2, "name": "Verse"},
     ["Renamed clip at track 1, slot 3 to 'Verse'"]),
    ("set_tempo", server.set_tempo, {"tempo": 128.0},
     {}, "set_tempo", {"tempo": 128.0}, ["Set tempo to 128.0 BPM"]),
    ("fire_clip", server.fire_clip, {"track_index": 3, "clip_index": 1},
     {}, "fire_clip", {"track_index": 2, "clip_index": 0},
     ["Started playing clip at track 3, slot 1"]),
    ("stop_clip", server.stop_clip, {"track_index": 3, "clip_index": 2},
     {}, "stop_clip", {"track_index": 2, "clip_index": 1},
     ["Stopped clip at track 3, slot 2"]),
    ("start_playback", server.start_playback, {}, {}, "start_playback", _NO_PARAMS,
     ["Started playback"]),
    ("stop_playback", server.stop_playback, {}, {}, "stop_playback", _NO_PARAMS,
     ["Stopped playback"]),
    ("jump_to_cue_next", server.jump_to_cue_point, {"direction": "next"},
     {"jump_to_cue": {"direction": "next", "time": 16.0}}, "jump_to_cue",
     {"direction": "next"}, ["Jumped to cue point:", "16.0"]),
    ("jump_to_cue_name", server.jump_to_cue_point, {"name": "Drop"},
     {"jump_to_cue": {"name": "Drop"}}, "jump_to_cue", {"name": "Drop"},
     ["Jumped to cue point:", "Drop"]),
    ("set_ableton_view", server.set_ableton_view, {"view": "Session"},
     {}, "set_view", {"view_name": "Session"}, ["Switched to Session view"]),
    ("control_view_zoom", server.control_arrangement_view, {"action": "zoom_in"},
     {}, "control_arrangement_view", {"action": "zoom_in", "track_index": 0},
     ["Arrangement view: zoom_in done"]),
    ("control_view_collapse", server.control_arrangement_view,
     {"action": "collapse_track", "track_index": 3},
     {}, "control_arrangement_view", {"action": "collapse_track", "track_index": 2},
     ["collapse_track done"]),
    ("manage_automation_create", server.manage_clip_automation,
     {"track_index": 2, "clip_index": 3, "action": "create", "parameter_name": "panning"},
     {}, "manage_clip_automation",
     {"track_index": 1, "clip_index": 2, "action": "create", "parameter_name": "panning"},
     ["Automation create: panning on clip 3, track 2"]),
    ("manage_automation_clear_all", server.manage_clip_automation,
     {"track_index": 1, "clip_index": 1, "action": "clear_all"},
     {}, "manage_clip_automation",
     {"track_index": 0, "clip_index": 0, "action": "clear_all", "parameter_name": "volume"},
     ["Cleared all automation on clip 1, track 1"]),
    ("navigate_preset_current", server.navigate_device_preset,
     {"track_index": 1, "direction": "current"},
     {"navigate_preset": {"device_name": "Operator", "preset_name": "Bell",
                          "preset_index": 2, "preset_count": 10}},
     "navigate_preset",
     {"track_index": 0, "device_index": 0, "chain_index": None, "direction": "current"},
     ["Operator: current preset is 'Bell' (3/10)"]),
    ("navigate_preset_next", server.navigate_device_preset,
     {"track_index": 2, "device_index": 2, "chain_index": 1, "direction": "next"},
     {"navigate_preset": {"device_name": "Operator", "preset_name": "Pad",
                          "preset_index": 3, "preset_count": 10}},
     "navigate_preset",
     {"track_index": 1, "device_index": 1, "chain_index": 0, "direction": "next"},
     ["Operator: loaded preset 'Pad' (4/10)"]),
]


@pytest.mark.parametrize(
    "tool,kwargs,replies,command,params,expected",
    [c[1:] for c in SIMPLE_CASES],
    ids=[c[0] for c in SIMPLE_CASES],
)
def test_simple_tool(tool, kwargs, replies, command, params, expected):
    out, fake = run_tool(tool, replies, **kwargs)
    assert fake.calls == [(command, params)]
    for text in expected:
        assert text in out


def test_manage_clip_automation_targets_named_clip():
    out, fake = run_tool(server.manage_clip_automation, track_index=1, clip_index=0,
                         clip_name="Chorus", action="clear_all")
    [params] = fake.sent("manage_clip_automation")
    assert params["clip_name"] == "Chorus"
    assert params.get("clip_index") != 0
    assert out == "Cleared all automation on clip 'Chorus', track 1"


# ── every tool reports connection failures as text ───────────────────

ERROR_CASES = [
    (server.get_session_info, {}, "Error getting session info: boom"),
    (server.get_track_info, {"track_index": 1}, "Error getting track info: boom"),
    (server.create_midi_track, {}, "Error creating MIDI track: boom"),
    (server.set_track_name, {"track_index": 1, "name": "x"}, "Error setting track name: boom"),
    (server.get_track_volume, {"track_index": 1}, "Error getting track volume: boom"),
    (server.set_track_volume, {"track_index": 1, "volume": 0.5}, "Error setting track volume: boom"),
    (server.set_track_panning, {"track_index": 1, "panning": 0.0}, "Error setting track panning: boom"),
    (server.create_clip, {"track_index": 1, "clip_index": 1}, "Error creating clip: boom"),
    (server.add_notes_to_clip, {"track_index": 1, "clip_index": 1, "notes": []},
     "Error adding notes to clip: boom"),
    (server.set_clip_name, {"track_index": 1, "clip_index": 1, "name": "x"},
     "Error setting clip name: boom"),
    (server.set_tempo, {"tempo": 100}, "Error setting tempo: boom"),
    (server.load_instrument_or_effect, {"track_index": 1, "uri": "u"},
     "Error loading instrument by URI: boom"),
    (server.fire_clip, {"track_index": 1, "clip_index": 1}, "Error firing clip: boom"),
    (server.stop_clip, {"track_index": 1, "clip_index": 1}, "Error stopping clip: boom"),
    (server.start_playback, {}, "Error starting playback: boom"),
    (server.stop_playback, {}, "Error stopping playback: boom"),
    (server.get_browser_tree, {}, "Error getting browser tree: boom"),
    (server.get_browser_items_at_path, {"path": "x"}, "Error getting browser items at path: boom"),
    (server.list_external_plugins, {}, "Error listing external plugins: boom"),
    (server.load_external_plugin, {"track_index": 1, "plugin_name": "Pro-Q"},
     "Error loading external plugin: boom"),
    (server.load_drum_kit, {"track_index": 1, "rack_uri": "r", "kit_path": "k"},
     "Error loading drum kit: boom"),
    (server.get_arrangement_info, {}, "Error getting arrangement info: boom"),
    (server.get_cue_points, {}, "Error getting cue points: boom"),
    (server.set_song_time, {"bar": 2}, "Error setting song time: boom"),
    (server.set_arrangement_loop, {}, "Error setting arrangement loop: boom"),
    (server.jump_to_cue_point, {"direction": "next"}, "Error jumping to cue point: boom"),
    (server.create_cue_point, {"bar": 1}, "Error creating cue point: boom"),
    (server.delete_cue_point, {"bar": 1}, "Error deleting cue point: boom"),
    (server.create_arrangement_midi_clip, {"track_index": 1},
     "Error creating arrangement MIDI clip: boom"),
    (server.create_arrangement_audio_clip, {"track_index": 1, "file_path": "/a.wav"},
     "Error creating arrangement audio clip: boom"),
    (server.duplicate_clip_to_arrangement, {"track_index": 1, "clip_index": 1},
     "Error duplicating clip to arrangement: boom"),
    (server.delete_arrangement_clip, {"track_index": 1, "clip_index": 1},
     "Error deleting arrangement clip: boom"),
    (server.set_arrangement_clip_property, {"track_index": 1, "muted": True},
     "Error setting arrangement clip property: boom"),
    (server.set_ableton_view, {}, "Error setting view: boom"),
    (server.control_arrangement_view, {"action": "zoom_in"},
     "Error controlling arrangement view: boom"),
    (server.manage_clip_automation, {"track_index": 1}, "Error managing clip automation: boom"),
    (server.get_device_parameters, {"track_index": 1}, "Error getting device parameters: boom"),
    (server.set_device_parameter, {"track_index": 1, "parameter_index": 1},
     "Error setting device parameter: boom"),
    (server.enable_device, {"track_index": 1}, "Error toggling device: boom"),
    (server.disable_device, {"track_index": 1}, "Error toggling device: boom"),
    (server.get_chain_info, {"track_index": 1}, "Error getting chain info: boom"),
    (server.get_drum_pad_info, {"track_index": 1}, "Error getting drum pad info: boom"),
    (server.delete_device, {"track_index": 1, "device_index": 1}, "Error deleting device: boom"),
    (server.get_track_deletion_status, {}, "Error checking track deletion status: boom"),
    (server.delete_track, {"track_index": 1}, "Error deleting track: boom"),
    (server.navigate_device_preset, {"track_index": 1}, "Error navigating preset: boom"),
]


@pytest.mark.parametrize("tool,kwargs,expected", ERROR_CASES,
                         ids=[c[0].__name__ for c in ERROR_CASES])
def test_tool_reports_connection_error(tool, kwargs, expected):
    with patch.object(server, "get_ableton_connection", side_effect=Exception("boom")):
        assert tool(CTX, **kwargs) == expected


@pytest.mark.parametrize("tool,kwargs", [
    (server.get_track_info, {"track_index": 0}),
    (server.create_clip, {"track_index": 1, "clip_index": 0}),
    (server.fire_clip, {"track_index": 0, "clip_index": 1}),
    (server.set_track_volume, {"track_index": -1, "volume": 0.5}),
])
def test_tools_reject_zero_based_indices(tool, kwargs):
    out, fake = run_tool(tool, **kwargs)
    assert "must be >= 1 (1-based)" in out
    assert fake.calls == []


# ── mixer read/write formatting ──────────────────────────────────────


class TestTrackVolume:
    @pytest.mark.parametrize("reply,expected", [
        ({"volume": 0.85, "panning": 0.0, "track_name": "Drums",
          "volume_min": 0.0, "volume_max": 1.0},
         ["Track 'Drums':", "Volume: 0.8500 (range 0.00–1.00) ≈ +0.0 dB", "(center)"]),
        ({"volume": 0.0, "panning": -0.5, "track_name": "Pad"},
         ["≈ -inf dB", "Panning: -0.5000 (0.50 L)"]),
        ({"volume": 1.0, "panning": 0.25, "track_name": "Lead"},
         ["Volume: 1.0000", "(0.25 R)"]),
    ])
    def test_get_track_volume(self, reply, expected):
        out, fake = run_tool(server.get_track_volume, {"get_track_volume": reply}, track_index=4)
        assert fake.calls == [("get_track_volume", {"track_index": 3})]
        for text in expected:
            assert text in out

    @pytest.mark.parametrize("volume,reply,expected", [
        (0.85, {"track_name": "Drums", "volume": 0.85}, "Set 'Drums' fader to 0.8500 (≈ +0.0 dB)"),
        (0.0, {"track_name": "Drums", "volume": 0.0}, "Set 'Drums' fader to 0.0000 (≈ -inf dB)"),
        (0.85, {}, "Set '?' fader to 0.8500 (≈ +0.0 dB)"),
    ])
    def test_set_track_volume(self, volume, reply, expected):
        out, fake = run_tool(server.set_track_volume, {"set_track_volume": reply},
                             track_index=2, volume=volume)
        assert fake.calls == [("set_track_volume", {"track_index": 1, "volume": volume})]
        assert out == expected

    @pytest.mark.xfail(strict=False, reason=(
        "Bug: the dB readout uses 20*log10(volume/0.85) instead of Live's fader "
        "mapping, where 0.85 is 0 dB and 1.0 is about +6 dB"))
    @pytest.mark.parametrize("tool,replies,kwargs", [
        (server.get_track_volume, {"get_track_volume": {"volume": 1.0, "panning": 0.0}}, {}),
        (server.set_track_volume, {"set_track_volume": {"volume": 1.0}}, {"volume": 1.0}),
    ], ids=["get", "set"])
    def test_full_fader_reads_about_plus_6_db(self, tool, replies, kwargs):
        out, _ = run_tool(tool, replies, track_index=1, **kwargs)
        db = float(re.search(r"≈ ([+-]\d+\.\d) dB", out).group(1))
        assert db == pytest.approx(6.0, abs=0.1)

    @pytest.mark.parametrize("panning,expected", [
        (0.0, "Set 'Bass' panning to 0.0000 (center)"),
        (-1.0, "Set 'Bass' panning to -1.0000 (1.00 L)"),
        (0.4, "Set 'Bass' panning to 0.4000 (0.40 R)"),
    ])
    def test_set_track_panning(self, panning, expected):
        out, fake = run_tool(server.set_track_panning,
                             {"set_track_panning": {"track_name": "Bass", "panning": panning}},
                             track_index=1, panning=panning)
        assert fake.calls == [("set_track_panning", {"track_index": 0, "panning": panning})]
        assert out == expected


# ── loading instruments and kits ─────────────────────────────────────


class TestLoadInstrument:
    URI = "query:Synths#Operator"

    @pytest.mark.parametrize("reply,expected", [
        ({"loaded": True, "new_devices": ["Operator"]},
         "Loaded instrument with URI 'query:Synths#Operator' on track 2. New devices: Operator"),
        ({"loaded": True, "devices_after": ["EQ Eight", "Operator"]},
         ("Loaded instrument with URI 'query:Synths#Operator' on track 2. "
         "Devices on track: EQ Eight, Operator")),
        ({"loaded": False}, "Failed to load instrument with URI 'query:Synths#Operator'"),
    ])
    def test_load_instrument_or_effect(self, reply, expected):
        out, fake = run_tool(server.load_instrument_or_effect, {"load_browser_item": reply},
                             track_index=2, uri=self.URI)
        assert fake.calls == [("load_browser_item", {"track_index": 1, "item_uri": self.URI})]
        assert out == expected


class TestLoadDrumKit:
    KW = {"track_index": 2, "rack_uri": "rack-uri", "kit_path": "drums/acoustic"}

    def test_rack_load_failure(self):
        out, fake = run_tool(server.load_drum_kit, {"load_browser_item": {"loaded": False}}, **self.KW)
        assert out == "Failed to load drum rack with URI 'rack-uri'"
        assert fake.commands() == ["load_browser_item"]

    def test_kit_path_error(self):
        out, _ = run_tool(server.load_drum_kit, {
            "load_browser_item": {"loaded": True},
            "get_browser_items_at_path": {"error": "Path part 'x' not found"},
        }, **self.KW)
        assert out == "Loaded drum rack but failed to find drum kit: Path part 'x' not found"

    def test_no_loadable_kit(self):
        out, _ = run_tool(server.load_drum_kit, {
            "load_browser_item": {"loaded": True},
            "get_browser_items_at_path": {"items": [{"name": "Folder", "is_loadable": False}]},
        }, **self.KW)
        assert out == "Loaded drum rack but no loadable drum kits found at 'drums/acoustic'"

    def test_loads_first_loadable_kit(self):
        out, fake = run_tool(server.load_drum_kit, {
            "load_browser_item": {"loaded": True},
            "get_browser_items_at_path": {"items": [
                {"name": "Folder", "is_loadable": False},
                {"name": "909 Kit", "is_loadable": True, "uri": "kit-909"},
                {"name": "808 Kit", "is_loadable": True, "uri": "kit-808"},
            ]},
        }, **self.KW)
        assert out == "Loaded drum rack and kit '909 Kit' on track 2"
        assert fake.calls == [
            ("load_browser_item", {"track_index": 1, "item_uri": "rack-uri"}),
            ("get_browser_items_at_path", {"path": "drums/acoustic"}),
            ("load_browser_item", {"track_index": 1, "item_uri": "kit-909"}),
        ]


# ── browser tools ────────────────────────────────────────────────────


class TestBrowserTree:
    def test_formats_nested_tree(self):
        reply = {
            "total_folders": 3,
            "categories": [
                {"name": "Instruments", "path": "instruments", "children": [
                    {"name": "Operator", "has_more": True},
                    {},
                ]},
                {"name": "Drums"},
            ],
        }
        out, fake = run_tool(server.get_browser_tree, {"get_browser_tree": reply},
                             category_type="all")
        assert fake.calls == [("get_browser_tree", {"category_type": "all"})]
        assert out.startswith("Browser tree for 'all' (showing 3 folders):")
        assert "• Instruments (path: instruments)\n" in out
        assert "  • Operator [...]\n" in out
        assert "• Drums\n" in out

    def test_no_categories_lists_available(self):
        reply = {"categories": [], "available_categories": ["instruments", "sounds"]}
        out, _ = run_tool(server.get_browser_tree, {"get_browser_tree": reply},
                          category_type="bogus")
        assert out == ("No categories found for 'bogus'. "
                       "Available browser categories: instruments, sounds")

    @pytest.mark.parametrize("message,expected", [
        ("Browser is not available in the Live application",
         "Error: The Ableton browser is not available."),
        ("Could not access Live application", "Error: Could not access the Ableton Live application."),
    ])
    def test_known_errors(self, message, expected):
        out, _ = run_tool(server.get_browser_tree, {"get_browser_tree": Exception(message)})
        assert out.startswith(expected)


class TestBrowserItemsAtPath:
    def test_returns_json(self):
        reply = {"path": "instruments", "items": [{"name": "Operator"}]}
        out, fake = run_tool(server.get_browser_items_at_path,
                             {"get_browser_items_at_path": reply}, path="instruments")
        assert fake.calls == [("get_browser_items_at_path", {"path": "instruments"})]
        assert json.loads(out) == reply

    def test_unknown_category_lists_available(self):
        reply = {"error": "Unknown or unavailable category: foo",
                 "available_categories": ["instruments", "drums"]}
        out, _ = run_tool(server.get_browser_items_at_path,
                          {"get_browser_items_at_path": reply}, path="foo")
        assert out == ("Error: Unknown or unavailable category: foo\n"
                       "Available browser categories: instruments, drums")

    @pytest.mark.parametrize("message,expected", [
        ("Browser is not available", "Error: The Ableton browser is not available."),
        ("Could not access Live application", "Error: Could not access the Ableton Live application."),
        ("Unknown or unavailable category: x",
         "Error: Unknown or unavailable category: x. Please check the available categories"),
        ("Path part 'Bass' not found", "Error: Path part 'Bass' not found. Please check the path"),
    ])
    def test_known_errors(self, message, expected):
        out, _ = run_tool(server.get_browser_items_at_path,
                          {"get_browser_items_at_path": Exception(message)}, path="x")
        assert out.startswith(expected)


# ── external plugin discovery helpers ────────────────────────────────


class TestPluginHelpers:
    @pytest.mark.parametrize("value,expected", [
        ("", ""),
        (None, ""),
        ("  Pro-Q_3  ", "pro q 3"),
    ])
    def test_normalize(self, value, expected):
        assert server._normalize_plugin_search_text(value) == expected

    @pytest.mark.parametrize("name,query,score", [
        ("Serum", "", 1),
        ("Serum", "serum", 1000),
        ("Serum FX", "serum", 900),
        ("FabFilter Pro-Q 3", "pro fab", 700 + 3 + 3),
        ("Valhalla", "zzz", 0),
    ])
    def test_match_score(self, name, query, score):
        assert server._plugin_match_score(name, query) == score

    def test_collect_walks_tree_and_skips_noise(self):
        tree = {
            "plugins": {"items": [
                {"name": "VST3", "is_folder": True},
                {"name": "", "is_loadable": True, "uri": "ignored"},
                {"name": "Loose", "is_loadable": True, "uri": "loose-uri", "is_device": True},
            ]},
            "plugins/VST3": {"items": [
                {"name": "Missing", "is_folder": True},
                {"name": "Serum", "is_loadable": True, "uri": "serum-uri"},
                {"name": "NoUri", "is_loadable": True},
            ]},
            "plugins/VST3/Missing": {"error": "Path part 'Missing' not found"},
        }
        fake = FakeAbleton({"get_browser_items_at_path": lambda p: tree[p["path"]]})
        plugins = server._collect_external_plugins_from_root(fake, "plugins")
        assert sorted(p["name"] for p in plugins) == ["Loose", "Serum"]
        serum = next(p for p in plugins if p["name"] == "Serum")
        assert serum == {"name": "Serum", "uri": "serum-uri", "path": "plugins/VST3/Serum",
                         "is_device": False, "root": "plugins"}

    def test_collect_skips_already_visited_paths(self):
        tree = {
            "plugins": {"items": [{"name": "A", "is_folder": True},
                                  {"name": "A", "is_folder": True}]},
            "plugins/A": {"items": []},
        }
        fake = FakeAbleton({"get_browser_items_at_path": lambda p: tree[p["path"]]})
        server._collect_external_plugins_from_root(fake, "plugins")
        assert fake.sent("get_browser_items_at_path").count({"path": "plugins/A"}) == 1

    def test_collect_root_error_raises(self):
        fake = FakeAbleton({"get_browser_items_at_path": {"error": "Unknown category"}})
        with pytest.raises(ValueError, match="Unknown category"):
            server._collect_external_plugins_from_root(fake, "plugins")

    def test_collect_respects_depth_and_safety_limits(self):
        fake = FakeAbleton({"get_browser_items_at_path": {"items": [
            {"name": "Deeper", "is_folder": True}]}})
        server._collect_external_plugins_from_root(fake, "plugins", max_depth=2)
        assert len(fake.calls) == 3
        with pytest.raises(RuntimeError, match="safety limit"):
            server._collect_external_plugins_from_root(fake, "plugins", max_visited_paths=1)

    def test_discover_returns_empty_when_roots_exist_but_hold_nothing(self):
        fake = FakeAbleton({"get_browser_items_at_path": {"items": []}})
        assert server._discover_external_plugins(fake) == []

    def test_discover_raises_when_no_root_resolves(self):
        fake = FakeAbleton({"get_browser_items_at_path": {"error": "nope"}})
        with pytest.raises(ValueError, match="Could not discover external plugins"):
            server._discover_external_plugins(fake)

    def test_cache_is_reused_until_refresh(self):
        fake = FakeAbleton()
        with patch.object(server, "_discover_external_plugins",
                          side_effect=[[{"name": "A"}], [{"name": "B"}]]) as discover:
            assert server._get_cached_external_plugins(fake) == [{"name": "A"}]
            assert server._get_cached_external_plugins(fake) == [{"name": "A"}]
            assert server._get_cached_external_plugins(fake, force_refresh=True) == [{"name": "B"}]
        assert discover.call_count == 2


class TestExternalPluginTools:
    PLUGINS = [
        {"name": "Serum", "uri": "serum-uri", "path": "plugins/Serum"},
        {"name": "Serum FX", "uri": "fx-uri", "path": "plugins/Serum FX"},
    ]

    def _run(self, fn, plugins, replies=None, **kwargs):
        with patch.object(server, "_get_cached_external_plugins", return_value=list(plugins)):
            return run_tool(fn, replies, **kwargs)

    @pytest.mark.parametrize("query,expected", [
        ("zzz", "No external plugins matched query 'zzz'."),
        ("", "No external plugins were discovered."),
    ])
    def test_list_with_no_results(self, query, expected):
        out, _ = self._run(server.list_external_plugins, [], query=query)
        assert out == expected

    def test_list_truncates_and_hints(self):
        out, _ = self._run(server.list_external_plugins, self.PLUGINS, max_results=1)
        assert "External plugins discovered: 2 total, showing 1" in out
        assert "1. Serum (path: plugins/Serum)" in out
        assert "Use max_results=2" in out

    @pytest.mark.parametrize("name", ["", "   "])
    def test_load_requires_name(self, name):
        out, fake = self._run(server.load_external_plugin, self.PLUGINS,
                              track_index=1, plugin_name=name)
        assert out == "Error: plugin_name is required."
        assert fake.calls == []

    def test_load_reports_failure(self):
        out, fake = self._run(server.load_external_plugin, self.PLUGINS,
                              {"load_browser_item": {"loaded": False}},
                              track_index=3, plugin_name="Serum", exact_match=True)
        assert out == "Failed to load external plugin 'Serum'."
        assert fake.calls == [("load_browser_item", {"track_index": 2, "item_uri": "serum-uri"})]


# ── arrangement tools ────────────────────────────────────────────────


class TestArrangementTools:
    def test_get_arrangement_info_formats_transport_and_clips(self):
        reply = {
            "transport": {"tempo": 120.0, "is_playing": True, "current_time": 8.0,
                          "signature_numerator": 4, "signature_denominator": 4,
                          "loop_enabled": True, "loop_start": 16.0, "loop_length": 16.0},
            "tracks": [{"index": 0, "name": "Drums", "is_midi": True, "arrangement_clips": [
                {"index": 0, "name": "Intro", "start_time": 0.0, "end_time": 16.0, "muted": True},
            ]}, {"index": 1, "name": "Vox", "is_midi": False, "arrangement_clips": []}],
        }
        out, fake = run_tool(server.get_arrangement_info, {"get_arrangement_info": reply},
                             track_index=0)
        assert fake.calls == [("get_arrangement_info", {"track_index": -1})]
        assert "Tempo: 120.0 BPM | Time Sig: 4/4 | Playing: True | Position: bar 3" in out
        assert "Loop: bars 5-9" in out
        assert "Track 1: Drums (MIDI) — 1 clip(s)" in out
        assert '1. "Intro" bars 1-5 [MUTED]' in out
        assert "Track 2: Vox (Audio) — 0 clip(s)" in out

    def test_get_arrangement_info_single_track(self):
        out, fake = run_tool(server.get_arrangement_info, {"get_arrangement_info": {}},
                             track_index=2)
        assert fake.calls == [("get_arrangement_info", {"track_index": 1})]
        assert "Loop:" not in out

    def test_get_cue_points(self):
        replies = {"get_cue_points": {"cue_points": [{"name": "Verse", "time": 32.0}]},
                   "get_session_info": {"signature_numerator": 3, "signature_denominator": 4}}
        out, _ = run_tool(server.get_cue_points, replies)
        assert out == '=== Cue Points ===\n  "Verse" — bar 11'

    def test_get_cue_points_empty(self):
        out, _ = run_tool(server.get_cue_points, {"get_session_info": SIG_4_4})
        assert out == "No cue points in this project."

    @pytest.mark.parametrize("kwargs,time_val,expected", [
        ({"bar": 3}, 8.0, "Jumped to bar 3 (beat 8.0)"),
        ({"beat": 6.0}, 6.0, "Jumped to bar 2 (beat 6.0)"),
    ])
    def test_set_song_time(self, kwargs, time_val, expected):
        out, fake = run_tool(server.set_song_time, {"get_session_info": SIG_4_4}, **kwargs)
        assert fake.sent("set_song_time") == [{"time": time_val}]
        assert out == expected

    @pytest.mark.parametrize("kwargs,params", [
        ({"start_bar": 2, "end_bar": 4}, {"enabled": True, "start": 4.0, "length": 8.0}),
        ({"start_bar": 2}, {"enabled": True, "start": 4.0}),
        ({"start_beat": 2.0, "length_beats": 6.0}, {"enabled": True, "start": 2.0, "length": 6.0}),
        ({"enabled": False}, {"enabled": False}),
    ])
    def test_set_arrangement_loop(self, kwargs, params):
        replies = {"get_session_info": SIG_4_4,
                   "set_arrangement_loop": {"enabled": params["enabled"],
                                            "start": 4.0, "length": 8.0}}
        out, fake = run_tool(server.set_arrangement_loop, replies, **kwargs)
        assert fake.sent("set_arrangement_loop") == [params]
        state = "enabled" if params["enabled"] else "disabled"
        assert out == f"Loop {state}: bars 2-4"

    @pytest.mark.parametrize("kwargs,time_val,label", [
        ({"bar": 5, "name": "Drop"}, 16.0, "bar 5"),
        ({"beat": 3.0, "name": "Pickup"}, 3.0, "bar ?"),
    ])
    def test_create_cue_point(self, kwargs, time_val, label):
        out, fake = run_tool(server.create_cue_point, {"get_session_info": SIG_4_4}, **kwargs)
        assert fake.sent("create_cue_point") == [{"time": time_val, "name": kwargs["name"]}]
        assert out == f"Created cue point '{kwargs['name']}' at {label}"

    @pytest.mark.parametrize("kwargs,time_val,label", [
        ({"bar": 2}, 4.0, "bar 2"),
        ({"beat": 3.0}, 3.0, "bar ?"),
    ])
    def test_delete_cue_point(self, kwargs, time_val, label):
        out, fake = run_tool(server.delete_cue_point, {"get_session_info": SIG_4_4}, **kwargs)
        assert fake.sent("delete_cue_point") == [{"time": time_val}]
        assert out == f"Deleted cue point at {label}"

    @pytest.mark.parametrize("kwargs,position,length", [
        ({"start_bar": 3, "end_bar": 5}, 8.0, 8.0),
        ({"start_bar": 3, "length_beats": 2.0}, 8.0, 2.0),
        ({"start_beat": 1.5, "length_beats": 3.0}, 1.5, 3.0),
    ])
    def test_create_arrangement_midi_clip(self, kwargs, position, length):
        replies = {"get_session_info": SIG_4_4,
                   "create_arrangement_clip": {"overlapped_clips": []}}
        out, fake = run_tool(server.create_arrangement_midi_clip, replies,
                             track_index=2, **kwargs)
        assert fake.sent("create_arrangement_clip") == [
            {"track_index": 1, "position": position, "length": length}]
        assert out.startswith("Created MIDI clip on track 2 at bar")
        assert f"length {length} beats" in out
        assert out.endswith(server._ARRANGEMENT_TIP)

    def test_create_arrangement_midi_clip_warns_on_overlap(self):
        replies = {"get_session_info": SIG_4_4,
                   "create_arrangement_clip": {"overlapped_clips": ["Intro", "Verse"]}}
        out, _ = run_tool(server.create_arrangement_midi_clip, replies, track_index=1)
        assert "Warning: overlapped existing clips: Intro, Verse" in out

    def test_create_arrangement_audio_clip(self):
        out, fake = run_tool(server.create_arrangement_audio_clip,
                             {"get_session_info": SIG_4_4},
                             track_index=1, file_path="/a.wav", start_bar=2)
        assert fake.sent("create_arrangement_audio_clip") == [
            {"track_index": 0, "position": 4.0, "file_path": "/a.wav"}]
        assert out == "Created audio clip from '/a.wav' on track 1" + server._ARRANGEMENT_TIP

    def test_duplicate_clip_to_arrangement(self):
        out, fake = run_tool(server.duplicate_clip_to_arrangement, {},
                             track_index=2, clip_index=3, destination_beat=12.0)
        assert fake.calls == [("duplicate_to_arrangement",
                               {"track_index": 1, "clip_index": 2, "destination_time": 12.0})]
        assert out == "Duplicated session clip to arrangement on track 2" + server._ARRANGEMENT_TIP

    @pytest.mark.parametrize("kwargs,params,ref", [
        ({"clip_name": "Intro"}, {"track_index": 0, "clip_name": "Intro"}, "'Intro'"),
        ({"clip_index": 2}, {"track_index": 0, "clip_index": 1}, "#2"),
    ])
    def test_delete_arrangement_clip(self, kwargs, params, ref):
        out, fake = run_tool(server.delete_arrangement_clip, {}, track_index=1, **kwargs)
        assert fake.calls == [("delete_arrangement_clip", params)]
        assert out == f"Deleted arrangement clip {ref} on track 1" + server._ARRANGEMENT_TIP

    def test_delete_arrangement_clip_needs_a_reference(self):
        out, fake = run_tool(server.delete_arrangement_clip, {}, track_index=1)
        assert out == "Error: provide clip_index or clip_name"
        assert fake.calls == []

    def test_set_arrangement_clip_property_sends_one_command_per_property(self):
        out, fake = run_tool(server.set_arrangement_clip_property, {},
                             track_index=2, clip_index=3, name="Hook", muted=False, gain=0.5)
        # "name" is sent last so a rename cannot break name-based lookup.
        assert fake.sent("set_arrangement_clip_property") == [
            {"track_index": 1, "clip_index": 2, "property": "muted", "value": False},
            {"track_index": 1, "clip_index": 2, "property": "gain", "value": 0.5},
            {"track_index": 1, "clip_index": 2, "property": "name", "value": "Hook"},
        ]
        assert out == "Updated clip 3 on track 2: muted=False, gain=0.5, name=Hook"

    def test_set_arrangement_clip_property_nothing_to_change(self):
        out, fake = run_tool(server.set_arrangement_clip_property, {}, track_index=1)
        assert out == "No properties specified to change."
        assert fake.calls == []


# ── device tools ─────────────────────────────────────────────────────


def _param(index, name, value=0.5, display="50 %", enabled=True):
    return {"index": index, "name": name, "value": value, "min": 0.0, "max": 1.0,
            "display_value": display, "is_enabled": enabled, "is_quantized": False,
            "value_items": []}


class TestDeviceTools:
    def test_get_device_parameters_summary_groups_by_category(self):
        reply = {"device_name": "Mystery Synth", "parameter_count": 2,
                 "parameters": [_param(0, "Cutoff"), _param(1, "Res")]}
        out, fake = run_tool(server.get_device_parameters,
                             {"get_device_parameters": reply}, track_index=1)
        assert fake.calls == [("get_device_parameters", {
            "track_index": 0, "device_index": 0, "chain_index": None, "show_all": True})]
        assert "Mystery Synth — 2 parameters total" in out
        assert "  Other: 2 parameters" in out
        assert "Use category='<name>' or show_all=True" in out

    def test_get_device_parameters_known_plugin_uses_categories_and_aliases(self):
        reply = {"device_name": "Serum_x64", "parameter_count": 3, "parameters": [
            _param(0, "Fil Cutoff", value=0.75, display="75 %"),
            _param(1, "Osc A WT Pos"),
            _param(2, "Chaos 1"),
        ]}
        summary, _ = run_tool(server.get_device_parameters, {"get_device_parameters": reply},
                              track_index=1)
        assert "  Filter: 1 parameters" in summary
        assert "  Oscillator A: 1 parameters" in summary
        assert "  Other: 1 parameters" in summary

        detail, _ = run_tool(server.get_device_parameters, {"get_device_parameters": reply},
                             track_index=1, category="filter")
        assert detail.splitlines()[0] == "Serum_x64 — 1 parameters"
        assert "  1. Fil Cutoff (filter cutoff): 75 % (normalized 0.75)" in detail

    def test_set_device_parameter_resolves_alias(self):
        replies = {"get_device_parameters": {"device_name": "Serum"},
                   "set_device_parameter": {"parameter_name": "Fil Cutoff",
                                            "display_value": "40 %", "new_value": 0.4}}
        out, fake = run_tool(server.set_device_parameter, replies,
                             track_index=1, parameter_name="filter cutoff", value=0.4)
        assert fake.sent("get_device_parameters") == [{
            "track_index": 0, "device_index": 0, "chain_index": None, "show_all": False}]
        assert fake.sent("set_device_parameter")[0]["parameter_name"] == "Fil Cutoff"
        assert out == "Set Fil Cutoff to 40 % (normalized 0.4) [alias: filter cutoff]"

    def test_get_device_parameters_unknown_category(self):
        reply = {"device_name": "Mystery Synth", "parameters": [_param(0, "Cutoff")]}
        out, _ = run_tool(server.get_device_parameters, {"get_device_parameters": reply},
                          track_index=1, category="Filter")
        assert out == "No parameters found in category 'Filter'. Available categories: Other"

    def test_get_device_parameters_detail_marks_disabled(self):
        reply = {"device_name": "Mystery Synth",
                 "parameters": [_param(0, "Cutoff", value=0.256, display="2 kHz", enabled=False)]}
        out, _ = run_tool(server.get_device_parameters, {"get_device_parameters": reply},
                          track_index=1, category="other")
        assert "  1. Cutoff: 2 kHz (normalized 0.26) [disabled]" in out

    @pytest.mark.parametrize("kwargs,param_name,pi", [
        ({"parameter_name": "Cutoff"}, "Cutoff", None),
        ({"parameter_index": 3}, None, 2),
    ])
    def test_set_device_parameter(self, kwargs, param_name, pi):
        replies = {"get_device_parameters": {"device_name": "Mystery Synth"},
                   "set_device_parameter": {"parameter_name": "Cutoff", "display_value": "1 kHz",
                                            "new_value": 0.5, "clamped": True}}
        out, fake = run_tool(server.set_device_parameter, replies,
                             track_index=1, device_index=2, value=1.5, **kwargs)
        assert fake.sent("set_device_parameter") == [{
            "track_index": 0, "device_index": 1, "chain_index": None,
            "parameter_name": param_name, "parameter_index": pi, "value": 1.5}]
        assert out == "Set Cutoff to 1 kHz (normalized 0.5) (value was clamped to 0.0-1.0 range)"

    def test_toggle_device_by_name_not_found(self):
        out, fake = run_tool(server.enable_device,
                             {"get_track_info": {"devices": [{"name": "EQ Eight", "index": 0}]}},
                             track_index=1, device_name="Reverb")
        assert out == "Error: Device 'Reverb' not found on track 1"
        assert fake.commands() == ["get_track_info"]

    def test_toggle_device_by_name_ambiguous(self):
        devices = [{"name": "Reverb", "index": 0}, {"name": "reverb", "index": 2}]
        out, _ = run_tool(server.disable_device, {"get_track_info": {"devices": devices}},
                          track_index=1, device_name="REVERB")
        assert out == ("Error: Multiple devices named 'REVERB' on track 1: "
                       "Reverb (index 1), reverb (index 3)")

    def test_toggle_device_by_name_resolves_index(self):
        replies = {"get_track_info": {"devices": [{"name": "EQ", "index": 0},
                                                  {"name": "Reverb", "index": 1}]},
                   "set_device_enabled": {"device_name": "Reverb", "is_active": False}}
        out, fake = run_tool(server.disable_device, replies, track_index=2, device_name="reverb")
        assert fake.sent("set_device_enabled") == [
            {"track_index": 1, "device_index": 1, "chain_index": None, "enabled": False}]
        assert out == "Reverb disabled"

    def test_toggle_device_defaults_to_first_device(self):
        replies = {"set_device_enabled": {"device_name": "EQ", "is_active": True}}
        out, fake = run_tool(server.enable_device, replies, track_index=1, chain_index=2)
        assert fake.calls == [("set_device_enabled", {
            "track_index": 0, "device_index": 0, "chain_index": 1, "enabled": True})]
        assert out == "EQ enabled"

    def test_get_chain_info_lists_chains(self):
        reply = {"device_name": "Instrument Rack", "chains": [
            {"index": 0, "name": "Keys", "mute": True, "solo": False, "device_count": 1,
             "devices": [{"name": "Electric"}]},
            {"index": 1, "name": "Empty", "mute": False, "solo": True, "device_count": 0,
             "devices": []},
        ]}
        out, fake = run_tool(server.get_chain_info, {"get_chain_info": reply}, track_index=1)
        assert fake.calls == [("get_chain_info",
                               {"track_index": 0, "device_index": 0, "chain_index": None})]
        assert "Instrument Rack — 2 chains" in out
        assert "  1. Keys [muted]: 1 devices (Electric)" in out
        assert "  2. Empty [solo]: 0 devices (empty)" in out

    def test_get_chain_info_chain_detail(self):
        reply = {"chain_name": "Keys", "devices": [
            {"index": 0, "name": "Electric", "type": "instrument", "parameter_count": 40},
            {"index": 1, "name": "Chorus", "type": "audio_effect", "is_active": False},
        ]}
        out, fake = run_tool(server.get_chain_info, {"get_chain_info": reply},
                             track_index=1, device_index=2, chain_index=1)
        assert fake.calls == [("get_chain_info",
                               {"track_index": 0, "device_index": 1, "chain_index": 0})]
        assert "Chain 'Keys' — 2 devices" in out
        assert "  1. Electric (instrument, 40 params)" in out
        assert "  2. Chorus (audio_effect, ? params) [disabled]" in out

    def test_get_drum_pad_info(self):
        reply = {"device_name": "Drum Rack", "filled_pads": [
            {"note": 36, "name": "Kick", "mute": True, "solo": False,
             "chains": [{"devices": [{"name": "Simpler"}]}]},
            {"note": 38, "name": "Snare", "solo": True, "chains": []},
        ]}
        out, fake = run_tool(server.get_drum_pad_info, {"get_drum_pad_info": reply},
                             track_index=1)
        assert fake.calls == [("get_drum_pad_info", {"track_index": 0, "device_index": 0})]
        assert "Drum Rack — 2 filled pads" in out
        assert "  Note 36: Kick [muted] → Simpler" in out
        assert "  Note 38: Snare [solo] → empty" in out

    def test_delete_device_by_name_not_found(self):
        out, _ = run_tool(server.delete_device, {"get_track_info": {"devices": []}},
                          track_index=1, device_name="Reverb")
        assert out == "Error: Device 'Reverb' not found on track 1"

    def test_delete_device_by_name_ambiguous(self):
        devices = [{"name": "Reverb", "index": 0}, {"name": "Reverb", "index": 1}]
        out, _ = run_tool(server.delete_device, {"get_track_info": {"devices": devices}},
                          track_index=1, device_name="Reverb")
        assert out == "Error: Multiple devices named 'Reverb': Reverb (index 1), Reverb (index 2)"

    def test_delete_device_by_name(self):
        replies = {"get_track_info": {"devices": [{"name": "Reverb", "index": 4}]},
                   "delete_device": {"deleted_device": "Reverb", "remaining_devices": 4}}
        out, fake = run_tool(server.delete_device, replies, track_index=1, device_name="Reverb")
        assert fake.sent("delete_device") == [{"track_index": 0, "device_index": 4}]
        assert out == "Deleted Reverb. 4 devices remaining."

    def test_delete_device_defaults_to_first(self):
        out, fake = run_tool(server.delete_device, {}, track_index=2)
        assert fake.calls == [("delete_device", {"track_index": 1, "device_index": 0})]
        assert out == "Deleted device. ? devices remaining."


# ── track deletion ───────────────────────────────────────────────────


class TestDeleteTrack:
    def test_needs_index_or_name(self):
        out, fake = run_tool(server.delete_track, {"get_session_info": {"track_count": 3}})
        assert out == "Error: provide either track_index (1-based) or track_name."
        assert fake.commands() == ["get_session_info"]

    def test_unknown_name(self):
        replies = {"get_session_info": {"track_count": 2},
                   "get_track_info": [{"name": "Drums"}, {"name": "Bass"}]}
        out, fake = run_tool(server.delete_track, replies, track_name="Vox")
        assert out == "Error: No track named 'Vox' found."
        assert fake.sent("get_track_info") == [{"track_index": 0}, {"track_index": 1}]
        assert "delete_track" not in fake.commands()
