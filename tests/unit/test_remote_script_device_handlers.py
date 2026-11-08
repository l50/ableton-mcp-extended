"""Unit tests for the Remote Script's device, rack, preset and browser handlers,
run against stub Live Device/Chain/DrumPad/Browser objects."""
import os
import sys
import types
from unittest.mock import ANY, MagicMock

import pytest


class _StubControlSurface:
    def __init__(self, c_instance):
        pass

    def log_message(self, msg):
        pass


_framework = types.ModuleType("_Framework")
_cs_module = types.ModuleType("_Framework.ControlSurface")
_cs_module.ControlSurface = _StubControlSurface
sys.modules.setdefault("_Framework", _framework)
sys.modules.setdefault("_Framework.ControlSurface", _cs_module)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from AbletonMCP_Remote_Script import AbletonMCP  # noqa: E402

AUDIO_EFFECT_TYPE_BUG = (
    "Bug: _get_device_type only matches the literal 'audio_effect' in class_name, "
    "so ordinary audio effects such as Chorus and EQ Eight (Eq8) are typed 'unknown'")

# ── stubs ────────────────────────────────────────────────────────────


class Param:
    def __init__(self, name, value=0.5, min=0.0, max=1.0, is_enabled=True,
                 is_quantized=False, value_items=(), display=None):
        self.name = name
        self.value = value
        self.min = min
        self.max = max
        self.is_enabled = is_enabled
        self.is_quantized = is_quantized
        self.value_items = list(value_items)
        self._display = display

    def __str__(self):
        return self._display if self._display is not None else f"{self.value:.2f}"


class Device:
    def __init__(self, name="Device", class_name="AudioEffectGroupDevice",
                 class_display_name="Audio Effect Rack", parameters=None, chains=None,
                 drum_pads=None, presets=None, selected_preset_index=0, is_active=True,
                 device_type=0):
        self.name = name
        self.class_name = class_name
        self.class_display_name = class_display_name
        self.type = device_type
        self.parameters = list(parameters if parameters is not None else [Param("Device On", 1.0)])
        self.chains = list(chains or [])
        self.can_have_chains = chains is not None
        self.drum_pads = list(drum_pads or [])
        self.can_have_drum_pads = drum_pads is not None
        self.is_active = is_active
        if presets is not None:
            self.presets = list(presets)
            self.selected_preset_index = selected_preset_index


class Chain:
    def __init__(self, name="Chain", devices=(), mute=False, solo=False):
        self.name = name
        self.devices = list(devices)
        self.mute = mute
        self.solo = solo


class Pad:
    def __init__(self, note, name, chains=(), mute=False, solo=False):
        self.note = note
        self.name = name
        self.chains = list(chains)
        self.mute = mute
        self.solo = solo


class Track:
    def __init__(self, name="Track", devices=()):
        self.name = name
        self.devices = list(devices)
        self.is_foldable = False
        self.has_midi_input = True
        self.has_audio_input = False
        self.arrangement_clips = []
        self.delete_device = MagicMock(side_effect=lambda i: self.devices.pop(i))


class Song:
    def __init__(self, tracks=()):
        self.tracks = list(tracks)
        self.return_tracks = []
        self.view = types.SimpleNamespace(selected_track=None)
        self.delete_track = MagicMock(side_effect=lambda i: self.tracks.pop(i))


def make_script(tracks=()):
    script = AbletonMCP.__new__(AbletonMCP)
    script._song = Song(tracks)
    script.log_message = MagicMock()
    script.application = MagicMock()
    return script


def synth():
    return Device("Wavetable", "InstrumentVector", "Wavetable Instrument", parameters=[
        Param("Device On", 1.0),
        Param("Filter Freq", 500.0, 20.0, 20000.0, display="500 Hz"),
        Param("Osc Type", 1.0, 0.0, 3.0, is_quantized=True, value_items=["Sine", "Saw"]),
        Param("Locked", 0.0, 0.0, 0.0, is_enabled=False),
    ], device_type=1)


def rack(chains):
    return Device("Instrument Rack", "InstrumentGroupDevice", "Instrument Rack", chains=chains)


# ── device resolution and lookup ─────────────────────────────────────


class TestResolveDevice:
    def test_resolves(self):
        track = Track("Keys", [synth()])
        assert make_script([track])._resolve_device(0, 0) == (track, track.devices[0])

    @pytest.mark.parametrize("tracks,args,error,match", [
        ([], (0, 0), IndexError, r"Track index 0 out of range \(0--1\)"),
        ([Track("Empty")], (0, 0), IndexError, r"on track 'Empty' \(0-0\)"),
        ([Track("Keys", [Device(), Device()])], (0, 2), IndexError, r"\(0-1\)"),
        ([Track("Keys", [synth()])], (0, 0, 0), ValueError, "is not a rack"),
        ([Track("Keys", [rack([Chain()])])], (0, 0, 1), IndexError, "Chain index 1 out of range"),
        ([Track("Keys", [rack([Chain("Empty")])])], (0, 0, 0), ValueError, "Chain 'Empty' has no devices"),
    ])
    def test_errors(self, tracks, args, error, match):
        with pytest.raises(error, match=match):
            make_script(tracks)._resolve_device(*args)

    def test_chain_index_returns_rack(self):
        device = rack([Chain("A", [synth()])])
        track = Track("Keys", [device])
        assert make_script([track])._resolve_device(0, 0, 0) == (track, device)


class TestFindParameter:
    @pytest.mark.parametrize("kwargs,name,match_type", [
        ({"name": "Filter Freq"}, "Filter Freq", "exact"),
        ({"name": "filter freq"}, "Filter Freq", "case_insensitive"),
        ({"name": "osc"}, "Osc Type", "partial"),
        ({"index": 0}, "Device On", "index"),
    ])
    def test_lookup_order(self, kwargs, name, match_type):
        param, kind = make_script()._find_parameter(synth(), **kwargs)
        assert (param.name, kind) == (name, match_type)

    @pytest.mark.parametrize("kwargs,error,match", [
        ({"name": "Resonance"}, ValueError, "Parameter 'Resonance' not found on device 'Wavetable'"),
        ({"index": 4}, IndexError, r"Parameter index 4 out of range on 'Wavetable' \(0-3\)"),
        ({}, ValueError, "Either name or index must be provided"),
    ])
    def test_errors(self, kwargs, error, match):
        with pytest.raises(error, match=match):
            make_script()._find_parameter(synth(), **kwargs)


class TestDeviceDescriptors:
    @pytest.mark.parametrize("device,expected", [
        (Device(drum_pads=[]), "drum_machine"),
        (Device(chains=[]), "rack"),
        (Device(class_name="X", class_display_name="Simpler Instrument", device_type=1),
         "instrument"),
        (Device(class_name="Audio_Effect_Thing", class_display_name="Reverb", device_type=2),
         "audio_effect"),
        (Device(class_name="midi_effect_arp", class_display_name="Arpeggiator", device_type=4),
         "midi_effect"),
        (Device(class_name="Mystery", class_display_name="Mystery"), "unknown"),
        (object(), "unknown"),
    ])
    def test_device_type(self, device, expected):
        assert make_script()._get_device_type(device) == expected

    @pytest.mark.xfail(strict=False, reason=AUDIO_EFFECT_TYPE_BUG)
    @pytest.mark.parametrize("device", [
        Device("Chorus", "AudioEffectChorus", "Chorus", device_type=2),
        Device("EQ Eight", "Eq8", "EQ Eight", device_type=2),
    ], ids=["chorus", "eq8"])
    def test_ordinary_audio_effects_are_audio_effect(self, device):
        assert make_script()._get_device_type(device) == "audio_effect"

    def test_device_info(self):
        info = make_script()._get_device_info(synth())
        assert info == {"name": "Wavetable", "class_name": "InstrumentVector",
                        "type": "instrument", "is_active": True, "can_have_chains": False,
                        "can_have_drum_pads": False, "parameter_count": 4,
                        "class_display_name": "Wavetable Instrument"}

    def test_device_info_without_display_name(self):
        class Bare:
            name = "Old"
            class_name = "OldDevice"
            is_active = False
            can_have_chains = False
            can_have_drum_pads = False
            parameters = []

        info = make_script()._get_device_info(Bare())
        assert info["class_display_name"] == "OldDevice"
        assert info["type"] == "unknown"


# ── parameter handlers ───────────────────────────────────────────────


class TestGetDeviceParameters:
    def test_lists_normalized_values(self):
        result = make_script([Track("Keys", [synth()])])._get_device_parameters(0, 0)
        assert result["device_name"] == "Wavetable"
        assert result["device_class"] == "InstrumentVector"
        assert result["parameter_count"] == 4
        freq = result["parameters"][1]
        assert freq == {"index": 1, "name": "Filter Freq", "value": round(480 / 19980, 4),
                        "min": 20.0, "max": 20000.0, "display_value": "500 Hz",
                        "is_enabled": True, "is_quantized": False, "value_items": []}
        assert result["parameters"][2]["value_items"] == ["Sine", "Saw"]
        assert result["parameters"][3]["value"] == 0.0

    def test_drills_into_chain(self):
        device = rack([Chain("A", [synth()])])
        result = make_script([Track("Keys", [device])])._get_device_parameters(0, 0, 0)
        assert result["device_name"] == "Wavetable"

    @pytest.mark.parametrize("device,chain_index,error,match", [
        (synth(), 0, ValueError, "is not a rack"),
        (rack([Chain()]), 2, IndexError, "Chain index 2 out of range"),
        (rack([Chain("Empty")]), 0, ValueError, "Chain 'Empty' has no devices"),
    ])
    def test_chain_errors(self, device, chain_index, error, match):
        script = make_script([Track("Keys", [device])])
        with pytest.raises(error, match=match):
            script._get_device_parameters(0, 0, chain_index)
        script.log_message.assert_called()


class TestSetDeviceParameter:
    def test_sets_by_name_with_denormalization(self):
        device = synth()
        result = make_script([Track("Keys", [device])])._set_device_parameter(
            0, 0, parameter_name="Filter Freq", value=0.5)
        assert device.parameters[1].value == pytest.approx(10010.0)
        assert result["parameter_name"] == "Filter Freq"
        assert result["new_value"] == 0.5
        assert result["clamped"] is False

    @pytest.mark.parametrize("value,stored,clamped", [(1.5, 3.0, True), (-1.0, 0.0, True)])
    def test_clamps_by_index(self, value, stored, clamped):
        device = synth()
        result = make_script([Track("Keys", [device])])._set_device_parameter(
            0, 0, parameter_index=2, value=value)
        assert device.parameters[2].value == stored
        assert result["clamped"] is clamped

    def test_targets_first_device_in_chain(self):
        inner = synth()
        script = make_script([Track("Keys", [rack([Chain("A", [inner])])])])
        script._set_device_parameter(0, 0, chain_index=0, parameter_index=0, value=0.0)
        assert inner.parameters[0].value == 0.0

    def test_constant_range_parameter_reports_zero(self):
        device = Device(parameters=[Param("Fixed", 2.0, 2.0, 2.0)])
        result = make_script([Track("T", [device])])._set_device_parameter(
            0, 0, parameter_index=0, value=0.3)
        assert result["old_value"] == 0.0

    @pytest.mark.parametrize("device,kwargs,error,match", [
        (synth(), {"chain_index": 0}, ValueError, "is not a rack"),
        (rack([Chain("Empty")]), {"chain_index": 0}, ValueError, "Chain 'Empty' has no devices"),
        (synth(), {"parameter_name": "Locked"}, ValueError, "'Locked' is currently disabled"),
    ])
    def test_errors(self, device, kwargs, error, match):
        script = make_script([Track("Keys", [device])])
        with pytest.raises(error, match=match):
            script._set_device_parameter(0, 0, **kwargs)


class TestSetDeviceEnabled:
    @pytest.mark.parametrize("enabled,stored", [(True, 1.0), (False, 0.0)])
    def test_toggles_device_on_parameter(self, enabled, stored):
        device = synth()
        device.parameters[0].value = 1.0 - stored
        result = make_script([Track("Keys", [device])])._set_device_enabled(0, 0, None, enabled)
        assert device.parameters[0].value == stored
        assert result == {"device_name": "Wavetable", "is_active": enabled}

    def test_targets_chain_device(self):
        inner = synth()
        script = make_script([Track("Keys", [rack([Chain("A", [inner])])])])
        assert script._set_device_enabled(0, 0, 0, False)["device_name"] == "Wavetable"
        assert inner.parameters[0].value == 0.0

    @pytest.mark.parametrize("device,chain_index,match", [
        (synth(), 0, "is not a rack"),
        (rack([Chain("Empty")]), 0, "Chain has no devices"),
        (Device("Bare", parameters=[]), None, "Device 'Bare' has no parameters"),
    ])
    def test_errors(self, device, chain_index, match):
        script = make_script([Track("Keys", [device])])
        with pytest.raises(ValueError, match=match):
            script._set_device_enabled(0, 0, chain_index, True)


# ── racks and drum pads ──────────────────────────────────────────────


class TestChainInfo:
    def _rack(self):
        return rack([
            Chain("Keys", [synth(), Device("Chorus", "AudioEffectChorus", "Chorus",
                                           is_active=False, device_type=2)], mute=True),
            Chain("Empty", [], solo=True),
        ])

    def test_lists_chains(self):
        result = make_script([Track("T", [self._rack()])])._get_chain_info(0, 0)
        assert result["device_name"] == "Instrument Rack"
        assert result["chain_count"] == 2
        assert result["chains"][0] == {
            "index": 0, "name": "Keys", "mute": True, "solo": False, "device_count": 2,
            "devices": [{"index": 0, "name": "Wavetable", "type": "instrument"},
                        {"index": 1, "name": "Chorus", "type": ANY}]}
        assert result["chains"][1]["devices"] == []

    def test_chain_detail(self):
        result = make_script([Track("T", [self._rack()])])._get_chain_info(0, 0, 0)
        assert result["chain_name"] == "Keys"
        assert result["devices"][1] == {"index": 1, "name": "Chorus",
                                        "class_name": "AudioEffectChorus", "type": ANY,
                                        "is_active": False, "parameter_count": 1}

    @pytest.mark.xfail(strict=False, reason=AUDIO_EFFECT_TYPE_BUG)
    def test_chain_types_audio_effects(self):
        script = make_script([Track("T", [self._rack()])])
        assert script._get_chain_info(0, 0)["chains"][0]["devices"][1]["type"] == "audio_effect"
        assert script._get_chain_info(0, 0, 0)["devices"][1]["type"] == "audio_effect"

    @pytest.mark.parametrize("device,chain_index,error,match", [
        (synth(), None, ValueError, "is not a rack and has no chains"),
        (rack([Chain()]), 3, IndexError, "Chain index 3 out of range"),
    ])
    def test_errors(self, device, chain_index, error, match):
        with pytest.raises(error, match=match):
            make_script([Track("T", [device])])._get_chain_info(0, 0, chain_index)


class TestDrumPadInfo:
    def test_lists_only_filled_pads(self):
        kick_chain = Chain("Kick", [Device("Simpler", "OriginalSimpler", "Simpler Instrument",
                                           device_type=1)])
        drum_rack = Device("Drum Rack", "DrumGroupDevice", "Drum Rack", drum_pads=[
            Pad(36, "Kick", [kick_chain], mute=True),
            Pad(37, "Empty"),
        ])
        result = make_script([Track("Drums", [drum_rack])])._get_drum_pad_info(0, 0)
        assert result == {"device_name": "Drum Rack", "filled_pads": [{
            "note": 36, "name": "Kick", "mute": True, "solo": False,
            "chains": [{"name": "Kick", "devices": [
                {"index": 0, "name": "Simpler", "type": "instrument"}]}],
        }]}

    def test_rejects_non_drum_rack(self):
        with pytest.raises(ValueError, match="is not a Drum Rack"):
            make_script([Track("T", [synth()])])._get_drum_pad_info(0, 0)


# ── deletion ─────────────────────────────────────────────────────────


class TestDeletion:
    def test_delete_device(self):
        track = Track("Keys", [synth(), Device("Reverb")])
        result = make_script([track])._delete_device(0, 1)
        assert result == {"deleted_device": "Reverb", "remaining_devices": 1}
        track.delete_device.assert_called_once_with(1)

    def test_delete_device_out_of_range(self):
        with pytest.raises(IndexError):
            make_script([Track("Keys")])._delete_device(0, 0)

    def test_delete_track(self):
        script = make_script([Track("A"), Track("B")])
        assert script._delete_track(1) == {"deleted_track": "B", "remaining_tracks": 1}

    @pytest.mark.parametrize("tracks,index,error,match", [
        ([Track("Only")], 0, ValueError, "Cannot delete the last remaining session track"),
        ([Track("A"), Track("B")], 2, IndexError, r"Track index 2 out of range \(0-1\)"),
    ])
    def test_delete_track_errors(self, tracks, index, error, match):
        script = make_script(tracks)
        with pytest.raises(error, match=match):
            script._delete_track(index)
        script._song.delete_track.assert_not_called()


# ── presets ──────────────────────────────────────────────────────────


class TestNavigatePreset:
    def _device(self, idx=1):
        return Device("Operator", presets=["Init", "Bell", "Pad"], selected_preset_index=idx)

    @pytest.mark.parametrize("start,direction,expected", [
        (1, "next", 2), (2, "next", 2), (1, "previous", 0), (0, "previous", 0), (1, "current", 1),
    ])
    def test_moves_within_bounds(self, start, direction, expected):
        device = self._device(start)
        result = make_script([Track("T", [device])])._navigate_preset(0, 0, None, direction)
        assert result == {"device_name": "Operator", "preset_name": ["Init", "Bell", "Pad"][expected],
                          "preset_index": expected, "preset_count": 3}
        assert device.selected_preset_index == expected

    def test_current_index_past_end_has_blank_name(self):
        result = make_script([Track("T", [self._device(7)])])._navigate_preset(0, 0)
        assert result["preset_name"] == ""

    def test_chain_device(self):
        script = make_script([Track("T", [rack([Chain("A", [self._device()])])])])
        assert script._navigate_preset(0, 0, 0, "current")["device_name"] == "Operator"

    @pytest.mark.parametrize("device,chain_index,direction,match", [
        (synth(), 0, "next", "is not a rack"),
        (rack([Chain("Empty")]), 0, "next", "Chain has no devices"),
        (synth(), None, "next", "Device 'Wavetable' has no presets available"),
        (Device("Op", presets=[]), None, "next", "has no presets available"),
        (Device("Op", presets=["A"]), None, "sideways", "Invalid direction: sideways"),
    ])
    def test_errors(self, device, chain_index, direction, match):
        with pytest.raises(ValueError, match=match):
            make_script([Track("T", [device])])._navigate_preset(0, 0, chain_index, direction)


# ── arrangement listing ──────────────────────────────────────────────


class _ArrangementClip:
    def __init__(self, name, start, end):
        self.name = name
        self.start_time = start
        self.end_time = end
        self.is_midi_clip = True
        self.is_audio_clip = False
        self.muted = False
        self.color = 1
        self.looping = False
        self.loop_start = 0.0
        self.loop_end = end - start


def test_arrangement_info_lists_clips_with_indices():
    track = Track("Keys")
    track.arrangement_clips = [_ArrangementClip("A", 0, 4), _ArrangementClip("B", 4, 8)]
    script = make_script([track])
    script._get_transport_info = MagicMock(return_value={"tempo": 120})
    result = script._get_arrangement_info(0)
    clips = result["tracks"][0]["arrangement_clips"]
    assert [(c["index"], c["name"]) for c in clips] == [(0, "A"), (1, "B")]
    assert result["tracks"][0]["clip_count"] == 2


def test_arrangement_info_error_is_logged():
    script = make_script([])
    script._get_transport_info = MagicMock(side_effect=RuntimeError("no song"))
    with pytest.raises(RuntimeError):
        script._get_arrangement_info(-1)
    script.log_message.assert_called_with("Error getting arrangement info: no song")


# ── browser ──────────────────────────────────────────────────────────


class Item:
    def __init__(self, name, children=(), uri=None, is_device=False, is_loadable=False,
                 is_folder=None):
        self.name = name
        self.children = list(children)
        self.uri = uri
        self.is_device = is_device
        self.is_loadable = is_loadable
        self.is_folder = bool(children) if is_folder is None else is_folder


class Browser:
    def __init__(self, **roots):
        for attr in ("instruments", "sounds", "drums", "audio_effects", "midi_effects",
                     "plugins"):
            setattr(self, attr, roots.pop(attr, Item(attr.title())))
        for attr, value in roots.items():
            setattr(self, attr, value)
        self.load_item = MagicMock()


def _browser():
    operator = Item("Operator", uri="query:Synths#Operator", is_device=True, is_loadable=True)
    return Browser(
        instruments=Item("Instruments", [Item("Synths", [operator])], uri="root:inst"),
        drums=Item("Drums", [Item("Kits", [Item("909", uri="kit:909", is_loadable=True)])]),
    )


def _script_with_browser(browser, tracks=()):
    script = make_script(tracks)
    script.application.return_value = types.SimpleNamespace(browser=browser)
    return script


class TestFindBrowserItemByUri:
    def test_finds_nested_item(self):
        item = make_script()._find_browser_item_by_uri(_browser(), "kit:909")
        assert item.name == "909"

    def test_missing_uri(self):
        assert make_script()._find_browser_item_by_uri(_browser(), "nope") is None

    def test_respects_max_depth(self):
        assert make_script()._find_browser_item_by_uri(_browser(), "kit:909", max_depth=2) is None

    def test_errors_return_none(self):
        class Broken:
            @property
            def uri(self):
                raise RuntimeError("browser crashed")

        script = make_script()
        assert script._find_browser_item_by_uri(Broken(), "x") is None
        script.log_message.assert_called()


class TestGetBrowserItem:
    def test_requires_application(self):
        script = make_script()
        script.application.return_value = None
        with pytest.raises(RuntimeError, match="Could not access Live application"):
            script._get_browser_item("u", None)

    def test_by_uri(self):
        result = _script_with_browser(_browser())._get_browser_item("query:Synths#Operator", None)
        assert result["found"] is True
        assert result["item"] == {"name": "Operator", "is_folder": False, "is_device": True,
                                  "is_loadable": True, "uri": "query:Synths#Operator"}

    def test_unknown_uri_without_path(self):
        result = _script_with_browser(_browser())._get_browser_item("nope", None)
        assert result == {"uri": "nope", "path": None, "found": False}

    def test_invalid_path(self):
        result = _script_with_browser(_browser())._get_browser_item(None, " / / ")
        assert result["error"] == "Invalid path"

    @pytest.mark.parametrize("path,name", [
        ("instruments/Synths/Operator", "Operator"),
        ("Drums/kits/909", "909"),
        ("Synths/Operator", "Operator"),
    ])
    def test_by_path(self, path, name):
        result = _script_with_browser(_browser())._get_browser_item(None, path)
        assert result["found"] is True
        assert result["item"]["name"] == name

    def test_path_part_not_found(self):
        result = _script_with_browser(_browser())._get_browser_item(None, "instruments/Pads")
        assert result["found"] is False
        assert result["error"] == "Path part 'Pads' not found"

    def test_error_is_reraised(self):
        script = make_script()
        script.application.side_effect = RuntimeError("no app")
        with pytest.raises(RuntimeError):
            script._get_browser_item("u", None)


class TestResolveBrowserRoot:
    @pytest.mark.parametrize("raw,expected", [
        ("Audio - Effects", "audio_effects"),
        ("  MIDI__FX ", "midi_effects"),
        ("User  Library", "user_library"),
    ])
    def test_normalize_collapses_separators(self, raw, expected):
        assert make_script()._normalize_browser_category_name(raw) == expected

    def test_falls_back_to_scanning_attributes(self):
        browser = types.SimpleNamespace(user_library=Item("User Library"))
        item, attr = make_script()._resolve_browser_root_category(browser, "User Library")
        assert (item.name, attr) == ("User Library", "user_library")

    def test_attribute_errors_are_logged(self):
        class Flaky:
            @property
            def packs(self):
                raise RuntimeError("not ready")

        script = make_script()
        item, attr = script._resolve_browser_root_category(Flaky(), "packs", ["packs"])
        assert (item, attr) == (None, "packs")
        script.log_message.assert_called()


class TestLoadBrowserItem:
    def test_loads_onto_selected_track(self):
        browser = _browser()
        track = Track("Keys")
        script = _script_with_browser(browser, [track])
        result = script._load_browser_item(0, "query:Synths#Operator")
        assert script._song.view.selected_track is track
        assert browser.load_item.call_args[0][0].name == "Operator"
        assert result == {"loaded": True, "item_name": "Operator", "track_name": "Keys",
                          "uri": "query:Synths#Operator"}

    @pytest.mark.parametrize("tracks,uri,error,match", [
        ([], "x", IndexError, "Track index out of range"),
        ([Track("Keys")], "missing", ValueError, "Browser item with URI 'missing' not found"),
    ])
    def test_errors(self, tracks, uri, error, match):
        with pytest.raises(error, match=match):
            _script_with_browser(_browser(), tracks)._load_browser_item(0, uri)


class TestGetBrowserTree:
    @pytest.mark.parametrize("app,match", [
        (None, "Could not access Live application"),
        (types.SimpleNamespace(browser=None), "Browser is not available"),
    ])
    def test_requires_browser(self, app, match):
        script = make_script()
        script.application.return_value = app
        with pytest.raises(RuntimeError, match=match):
            script.get_browser_tree()

    def test_all_categories(self):
        browser = _browser()
        browser.max_for_live = Item("M4L", uri="root:m4l")
        script = _script_with_browser(browser)
        result = script.get_browser_tree("all")
        names = [c["name"] for c in result["categories"]]
        assert names[:5] == ["Instruments", "Sounds", "Drums", "Audio Effects", "MIDI Effects"]
        assert "Max_for_live" in names and "Plugins" in names
        inst = result["categories"][0]
        assert inst == {"name": "Instruments", "is_folder": True, "is_device": False,
                        "is_loadable": False, "uri": "root:inst", "children": []}
        assert "max_for_live" in result["available_categories"]

    def test_single_category(self):
        result = _script_with_browser(_browser()).get_browser_tree("drums")
        assert [c["name"] for c in result["categories"]] == ["Drums"]

    @pytest.mark.parametrize("empty_root", ["instruments", "sounds"])
    def test_skips_empty_roots_and_logs_errors(self, empty_root):
        class Boom:
            @property
            def name(self):
                raise RuntimeError("boom")

        roots = ("instruments", "sounds", "drums", "audio_effects", "midi_effects", "plugins")
        browser = Browser(**{r: (None if r == empty_root else Boom()) for r in roots})
        del browser.load_item
        script = _script_with_browser(browser)
        result = script.get_browser_tree("all")
        assert result["categories"] == []
        logged = " ".join(c.args[0] for c in script.log_message.call_args_list)
        for root in roots:
            expected = root != empty_root
            assert (f"Error processing {root}" in logged) is expected

    def test_unexpected_error_is_reraised(self):
        script = make_script()
        script.application.side_effect = RuntimeError("no app")
        with pytest.raises(RuntimeError):
            script.get_browser_tree()


class TestGetBrowserItemsAtPath:
    @pytest.mark.parametrize("app,match", [
        (None, "Could not access Live application"),
        (types.SimpleNamespace(browser=None), "Browser is not available"),
    ])
    def test_requires_browser(self, app, match):
        script = make_script()
        script.application.return_value = app
        with pytest.raises(RuntimeError, match=match):
            script.get_browser_items_at_path("instruments")

    def test_invalid_path(self):
        with pytest.raises(ValueError, match="Invalid path"):
            _script_with_browser(_browser()).get_browser_items_at_path("/")

    def test_lists_children(self):
        result = _script_with_browser(_browser()).get_browser_items_at_path("instruments/synths")
        assert result["name"] == "Synths"
        assert result["is_folder"] is True
        assert result["items"] == [{"name": "Operator", "is_folder": False, "is_device": True,
                                    "is_loadable": True, "uri": "query:Synths#Operator"}]

    def test_leaf_without_children_attribute(self):
        leaf = types.SimpleNamespace(name="Leaf", uri="leaf:1")
        browser = _browser()
        browser.instruments.children.append(leaf)
        result = _script_with_browser(browser).get_browser_items_at_path("instruments/Leaf")
        assert result["items"] == [] and result["uri"] == "leaf:1"

    def test_descending_past_a_leaf(self):
        leaf = types.SimpleNamespace(name="Leaf")
        browser = _browser()
        browser.instruments.children.append(leaf)
        result = _script_with_browser(browser).get_browser_items_at_path("instruments/Leaf/More")
        assert result["error"] == "Item at 'instruments/Leaf' has no children"

    def test_missing_part(self):
        result = _script_with_browser(_browser()).get_browser_items_at_path("drums/Loops")
        assert result["error"] == "Path part 'Loops' not found"
