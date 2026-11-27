"""Unit tests for device paths through nested racks (#16) and loading a device
from a browser file path (#17), run against stub Live objects."""
import types
from unittest.mock import MagicMock

import pytest

from tests.unit.test_remote_script_device_handlers import (
    Chain,
    Device,
    Param,
    Track,
    make_script,
    rack,
    synth,
)

# ── fixtures ─────────────────────────────────────────────────────────


def wavetable(name="Wavetable"):
    device = synth()
    device.name = name
    device.parameters.append(Param("Osc 1 Detune", 0.5, -1.0, 1.0, display="0.00 st"))
    return device


def nested_rack():
    """Track device 1 is a rack; its chain 2 holds an EQ and a second rack; that
    rack's chain 1 holds a third rack; its chain 1 holds the Wavetable."""
    deep = wavetable("Deep Wavetable")
    innermost = rack([Chain("Inner", [deep])])
    innermost.name = "Innermost Rack"
    middle = rack([Chain("Mid", [innermost])])
    middle.name = "Middle Rack"
    outer = rack([
        Chain("Top A", [wavetable("Shallow Wavetable")]),
        Chain("Top B", [Device("EQ Eight", "Eq8", "EQ Eight", device_type=2), middle]),
    ])
    outer.name = "Outer Rack"
    track = Track("Keys", [outer])
    return track, outer, deep


# Device 1, chain 2, device 2, chain 1, device 1, chain 1, device 1 (1-based "1/c2/2/c1/1/c1/1").
DEEP_PATH = [["device", 0], ["chain", 1], ["device", 1], ["chain", 0], ["device", 0],
             ["chain", 0], ["device", 0]]


def dispatching_script(tracks):
    script = make_script(tracks)
    script.schedule_message = MagicMock(side_effect=lambda _delay, fn: fn())
    script.show_message = MagicMock()
    return script


# ── path walking ─────────────────────────────────────────────────────


class TestResolveDevicePath:
    def test_walks_three_levels(self):
        track, _, deep = nested_rack()
        resolved_track, node, kind = make_script([track])._resolve_device_path(0, DEEP_PATH)
        assert (resolved_track, node, kind) == (track, deep, "device")

    def test_a_path_ending_on_a_chain_returns_the_chain(self):
        track, outer, _ = nested_rack()
        _, node, kind = make_script([track])._resolve_device_path(
            0, [["device", 0], ["chain", 1]])
        assert (node, kind) == (outer.chains[1], "chain")

    def test_a_single_device_is_a_track_device(self):
        track, outer, _ = nested_rack()
        assert make_script([track])._resolve_device_path(0, [["device", 0]])[1] is outer

    @pytest.mark.parametrize("path,error,match", [
        ([], ValueError, "non-empty list"),
        ("1/c1", ValueError, "non-empty list"),
        ([["chain", 0]], ValueError, "a chain cannot follow a track"),
        ([["device", 0], ["device", 0]], ValueError, "a device cannot follow a device"),
        ([["device", 0], ["chain", 0], ["chain", 0]], ValueError, "a chain cannot follow a chain"),
        ([["device", 5]], IndexError, r"step 1: device index 5 out of range on 'Keys' \(1 available\)"),
        ([["device", 0], ["chain", 2]], IndexError,
         r"step 2: chain index 2 out of range on 'Outer Rack' \(2 available\)"),
        ([["device", 0], ["chain", 0], ["device", 1]], IndexError,
         r"step 3: device index 1 out of range on 'Top A' \(1 available\)"),
        ([["device", 0], ["chain", 0], ["device", 0], ["chain", 0]], ValueError,
         "device 'Shallow Wavetable' is not a rack"),
        ([["device", -1]], IndexError, "device index -1 out of range"),
        ([["device"]], ValueError, r"step 1 must be \[kind, index\]"),
        ([["device", "0"]], ValueError, "non-integer index"),
        ([["device", True]], ValueError, "non-integer index"),
    ])
    def test_rejects_bad_paths_naming_the_step(self, path, error, match):
        track, _, _ = nested_rack()
        with pytest.raises(error, match=match):
            make_script([track])._resolve_device_path(0, path)

    def test_rejects_unknown_track(self):
        with pytest.raises(IndexError, match="Track index 3 out of range"):
            make_script([])._resolve_device_path(3, [["device", 0]])

    def test_device_at_path_rejects_a_path_ending_on_a_chain(self):
        track, _, _ = nested_rack()
        with pytest.raises(ValueError, match="ends on chain 'Top B'; it must end on a device"):
            make_script([track])._device_at_path(0, [["device", 0], ["chain", 1]])


# ── parameters ───────────────────────────────────────────────────────


class TestDevicePathParameters:
    def test_read_parameters_of_the_deep_device(self):
        track, _, _ = nested_rack()
        result = make_script([track])._get_device_parameters_at_path(0, DEEP_PATH, True)
        assert result["device_name"] == "Deep Wavetable"
        assert result["parameter_count"] == 5
        names = [p["name"] for p in result["parameters"]]
        assert names == ["Device On", "Filter Freq", "Osc Type", "Locked", "Osc 1 Detune"]

    def test_read_has_the_same_shape_as_the_chain_index_form(self):
        track, outer, _ = nested_rack()
        script = make_script([track])
        via_path = script._get_device_parameters_at_path(0, [["device", 0], ["chain", 0],
                                                            ["device", 0]])
        via_chain = script._get_device_parameters(0, 0, 0, True)
        assert via_path == via_chain

    def test_set_then_read_back_three_levels_deep(self):
        track, _, deep = nested_rack()
        script = make_script([track])

        result = script._set_device_parameter_at_path(0, DEEP_PATH, "Osc 1 Detune", None, 0.75)

        assert result["parameter_name"] == "Osc 1 Detune"
        assert result["new_value"] == 0.75
        # 0.75 of -1..1 is 0.5 raw; the deep device holds it and no other device changed.
        assert deep.parameters[4].value == pytest.approx(0.5)
        read = script._get_device_parameters_at_path(0, DEEP_PATH)
        assert read["parameters"][4]["value"] == 0.75
        shallow = track.devices[0].chains[0].devices[0]
        assert shallow.parameters[4].value == 0.5

    def test_set_by_parameter_index(self):
        track, _, deep = nested_rack()
        make_script([track])._set_device_parameter_at_path(0, DEEP_PATH, None, 4, 1.0)
        assert deep.parameters[4].value == 1.0

    def test_set_rejects_a_disabled_parameter(self):
        track, _, _ = nested_rack()
        with pytest.raises(ValueError, match="'Locked' is currently disabled"):
            make_script([track])._set_device_parameter_at_path(0, DEEP_PATH, "Locked", None, 1.0)

    def test_errors_are_logged_and_raised(self):
        track, _, _ = nested_rack()
        script = make_script([track])
        for call in (
            lambda: script._get_device_parameters_at_path(0, [["device", 9]]),
            lambda: script._set_device_parameter_at_path(0, [["device", 9]], "x", None, 0.5),
        ):
            with pytest.raises(IndexError):
                call()
        assert script.log_message.call_count == 2


class TestDevicePathEnable:
    def test_disable_and_enable_only_the_deep_device(self):
        track, outer, deep = nested_rack()
        script = make_script([track])

        off = script._set_device_enabled_at_path(0, DEEP_PATH, False)
        assert off == {"device_name": "Deep Wavetable", "is_active": False}
        assert deep.parameters[0].value == 0.0
        assert outer.parameters[0].value == 1.0

        on = script._set_device_enabled_at_path(0, DEEP_PATH, True)
        assert on["is_active"] is True
        assert deep.parameters[0].value == 1.0

    def test_a_rack_in_the_path_can_itself_be_disabled(self):
        track, outer, deep = nested_rack()
        make_script([track])._set_device_enabled_at_path(0, [["device", 0]], False)
        assert outer.parameters[0].value == 0.0
        assert deep.parameters[0].value == 1.0

    def test_errors_are_logged_and_raised(self):
        track, _, _ = nested_rack()
        script = make_script([track])
        with pytest.raises(IndexError):
            script._set_device_enabled_at_path(0, [["device", 4]], True)
        script.log_message.assert_called_once()


# ── chain info ───────────────────────────────────────────────────────


class TestDevicePathChainInfo:
    def test_a_path_ending_on_a_nested_rack_lists_its_chains(self):
        track, _, _ = nested_rack()
        result = make_script([track])._get_chain_info_at_path(
            0, [["device", 0], ["chain", 1], ["device", 1]])
        assert result["device_name"] == "Middle Rack"
        assert result["chain_count"] == 1
        assert result["chains"][0]["name"] == "Mid"
        assert result["chains"][0]["devices"] == [
            {"index": 0, "name": "Innermost Rack", "type": "rack"}]

    def test_a_path_ending_on_a_chain_lists_its_devices(self):
        track, _, _ = nested_rack()
        result = make_script([track])._get_chain_info_at_path(
            0, [["device", 0], ["chain", 1], ["device", 1], ["chain", 0], ["device", 0],
                ["chain", 0]])
        assert result["chain_name"] == "Inner"
        assert [d["name"] for d in result["devices"]] == ["Deep Wavetable"]

    def test_the_replies_match_the_chain_index_form(self):
        track, _, _ = nested_rack()
        script = make_script([track])
        assert script._get_chain_info_at_path(0, [["device", 0]]) == script._get_chain_info(0, 0)
        assert (script._get_chain_info_at_path(0, [["device", 0], ["chain", 1]])
                == script._get_chain_info(0, 0, 1))

    def test_a_path_to_a_device_that_is_not_a_rack_is_rejected(self):
        track, _, _ = nested_rack()
        script = make_script([track])
        with pytest.raises(ValueError, match="'Deep Wavetable' is not a rack"):
            script._get_chain_info_at_path(0, DEEP_PATH)
        script.log_message.assert_called_once()


# ── dispatch ─────────────────────────────────────────────────────────


class TestDevicePathDispatch:
    def _run(self, script, command, params):
        return script._process_command({"type": command, "params": params})

    def test_get_device_parameters_with_a_path_uses_the_path_handler(self):
        script = dispatching_script([])
        script._get_device_parameters_at_path = MagicMock(return_value={"at": "path"})
        script._get_device_parameters = MagicMock()
        params = {"track_index": 1, "device_index": -1, "chain_index": None,
                  "device_path": DEEP_PATH, "show_all": True}
        assert self._run(script, "get_device_parameters", params)["result"] == {"at": "path"}
        script._get_device_parameters_at_path.assert_called_once_with(1, DEEP_PATH, True)
        script._get_device_parameters.assert_not_called()

    def test_get_chain_info_with_a_path_uses_the_path_handler(self):
        script = dispatching_script([])
        script._get_chain_info_at_path = MagicMock(return_value={"at": "path"})
        script._get_chain_info = MagicMock()
        params = {"track_index": 1, "device_index": -1, "device_path": DEEP_PATH}
        assert self._run(script, "get_chain_info", params)["result"] == {"at": "path"}
        script._get_chain_info_at_path.assert_called_once_with(1, DEEP_PATH)
        script._get_chain_info.assert_not_called()

    def test_set_device_parameter_with_a_path_uses_the_path_handler(self):
        script = dispatching_script([])
        script._set_device_parameter_at_path = MagicMock(return_value={"at": "path"})
        script._set_device_parameter = MagicMock()
        params = {"track_index": 1, "device_index": -1, "device_path": DEEP_PATH,
                  "parameter_name": "Osc 1 Detune", "parameter_index": None, "value": 0.25}
        assert self._run(script, "set_device_parameter", params)["result"] == {"at": "path"}
        script._set_device_parameter_at_path.assert_called_once_with(
            1, DEEP_PATH, "Osc 1 Detune", None, 0.25)
        script._set_device_parameter.assert_not_called()

    def test_set_device_enabled_with_a_path_uses_the_path_handler(self):
        script = dispatching_script([])
        script._set_device_enabled_at_path = MagicMock(return_value={"at": "path"})
        script._set_device_enabled = MagicMock()
        params = {"track_index": 1, "device_index": -1, "device_path": DEEP_PATH,
                  "enabled": False}
        assert self._run(script, "set_device_enabled", params)["result"] == {"at": "path"}
        script._set_device_enabled_at_path.assert_called_once_with(1, DEEP_PATH, False)
        script._set_device_enabled.assert_not_called()

    def test_requests_without_a_path_use_the_old_handlers(self):
        script = dispatching_script([])
        for name in ("_get_device_parameters", "_get_chain_info", "_set_device_parameter",
                     "_set_device_enabled"):
            setattr(script, name, MagicMock(return_value={"old": name}))
            setattr(script, name + "_at_path", MagicMock())
        base = {"track_index": 1, "device_index": 2, "chain_index": 3}
        assert self._run(script, "get_device_parameters", base)["result"] == {
            "old": "_get_device_parameters"}
        assert self._run(script, "get_chain_info", base)["result"] == {"old": "_get_chain_info"}
        assert self._run(script, "set_device_parameter", base)["result"] == {
            "old": "_set_device_parameter"}
        assert self._run(script, "set_device_enabled", base)["result"] == {
            "old": "_set_device_enabled"}
        for name in ("_get_device_parameters", "_get_chain_info", "_set_device_parameter",
                     "_set_device_enabled"):
            getattr(script, name + "_at_path").assert_not_called()

    def test_a_path_request_for_the_guard_index_would_fail_on_an_old_script(self):
        """The guard index the MCP server sends is rejected by the old resolver."""
        track, _, _ = nested_rack()
        with pytest.raises(IndexError, match="Device index -1 out of range"):
            make_script([track])._resolve_device(0, -1)

    def test_load_device_from_path_runs_on_the_main_thread(self):
        script = dispatching_script([])
        script._load_device_from_path = MagicMock(return_value={"loaded": True})
        response = self._run(script, "load_device_from_path", {"track_index": 2, "path": "a/b"})
        assert response == {"status": "success", "result": {"loaded": True}}
        script._load_device_from_path.assert_called_once_with(2, "a/b")
        assert script.schedule_message.call_args[0][0] == 0


# ── browser stubs ────────────────────────────────────────────────────


class BItem:
    def __init__(self, name, children=None, loadable=False):
        self.name = name
        self.children = list(children) if children is not None else []
        self.is_folder = children is not None
        self.is_loadable = loadable
        self.is_device = loadable
        self.uri = "query:UserLibrary#" + name


def user_library():
    return BItem("User Library", [
        BItem("Presets", [
            BItem("MIDI Effects", [
                BItem("Max MIDI Effect", [BItem("Chord Gen.amxd", loadable=True)]),
            ]),
            BItem("Instruments", [
                BItem("Wavetable", [BItem("Bass Pad.adv", loadable=True)]),
                BItem("Instrument Rack", [BItem("Layered Keys.adg", loadable=True)]),
            ]),
        ]),
        BItem("Empty Folder", []),
        BItem("Pad.adv", loadable=True),
        BItem("Pad.adg", loadable=True),
        BItem("Notes.txt", loadable=False),
    ])


def make_browser(library=None, **roots):
    browser = types.SimpleNamespace(user_library=library or user_library(), load_item=MagicMock())
    for name, value in roots.items():
        setattr(browser, name, value)
    return browser


def loading_script(browser, devices=None, adds=("Loaded Device", "Device")):
    """A script on one track whose browser adds a device (name, class) on load."""
    track = Track("Test Track", devices or [])
    script = make_script([track])
    script.application.return_value = types.SimpleNamespace(browser=browser)

    def load(item):
        if adds is not None:
            name, class_name = adds
            track.devices.append(Device(name, class_name, name, parameters=[Param("Device On")]))

    browser.load_item.side_effect = load
    return script, track


# ── load_device_from_path ────────────────────────────────────────────


class TestLoadDeviceFromPath:
    def test_loads_an_amxd_by_relative_path(self):
        browser = make_browser()
        script, track = loading_script(browser, adds=("Chord Gen", "MxDeviceMidiEffect"))

        result = script._load_device_from_path(
            0, "Presets/MIDI Effects/Max MIDI Effect/Chord Gen.amxd")

        loaded = browser.load_item.call_args[0][0]
        assert loaded.name == "Chord Gen.amxd"
        assert result == {
            "loaded": True, "item_name": "Chord Gen.amxd",
            "uri": "query:UserLibrary#Chord Gen.amxd", "track_name": "Test Track",
            "device_name": "Chord Gen", "device_index": 0, "devices_added": 1,
        }
        assert script._song.view.selected_track is track

    def test_loads_an_adv_preset_after_existing_devices(self):
        browser = make_browser()
        script, track = loading_script(
            browser, devices=[wavetable("Existing")], adds=("Bass Pad", "InstrumentVector"))

        result = script._load_device_from_path(0, "Presets/Instruments/Wavetable/Bass Pad.adv")

        assert result["item_name"] == "Bass Pad.adv"
        assert result["device_name"] == "Bass Pad"
        assert result["device_index"] == 1
        assert [d.name for d in track.devices] == ["Existing", "Bass Pad"]

    def test_loads_an_adg_rack(self):
        browser = make_browser()
        script, _ = loading_script(browser, adds=("Layered Keys", "InstrumentGroupDevice"))

        result = script._load_device_from_path(
            0, "Presets/Instruments/Instrument Rack/Layered Keys.adg")

        assert browser.load_item.call_args[0][0].name == "Layered Keys.adg"
        assert (result["device_name"], result["device_index"]) == ("Layered Keys", 0)

    @pytest.mark.parametrize("path", [
        "Presets/MIDI Effects/Max MIDI Effect/Chord Gen",
        "presets/midi effects/max midi effect/chord gen.AMXD",
        "/Presets/MIDI Effects//Max MIDI Effect/Chord Gen.amxd/",
        "  Presets / MIDI Effects / Max MIDI Effect / Chord Gen.amxd  ",
    ], ids=["no-extension-amxd", "case-insensitive-amxd", "extra-slashes-amxd", "padded-amxd"])
    def test_tolerates_case_slashes_and_a_missing_extension(self, path):
        browser = make_browser()
        script, _ = loading_script(browser, adds=("Chord Gen", "MxDeviceMidiEffect"))
        script._load_device_from_path(0, path)
        assert browser.load_item.call_args[0][0].name == "Chord Gen.amxd"

    def test_a_new_device_inserted_before_the_existing_ones_gets_its_own_index(self):
        browser = make_browser()
        track = Track("Test Track", [wavetable("Existing")])
        script = make_script([track])
        script.application.return_value = types.SimpleNamespace(browser=browser)
        browser.load_item.side_effect = lambda item: track.devices.insert(
            0, Device("Chord Gen", "MxDeviceMidiEffect", "Chord Gen"))

        result = script._load_device_from_path(0, "Presets/MIDI Effects/Max MIDI Effect/Chord Gen")

        assert (result["device_name"], result["device_index"]) == ("Chord Gen", 0)

    def test_devices_that_cannot_be_compared_fall_back_to_names(self):
        """Live hands out fresh objects for each read, so none equals an earlier one."""
        class Fresh(Device):
            def __eq__(self, other):
                return False

            __hash__ = object.__hash__

        browser = make_browser()
        track = Track("Test Track", [Fresh("A"), Fresh("B")])
        script = make_script([track])
        script.application.return_value = types.SimpleNamespace(browser=browser)

        def load(item):
            names = ["A", "Bass Pad", "B"]
            track.devices = [Fresh(name) for name in names]

        browser.load_item.side_effect = load

        result = script._load_device_from_path(0, "Presets/Instruments/Wavetable/Bass Pad.adv")

        assert (result["device_name"], result["device_index"]) == ("Bass Pad", 1)

    def test_a_new_last_device_is_found_by_name_when_devices_cannot_be_compared(self):
        class Fresh(Device):
            def __eq__(self, other):
                return False

            __hash__ = object.__hash__

        browser = make_browser()
        track = Track("Test Track", [Fresh("A")])
        script = make_script([track])
        script.application.return_value = types.SimpleNamespace(browser=browser)
        browser.load_item.side_effect = lambda item: setattr(
            track, "devices", [Fresh("A"), Fresh("Bass Pad")])

        result = script._load_device_from_path(0, "Presets/Instruments/Wavetable/Bass Pad.adv")

        assert (result["device_name"], result["device_index"]) == ("Bass Pad", 1)

    def test_a_reload_that_adds_nothing_is_an_error_even_when_devices_cannot_be_compared(self):
        class Fresh(Device):
            def __eq__(self, other):
                return False

            __hash__ = object.__hash__

        browser = make_browser()
        track = Track("Test Track", [Fresh("A")])
        script = make_script([track])
        script.application.return_value = types.SimpleNamespace(browser=browser)
        browser.load_item.side_effect = lambda item: setattr(track, "devices", [Fresh("A")])

        with pytest.raises(RuntimeError, match="no new device appeared"):
            script._load_device_from_path(0, "Presets/Instruments/Wavetable/Bass Pad.adv")

    def test_logs_and_raises_on_failure(self):
        script, _ = loading_script(make_browser())
        with pytest.raises(ValueError):
            script._load_device_from_path(0, "Nope/Missing.adv")
        assert script.log_message.call_count >= 1


class TestBrowserPathRoots:
    def test_the_user_library_is_the_default_root(self):
        browser = make_browser()
        script, _ = loading_script(browser)
        assert script._find_browser_file(browser, "Presets/Instruments/Wavetable/Bass Pad.adv"
                                         ).name == "Bass Pad.adv"

    @pytest.mark.parametrize("prefix", ["User Library", "user_library", "USER-LIBRARY"])
    def test_a_user_library_prefix_is_accepted(self, prefix):
        browser = make_browser()
        script, _ = loading_script(browser)
        item = script._find_browser_file(
            browser, prefix + "/Presets/Instruments/Wavetable/Bass Pad.adv")
        assert item.name == "Bass Pad.adv"

    def test_current_project_root(self):
        project = BItem("Set Project", [BItem("Samples", [BItem("Lead.adv", loadable=True)])])
        browser = make_browser(current_project=project)
        script, _ = loading_script(browser)
        assert script._find_browser_file(browser, "Current Project/Samples/Lead.adv"
                                         ).name == "Lead.adv"

    def test_packs_root(self):
        packs = BItem("Packs", [BItem("Core Library", [BItem("Pad.adg", loadable=True)])])
        browser = make_browser(packs=packs)
        script, _ = loading_script(browser)
        assert script._find_browser_file(browser, "Packs/Core Library/Pad.adg") is (
            packs.children[0].children[0])

    def test_a_user_added_places_folder_is_a_root(self):
        folder = BItem("Sound Design", [BItem("Hits", [BItem("Zap.adv", loadable=True)])])
        browser = make_browser(user_folders=[folder])
        script, _ = loading_script(browser)
        assert script._find_browser_file(browser, "Sound Design/Hits/Zap.adv").name == "Zap.adv"

    def test_a_user_library_folder_wins_over_a_places_folder_of_the_same_name(self):
        shadow = BItem("Presets", [BItem("Other.adv", loadable=True)])
        browser = make_browser(user_folders=[shadow])
        script, _ = loading_script(browser)
        item = script._find_browser_file(browser, "Presets/Instruments/Wavetable/Bass Pad.adv")
        assert item.name == "Bass Pad.adv"

    def test_a_missing_root_falls_back_to_the_user_library_and_reports_it(self):
        browser = make_browser(current_project=None, user_folders=None)
        script, _ = loading_script(browser)
        with pytest.raises(ValueError, match="Path part 'Current Project' not found in "
                                             "'User Library'"):
            script._find_browser_file(browser, "Current Project/x.adv")

    def test_a_browser_without_a_user_library_is_reported(self):
        browser = types.SimpleNamespace(user_library=None, load_item=MagicMock())
        script, _ = loading_script(browser)
        with pytest.raises(ValueError, match="no User Library"):
            script._find_browser_file(browser, "Presets/x.adv")

    def test_a_root_name_alone_is_not_a_file(self):
        browser = make_browser()
        script, _ = loading_script(browser)
        with pytest.raises(ValueError, match="'User Library' is a browser location, not a file"):
            script._find_browser_file(browser, "User Library")

    def test_an_exact_name_beats_an_extension_match(self):
        library = BItem("User Library", [BItem("Pad", [BItem("x.adv", loadable=True)]),
                                         BItem("Pad.adv", loadable=True)])
        script, _ = loading_script(make_browser(library))
        assert script._find_browser_child(library, "Pad").is_folder
        assert not script._find_browser_child(library, "Pad.adv").is_folder


class TestLoadDeviceFromPathErrors:
    def _script(self, **kwargs):
        browser = make_browser()
        script, track = loading_script(browser, **kwargs)
        return script, track, browser

    def test_a_missing_part_lists_what_is_there(self):
        script, track, browser = self._script()
        with pytest.raises(ValueError, match=r"Path part 'Nope' not found in 'Presets'. "
                                              r"It holds: MIDI Effects, Instruments"):
            script._load_device_from_path(0, "Presets/Nope/Thing.adv")
        browser.load_item.assert_not_called()
        assert track.devices == []

    def test_a_long_listing_is_cut_short(self):
        library = BItem("User Library", [BItem("Item {0}".format(i), []) for i in range(12)])
        script, _ = loading_script(make_browser(library))
        with pytest.raises(ValueError, match=r"\.\.\. \(12 in all\)"):
            script._load_device_from_path(0, "Missing.adv")

    def test_an_empty_folder_is_listed_as_holding_nothing(self):
        script, _, _ = self._script()
        with pytest.raises(ValueError, match="'Empty Folder'. It holds: nothing"):
            script._load_device_from_path(0, "Empty Folder/x.adv")

    def test_a_folder_is_not_loadable(self):
        script, _, browser = self._script()
        with pytest.raises(ValueError, match="'Presets' is a folder, not something that can be "
                                              "loaded"):
            script._load_device_from_path(0, "Presets")
        browser.load_item.assert_not_called()

    def test_an_unloadable_file_is_rejected(self):
        script, _, browser = self._script()
        with pytest.raises(ValueError, match="'Notes.txt' is not loadable"):
            script._load_device_from_path(0, "Notes.txt")
        browser.load_item.assert_not_called()

    def test_an_ambiguous_extensionless_name_asks_for_the_extension(self):
        script, _, browser = self._script()
        with pytest.raises(ValueError, match="matches several items in 'User Library': "
                                              "Pad.adv, Pad.adg. Include the file extension"):
            script._load_device_from_path(0, "Pad")
        browser.load_item.assert_not_called()

    def test_the_extension_picks_between_same_named_files(self):
        script, _, browser = self._script(adds=("Pad", "AudioEffectGroupDevice"))
        script._load_device_from_path(0, "Pad.adg")
        assert browser.load_item.call_args[0][0].name == "Pad.adg"

    @pytest.mark.parametrize("path", ["", "   ", "//"])
    def test_an_empty_path_is_rejected(self, path):
        script, _, browser = self._script()
        with pytest.raises(ValueError, match="path is empty"):
            script._load_device_from_path(0, path)
        browser.load_item.assert_not_called()

    @pytest.mark.parametrize("path", ["../Presets/x.adv", "Presets/./x.adv"])
    def test_dot_segments_are_rejected(self, path):
        script, _, browser = self._script()
        with pytest.raises(ValueError, match=r"must not contain '\.' or '\.\.'"):
            script._load_device_from_path(0, path)
        browser.load_item.assert_not_called()

    def test_a_load_that_adds_no_device_is_an_error(self):
        script, _, _ = self._script(adds=None)
        with pytest.raises(RuntimeError, match="Live loaded 'Pad.adv' but no new device "
                                               "appeared on track 'Test Track'"):
            script._load_device_from_path(0, "Pad.adv")

    def test_an_unknown_track_is_rejected_before_the_browser_is_touched(self):
        script, _, browser = self._script()
        with pytest.raises(IndexError, match="Track index out of range"):
            script._load_device_from_path(4, "Pad.adv")
        browser.load_item.assert_not_called()

    def test_no_browser_is_reported(self):
        script, _, _ = self._script()
        script.application.return_value = None
        with pytest.raises(RuntimeError, match="Browser is not available"):
            script._load_device_from_path(0, "Pad.adv")
