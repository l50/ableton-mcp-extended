"""Remote Script tests for the recording-safety handlers.

Covers track addressing (_resolve_track and the expect_name guard), arm,
mute/solo, input routing, monitoring, the validated set_tracks_state batch,
get_recording_status, list_tracks and the extended get_track_info. Live is
replaced by small stub objects; no running Live instance is needed.
"""

import os
import sys
import types
from unittest.mock import MagicMock

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


IN, AUTO, OFF = 0, 1, 2


class _Routing:
    def __init__(self, name):
        self.display_name = name

    def __repr__(self):
        return "_Routing({0!r})".format(self.display_name)


_DEFAULT_INPUTS = {
    "Ext. In": ["1", "2", "1/2"],
    "No Input": [],
    "Resampling": ["Post FX"],
}


class _Track:
    """A Live audio or MIDI track with routing and monitoring.

    Setting input_routing_type swaps the channel list and picks the first
    channel, as Live does.
    """

    is_foldable = False

    def __init__(self, name, kind="audio", arm=False, input_type="Ext. In",
                 input_channel="1", monitoring=AUTO, inputs=None):
        self.name = name
        self.mute = False
        self.solo = False
        self.arm = arm
        self.implicit_arm = False
        self.muted_via_solo = False
        self.can_be_armed = True
        self.is_frozen = False
        self.is_grouped = False
        self.group_track = None
        self.is_visible = True
        self.color = 123
        self.has_audio_input = kind == "audio"
        self.has_midi_input = kind == "midi"
        self.clip_slots = []
        self.devices = []
        self.arrangement_clips = []
        self.mixer_device = MagicMock()
        self.mixer_device.volume.value = 0.85
        self.mixer_device.panning.value = 0.0
        self.current_monitoring_state = monitoring
        self._types = dict((n, _Routing(n)) for n in (inputs or _DEFAULT_INPUTS))
        self._channels = dict(
            (n, [_Routing(c) for c in chans])
            for n, chans in (inputs or _DEFAULT_INPUTS).items())
        self._input_type = self._types[input_type]
        self.input_routing_channel = self._find_channel(input_channel)
        self.output_routing_type = _Routing("Master")
        self.output_routing_channel = _Routing("Track In")
        self.available_output_routing_types = [_Routing("Master"),
                                               _Routing("Sends Only")]
        self.available_output_routing_channels = [_Routing("Track In")]

    def _find_channel(self, name):
        for channel in self._channels[self._input_type.display_name]:
            if channel.display_name == name:
                return channel
        return None

    @property
    def available_input_routing_types(self):
        return list(self._types.values())

    @property
    def available_input_routing_channels(self):
        return list(self._channels[self._input_type.display_name])

    @property
    def input_routing_type(self):
        return self._input_type

    @input_routing_type.setter
    def input_routing_type(self, value):
        self._input_type = value
        channels = self._channels[value.display_name]
        self.input_routing_channel = channels[0] if channels else None


class _GroupTrack:
    is_foldable = True

    def __init__(self, name="Bus", fold_state=1):
        self.name = name
        self.mute = False
        self.solo = False
        self.fold_state = fold_state
        self.is_grouped = False
        self.group_track = None
        self.has_audio_input = False
        self.has_midi_input = False
        self.clip_slots = []
        self.devices = []
        self.mixer_device = MagicMock()
        self.output_routing_type = _Routing("Master")
        self.output_routing_channel = _Routing("Track In")
        self.available_output_routing_types = [_Routing("Master")]
        self.available_output_routing_channels = [_Routing("Track In")]

    @property
    def arm(self):
        raise RuntimeError("Master and Return Tracks have no 'Arm' state!")

    @property
    def arrangement_clips(self):
        raise RuntimeError("Group tracks have no arrangement clips")


class _ReturnTrack:
    is_foldable = False
    can_be_armed = False

    def __init__(self, name="A-Reverb"):
        self.name = name
        self.mute = False
        self.solo = False
        self.devices = []
        self.output_routing_type = _Routing("Master")
        self.output_routing_channel = _Routing("Track In")

    @property
    def arm(self):
        raise RuntimeError("Master and Return Tracks have no 'Arm' state!")


class _MasterTrack:
    is_foldable = False
    can_be_armed = False

    def __init__(self):
        self.name = "Master"
        self.devices = [object(), object()]
        self.output_routing_type = _Routing("1/2")
        self.output_routing_channel = None

    @property
    def mute(self):
        raise RuntimeError("Master track has no mute")


class _Song:
    def __init__(self, tracks=(), returns=()):
        self.tracks = list(tracks)
        self.return_tracks = list(returns)
        self.master_track = _MasterTrack()
        self.is_playing = False
        self.record_mode = False
        self.session_record = False
        self.arrangement_overdub = False
        self.punch_in = False
        self.punch_out = False
        self.metronome = True
        self.count_in_duration = 0
        self.loop = False
        self.exclusive_arm = True
        self.exclusive_solo = True


def _make_script(tracks=(), returns=()):
    script = AbletonMCP.__new__(AbletonMCP)
    script.log_message = lambda msg: None
    script._song = _Song(tracks, returns)
    return script


def _dispatch(script, command_type, params=None):
    script.schedule_message = MagicMock(side_effect=AssertionError)
    return script._process_command({"type": command_type,
                                    "params": params or {}})


@pytest.fixture(autouse=True)
def _no_live_module(monkeypatch):
    """Use the built-in monitoring values unless a test installs Live."""
    monkeypatch.delitem(sys.modules, "Live", raising=False)


# ── _resolve_track and expect_name ──────────────────────────────────


class TestResolveTrack:
    def test_by_index(self):
        a, b = _Track("Gtr"), _Track("Bass")
        script = _make_script([a, b])
        assert script._resolve_track({"track_index": 1}) is b

    def test_by_name_is_case_insensitive(self):
        a = _Track("Gtr DI")
        script = _make_script([_Track("Bass"), a])
        assert script._resolve_track({"track_name": "gtr di"}) is a

    def test_name_not_found_lists_the_tracks(self):
        script = _make_script([_Track("Gtr"), _Track("Bass")])
        with pytest.raises(ValueError) as excinfo:
            script._resolve_track({"track_name": "Vox"})
        assert "No track named 'Vox'" in str(excinfo.value)
        assert "'Gtr', 'Bass'" in str(excinfo.value)

    def test_name_not_found_in_an_empty_set(self):
        script = _make_script([])
        with pytest.raises(ValueError) as excinfo:
            script._resolve_track({"track_name": "Vox"})
        assert "(none)" in str(excinfo.value)

    def test_ambiguous_name_lists_every_candidate(self):
        ret = _ReturnTrack("Gtr")
        script = _make_script([_Track("Gtr"), _Track("Bass"), _Track("GTR")],
                              [ret])
        with pytest.raises(ValueError) as excinfo:
            script._resolve_track({"track_name": "gtr"}, allow_returns=True)
        message = str(excinfo.value)
        assert "3 tracks match" in message
        assert "'Gtr' (track_index 1)" in message
        assert "'GTR' (track_index 3)" in message
        assert "'Gtr' (return_track A)" in message

    def test_name_can_match_a_return_when_allowed(self):
        ret = _ReturnTrack("Verb")
        script = _make_script([_Track("Gtr")], [ret])
        assert script._resolve_track({"track_name": "verb"},
                                     allow_returns=True) is ret
        with pytest.raises(ValueError):
            script._resolve_track({"track_name": "verb"})

    def test_by_return_index(self):
        ret = _ReturnTrack("B-Delay")
        script = _make_script([_Track("Gtr")], [_ReturnTrack(), ret])
        assert script._resolve_track({"return_track_index": 1},
                                     allow_returns=True) is ret

    def test_return_refused_when_not_allowed(self):
        script = _make_script([_Track("Gtr")], [_ReturnTrack()])
        with pytest.raises(ValueError) as excinfo:
            script._resolve_track({"return_track_index": 0})
        assert "does not apply to return tracks" in str(excinfo.value)

    def test_return_out_of_range(self):
        script = _make_script([_Track("Gtr")], [_ReturnTrack()])
        with pytest.raises(IndexError) as excinfo:
            script._resolve_track({"return_track_index": 2},
                                  allow_returns=True)
        assert "return_track C does not exist" in str(excinfo.value)
        assert "1 return track(s)" in str(excinfo.value)

    def test_index_out_of_range_uses_one_based_numbers(self):
        script = _make_script([_Track("Gtr"), _Track("Bass")])
        with pytest.raises(IndexError) as excinfo:
            script._resolve_track({"track_index": 5})
        assert "track_index 6 is out of range" in str(excinfo.value)
        assert "(1-2)" in str(excinfo.value)

    def test_negative_index_is_out_of_range(self):
        script = _make_script([_Track("Gtr")])
        with pytest.raises(IndexError):
            script._resolve_track({"track_index": -1})

    @pytest.mark.parametrize("params", [
        {},
        {"track_index": 0, "track_name": "Gtr"},
        {"track_name": "Gtr", "return_track_index": 0},
    ])
    def test_needs_exactly_one_reference(self, params):
        script = _make_script([_Track("Gtr")], [_ReturnTrack()])
        with pytest.raises(ValueError) as excinfo:
            script._resolve_track(params, allow_returns=True)
        assert "exactly one of" in str(excinfo.value)

    def test_expect_name_match_passes(self):
        track = _Track("Gtr DI")
        script = _make_script([track])
        assert script._resolve_track({"track_index": 0,
                                      "expect_name": "GTR di"}) is track

    def test_expect_name_mismatch_raises(self):
        script = _make_script([_Track("Bass")])
        with pytest.raises(ValueError) as excinfo:
            script._resolve_track({"track_index": 0, "expect_name": "Gtr"})
        assert "expected 'Gtr'" in str(excinfo.value)
        assert "is 'Bass'" in str(excinfo.value)

    def test_expect_name_guard_blocks_a_write(self):
        track = _Track("Bass")
        script = _make_script([track])
        with pytest.raises(ValueError):
            script._set_track_mute({"track_index": 0, "expect_name": "Gtr",
                                    "mute": True})
        assert track.mute is False


class TestSmallHelpers:
    def test_return_label_past_z_is_numeric(self):
        assert AbletonMCP._return_track_label(0) == "A"
        assert AbletonMCP._return_track_label(25) == "Z"
        assert AbletonMCP._return_track_label(26) == "27"

    def test_safe_attr_swallows_live_errors(self):
        assert AbletonMCP._safe_attr(_GroupTrack(), "arm", "x") == "x"

    def test_routing_name_of_none(self):
        assert AbletonMCP._routing_name(None) is None

    def test_track_ref_of_master(self):
        script = _make_script([_Track("Gtr")])
        ref = script._track_ref(script._song.master_track)
        assert ref == {"master": True, "name": "Master"}

    def test_track_kind(self):
        midi, audio, odd = _Track("M", "midi"), _Track("A"), _Track("O", "x")
        group, ret = _GroupTrack(), _ReturnTrack()
        script = _make_script([midi, audio, odd, group], [ret])
        assert script._track_kind(midi) == "midi"
        assert script._track_kind(audio) == "audio"
        assert script._track_kind(odd) == "unknown"
        assert script._track_kind(group) == "group"
        assert script._track_kind(ret) == "return"
        assert script._track_kind(script._song.master_track) == "master"

    def test_monitoring_name_missing_and_unknown(self):
        script = _make_script()
        assert script._monitoring_name(_ReturnTrack()) is None
        track = _Track("Gtr", monitoring=7)
        assert script._monitoring_name(track) == "7"

    def test_monitoring_states_use_live_when_importable(self, monkeypatch):
        states = types.SimpleNamespace(IN=10, AUTO=11, OFF=12)
        live = types.ModuleType("Live")
        live.Track = types.SimpleNamespace(
            Track=types.SimpleNamespace(monitoring_states=states))
        monkeypatch.setitem(sys.modules, "Live", live)
        track = _Track("Gtr", monitoring=12)
        script = _make_script([track])

        assert script._monitoring_name(track) == "off"
        script._set_track_monitoring({"track_index": 0, "state": "in"})
        assert track.current_monitoring_state == 10


# ── mute / solo ─────────────────────────────────────────────────────


class TestMuteAndSolo:
    def test_mute_by_name_reports_solo_state(self):
        track = _Track("Gtr")
        track.solo = True
        script = _make_script([track])
        result = script._set_track_mute({"track_name": "gtr", "mute": True})
        assert result == {"track": {"index": 0, "name": "Gtr"}, "mute": True,
                          "solo": True, "muted_via_solo": False}

    def test_mute_a_return_by_index(self):
        ret = _ReturnTrack("B-Delay")
        script = _make_script([_Track("Gtr")], [_ReturnTrack(), ret])
        result = script._set_track_mute({"return_track_index": 1,
                                         "mute": True})
        assert ret.mute is True
        assert result["track"] == {"return_track": "B", "name": "B-Delay"}

    def test_exclusive_solo_unsolos_tracks_and_returns(self):
        a, b, c = _Track("Gtr"), _Track("Bass"), _Track("Drums")
        ret = _ReturnTrack("Verb")
        b.solo = True
        ret.solo = True
        script = _make_script([a, b, c], [ret])
        result = script._set_track_solo({"track_index": 0, "solo": True,
                                         "exclusive": True})
        assert a.solo is True and b.solo is False and ret.solo is False
        assert result["unsoloed"] == ["Bass", "Verb"]

    def test_non_exclusive_solo_leaves_others(self):
        a, b = _Track("Gtr"), _Track("Bass")
        b.solo = True
        script = _make_script([a, b])
        result = script._set_track_solo({"track_index": 0, "solo": True})
        assert b.solo is True and result["unsoloed"] == []

    def test_unsolo_with_exclusive_does_not_touch_others(self):
        a, b = _Track("Gtr"), _Track("Bass")
        a.solo = b.solo = True
        script = _make_script([a, b])
        script._set_track_solo({"track_index": 0, "solo": False,
                                "exclusive": True})
        assert a.solo is False and b.solo is True

    def test_solo_error_is_raised(self):
        script = _make_script([_Track("Gtr")])
        with pytest.raises(ValueError):
            script._set_track_solo({"track_name": "nope", "solo": True})


# ── arm ─────────────────────────────────────────────────────────────


class TestArm:
    def test_exclusive_arm_disarms_other_armable_tracks(self):
        a = _Track("Gtr")
        b = _Track("Bandmate", arm=True)
        c = _Track("Click", arm=True)
        c.can_be_armed = False
        group = _GroupTrack("Bus")
        script = _make_script([a, b, c, group])
        result = script._set_track_arm({"track_index": 0, "arm": True,
                                        "exclusive": True})
        assert a.arm is True and b.arm is False
        assert c.arm is True  # cannot be armed, so it is left alone
        assert result["disarmed"] == ["Bandmate"]
        assert result["warnings"] == []

    def test_arm_reports_input_monitoring_and_implicit_arm(self):
        track = _Track("Gtr", input_type="Ext. In", input_channel="2",
                       monitoring=IN)
        track.implicit_arm = True
        script = _make_script([track, _Track("Other", arm=True)])
        result = script._set_track_arm({"track_name": "Gtr", "arm": True})
        assert result["input"] == {"type": "Ext. In", "channel": "2"}
        assert result["monitoring"] == "in"
        assert result["implicit_arm"] is True
        joined = " | ".join(result["warnings"])
        assert "monitoring 'In'" in joined
        assert "Other tracks are still armed: Other" in joined
        assert "implicit_arm" in joined

    def test_arm_with_no_input_warns(self):
        track = _Track("Guide", input_type="No Input", input_channel=None)
        script = _make_script([track])
        result = script._set_track_arm({"track_index": 0, "arm": True})
        assert any("No Input" in w for w in result["warnings"])

    def test_disarm_has_no_input_warnings(self):
        track = _Track("Guide", arm=True, input_type="No Input",
                       input_channel=None, monitoring=IN)
        script = _make_script([track])
        result = script._set_track_arm({"track_index": 0, "arm": False})
        assert track.arm is False
        assert result["warnings"] == []

    def test_frozen_track_cannot_be_armed_but_can_be_disarmed(self):
        track = _Track("Frozen", arm=True)
        track.is_frozen = True
        script = _make_script([track])
        with pytest.raises(ValueError) as excinfo:
            script._set_track_arm({"track_index": 0, "arm": True})
        assert "frozen" in str(excinfo.value)
        script._set_track_arm({"track_index": 0, "arm": False})
        assert track.arm is False


# ── routing options and input ───────────────────────────────────────


class TestRoutingOptions:
    def test_audio_track_lists_inputs_and_outputs(self):
        script = _make_script([_Track("Gtr")])
        result = script._get_track_routing_options({"track_index": 0})
        assert result["track"] == {"index": 0, "name": "Gtr"}
        assert result["input"]["current"] == {"type": "Ext. In",
                                              "channel": "1"}
        assert result["input"]["types"] == ["Ext. In", "No Input",
                                            "Resampling"]
        assert result["input"]["channels"] == ["1", "2", "1/2"]
        assert result["output"]["types"] == ["Master", "Sends Only"]

    def test_group_has_no_input(self):
        script = _make_script([_GroupTrack("Bus")])
        result = script._get_track_routing_options({"track_index": 0})
        assert result["input"] is None
        assert result["output"]["current"]["type"] == "Master"

    def test_return_track_by_letter(self):
        script = _make_script([_Track("Gtr")], [_ReturnTrack("Verb")])
        result = script._get_track_routing_options({"return_track_index": 0})
        assert result["track"]["return_track"] == "A"
        assert result["input"] is None
        assert result["output"]["types"] == []

    def test_error_is_raised(self):
        script = _make_script([_Track("Gtr")])
        with pytest.raises(IndexError):
            script._get_track_routing_options({"track_index": 3})


class TestSetTrackInput:
    def test_sets_type_and_channel(self):
        track = _Track("Gtr", input_type="No Input", input_channel=None)
        script = _make_script([track])
        result = script._set_track_input({"track_index": 0,
                                          "input_type": "ext. in",
                                          "input_channel": "1/2"})
        assert track.input_routing_type.display_name == "Ext. In"
        assert track.input_routing_channel.display_name == "1/2"
        assert result["input"] == {"type": "Ext. In", "channel": "1/2"}
        assert result["available_channels"] == ["1", "2", "1/2"]
        assert result["arm"] is False
        assert result["warnings"] == []

    def test_same_type_only_changes_the_channel(self):
        track = _Track("Gtr", input_channel="1")
        original_type = track.input_routing_type
        script = _make_script([track])
        script._set_track_input({"track_index": 0, "input_type": "Ext. In",
                                 "input_channel": "2"})
        assert track.input_routing_type is original_type
        assert track.input_routing_channel.display_name == "2"

    def test_empty_channel_keeps_lives_default(self):
        track = _Track("Gtr", input_type="No Input", input_channel=None)
        script = _make_script([track])
        script._set_track_input({"track_index": 0, "input_type": "Ext. In"})
        assert track.input_routing_channel.display_name == "1"

    def test_unknown_type_lists_valid_names(self):
        track = _Track("Gtr")
        script = _make_script([track])
        with pytest.raises(ValueError) as excinfo:
            script._set_track_input({"track_index": 0,
                                     "input_type": "USB"})
        assert "No input type named 'USB'" in str(excinfo.value)
        assert "'Ext. In', 'No Input', 'Resampling'" in str(excinfo.value)

    def test_unknown_channel_after_type_change_restores_the_input(self):
        track = _Track("Gtr", input_type="Ext. In", input_channel="2")
        script = _make_script([track])
        with pytest.raises(ValueError) as excinfo:
            script._set_track_input({"track_index": 0,
                                     "input_type": "Resampling",
                                     "input_channel": "9"})
        assert "left unchanged" in str(excinfo.value)
        assert "'Post FX'" in str(excinfo.value)
        assert track.input_routing_type.display_name == "Ext. In"
        assert track.input_routing_channel.display_name == "2"

    def test_unknown_channel_from_no_input_restores_without_channel(self):
        track = _Track("Gtr", input_type="No Input", input_channel=None)
        script = _make_script([track])
        with pytest.raises(ValueError):
            script._set_track_input({"track_index": 0,
                                     "input_type": "Ext. In",
                                     "input_channel": "9"})
        assert track.input_routing_type.display_name == "No Input"

    def test_unknown_channel_with_same_type_is_plain_error(self):
        track = _Track("Gtr", input_channel="2")
        script = _make_script([track])
        with pytest.raises(ValueError) as excinfo:
            script._set_track_input({"track_index": 0,
                                     "input_type": "Ext. In",
                                     "input_channel": "9"})
        assert "left unchanged" not in str(excinfo.value)
        assert track.input_routing_channel.display_name == "2"

    def test_input_type_is_required(self):
        script = _make_script([_Track("Gtr")])
        with pytest.raises(ValueError) as excinfo:
            script._set_track_input({"track_index": 0})
        assert "input_type is required" in str(excinfo.value)

    def test_group_is_refused(self):
        script = _make_script([_GroupTrack("Bus")])
        with pytest.raises(ValueError) as excinfo:
            script._set_track_input({"track_index": 0,
                                     "input_type": "Ext. In"})
        assert "group track" in str(excinfo.value)

    def test_return_is_refused(self):
        ret = _ReturnTrack("Verb")
        script = _make_script([_Track("Gtr")], [ret])
        with pytest.raises(ValueError) as excinfo:
            script._require_input_routing(ret)
        assert "return track" in str(excinfo.value)

    def test_old_live_without_routing_api(self):
        track = _Track("Gtr")
        script = _make_script([track])
        old = types.SimpleNamespace(name="Old", is_foldable=False)
        script._song.tracks = [old]
        with pytest.raises(ValueError) as excinfo:
            script._set_track_input({"track_index": 0,
                                     "input_type": "Ext. In"})
        assert "not available in this Live version" in str(excinfo.value)

    def test_no_input_while_armed_warns(self):
        track = _Track("Guide", arm=True)
        script = _make_script([track])
        result = script._set_track_input({"track_index": 0,
                                          "input_type": "No Input"})
        assert result["input"] == {"type": "No Input", "channel": None}
        assert "still armed" in result["warnings"][0]


# ── monitoring ──────────────────────────────────────────────────────


class TestSetTrackMonitoring:
    @pytest.mark.parametrize("state,value", [("in", IN), ("AUTO", AUTO),
                                             ("off", OFF)])
    def test_sets_each_state(self, state, value):
        track = _Track("Gtr")
        script = _make_script([track])
        result = script._set_track_monitoring({"track_index": 0,
                                               "state": state})
        assert track.current_monitoring_state == value
        assert result == {"track": {"index": 0, "name": "Gtr"},
                          "monitoring": state.lower()}

    def test_invalid_state(self):
        script = _make_script([_Track("Gtr")])
        with pytest.raises(ValueError) as excinfo:
            script._set_track_monitoring({"track_index": 0,
                                          "state": "loud"})
        assert "in, auto, off" in str(excinfo.value)

    def test_group_is_refused(self):
        script = _make_script([_GroupTrack("Bus")])
        with pytest.raises(ValueError) as excinfo:
            script._set_track_monitoring({"track_index": 0, "state": "in"})
        assert "group track" in str(excinfo.value)

    def test_unarmable_track_is_refused(self):
        track = _Track("Odd")
        track.can_be_armed = False
        script = _make_script([track])
        with pytest.raises(ValueError) as excinfo:
            script._set_track_monitoring({"track_index": 0, "state": "in"})
        assert "no monitoring state" in str(excinfo.value)

    def test_frozen_is_refused(self):
        track = _Track("Frozen")
        track.is_frozen = True
        script = _make_script([track])
        with pytest.raises(ValueError) as excinfo:
            script._set_track_monitoring({"track_index": 0, "state": "in"})
        assert "frozen" in str(excinfo.value)

    def test_old_live_without_monitoring(self):
        script = _make_script()
        old = types.SimpleNamespace(name="Old", is_foldable=False,
                                    can_be_armed=True)
        script._song.tracks = [old]
        with pytest.raises(ValueError) as excinfo:
            script._set_track_monitoring({"track_index": 0, "state": "in"})
        assert "not available in this Live version" in str(excinfo.value)


# ── set_tracks_state ────────────────────────────────────────────────


class TestSetTracksState:
    def _set(self):
        guide = _Track("Guide", arm=True)
        gtr = _Track("Gtr DI", input_type="No Input", input_channel=None)
        bass = _Track("Bass")
        ret = _ReturnTrack("Verb")
        return guide, gtr, bass, ret, _make_script([guide, gtr, bass], [ret])

    def test_applies_every_change(self):
        guide, gtr, bass, ret, script = self._set()
        result = script._set_tracks_state({"changes": [
            {"track_name": "Guide", "input_type": "No Input", "arm": False},
            {"track_index": 1, "expect_name": "gtr di",
             "input_type": "Ext. In", "input_channel": "2",
             "monitoring": "auto", "arm": True},
            {"track_index": 2, "mute": True, "solo": False},
            {"return_track_index": 0, "mute": True},
        ]})
        assert guide.arm is False
        assert guide.input_routing_type.display_name == "No Input"
        assert gtr.arm is True
        assert gtr.input_routing_channel.display_name == "2"
        assert bass.mute is True and ret.mute is True
        assert result["dry_run"] is False
        first = result["changes"][0]
        assert first["track"] == {"index": 0, "name": "Guide"}
        assert first["before"]["arm"] is True
        assert first["after"]["arm"] is False
        assert first["after"]["input"]["type"] == "No Input"
        assert result["changes"][3]["track"]["return_track"] == "A"
        assert result["changes"][3]["after"]["arm"] is None
        assert result["warnings"] == []

    def test_channel_only_uses_the_current_type(self):
        guide, gtr, bass, ret, script = self._set()
        script._set_tracks_state({"changes": [
            {"track_index": 2, "input_channel": "1/2"}]})
        assert bass.input_routing_channel.display_name == "1/2"

    def test_dry_run_changes_nothing(self):
        guide, gtr, bass, ret, script = self._set()
        result = script._set_tracks_state({"dry_run": True, "changes": [
            {"track_index": 0, "arm": False},
            {"track_index": 2, "mute": True},
        ]})
        assert guide.arm is True and bass.mute is False
        assert result["dry_run"] is True
        assert result["changes"][0]["requested"] == {"arm": False}
        assert result["changes"][0]["before"]["arm"] is True
        assert "after" not in result["changes"][0]

    def test_every_problem_is_listed_and_nothing_changes(self):
        guide, gtr, bass, ret, script = self._set()
        with pytest.raises(ValueError) as excinfo:
            script._set_tracks_state({"changes": [
                {"track_index": 2, "mute": True},
                {"track_name": "Nope", "mute": True},
                {"track_index": 0, "expect_name": "Gtr", "arm": False},
                {"return_track_index": 0, "arm": True},
                "not an object",
                {"track_index": 1, "volume": 0.5},
                {"track_index": 1},
                {"track_index": 2, "solo": True},
                {"track_index": 1, "mute": "yes", "monitoring": "loud",
                 "input_type": "USB"},
            ]})
        message = str(excinfo.value)
        assert message.startswith("Rejected the whole batch; nothing was "
                                  "changed.")
        assert "change 2: No track named 'Nope'" in message
        assert "change 3: expect_name mismatch" in message
        assert "change 4: Track 'Verb' cannot be armed" in message
        assert "change 5: each change must be an object" in message
        assert "change 6: unknown key(s): volume" in message
        assert "change 7: nothing to change" in message
        assert "change 8: 'Bass' is already changed by change 1" in message
        assert "change 9: mute must be true or false" in message
        assert "monitoring must be one of in, auto, off" in message
        assert "No input type named 'USB'" in message
        assert "change 1" not in message.replace("by change 1", "")
        assert bass.mute is False and guide.arm is True

    def test_validation_rules_per_field(self):
        frozen = _Track("Frozen")
        frozen.is_frozen = True
        group = _GroupTrack("Bus")
        script = _make_script([frozen, group, _Track("Gtr", input_channel="1")])
        with pytest.raises(ValueError) as excinfo:
            script._set_tracks_state({"changes": [
                {"track_index": 0, "arm": True, "monitoring": "in"},
                {"track_index": 1, "arm": False, "monitoring": "off",
                 "input_type": "Ext. In"},
                {"track_index": 2, "input_channel": "9"},
            ]})
        message = str(excinfo.value)
        assert "change 1: Track 'Frozen' is frozen; unfreeze it before arming" in message
        assert "unfreeze it before changing monitoring" in message
        assert "change 2: Track 'Bus' is a group track and has no Arm state" in message
        assert "has no input routing" in message
        assert "has no monitoring state" in message
        assert "change 3: No input channel named '9'" in message

    @pytest.mark.parametrize("changes", [None, [], "x"])
    def test_changes_must_be_a_non_empty_list(self, changes):
        script = _make_script([_Track("Gtr")])
        with pytest.raises(ValueError) as excinfo:
            script._set_tracks_state({"changes": changes})
        assert "non-empty list" in str(excinfo.value)

    def test_live_rejection_rolls_back_every_track(self):
        guide, gtr, bass, ret, script = self._set()
        with pytest.raises(RuntimeError) as excinfo:
            script._set_tracks_state({"changes": [
                {"track_index": 0, "arm": False, "mute": True},
                {"track_index": 2, "solo": True},
                # The channel can only be checked after the type change.
                {"track_index": 1, "input_type": "Resampling",
                 "input_channel": "9", "arm": True},
            ]})
        message = str(excinfo.value)
        assert "Live rejected the change to 'Gtr DI'" in message
        assert "Every track in the batch was restored" in message
        assert guide.arm is True and guide.mute is False
        assert bass.solo is False
        assert gtr.arm is False
        assert gtr.input_routing_type.display_name == "No Input"

    def test_rollback_reports_tracks_it_could_not_restore(self):
        class _Stubborn(_Track):
            def __setattr__(self, name, value):
                if name == "mute" and getattr(self, "_locked", False):
                    raise RuntimeError("locked")
                object.__setattr__(self, name, value)

        stubborn = _Stubborn("Stubborn")
        bad = _Track("Bad")
        script = _make_script([stubborn, bad])
        original_apply = script._apply_track_change

        def apply(plan):
            original_apply(plan)
            if plan["track"] is stubborn:
                stubborn._locked = True
            else:
                raise RuntimeError("Live said no")

        script._apply_track_change = apply
        with pytest.raises(RuntimeError) as excinfo:
            script._set_tracks_state({"changes": [
                {"track_index": 0, "mute": True},
                {"track_index": 1, "solo": True},
            ]})
        message = str(excinfo.value)
        assert "Live said no" in message
        assert "except: Stubborn.mute: locked" in message
        assert bad.solo is False

    def test_arm_and_input_changes_return_warnings(self):
        gtr = _Track("Gtr", input_type="Ext. In", monitoring=IN)
        script = _make_script([gtr, _Track("Other", arm=True)])
        result = script._set_tracks_state({"changes": [
            {"track_index": 0, "input_type": "No Input", "arm": True}]})
        joined = " | ".join(result["warnings"])
        assert "records nothing" in joined
        assert "monitoring 'In'" in joined
        assert "Other tracks" not in joined

    def test_round_trip_through_the_dispatcher(self):
        guide, gtr, bass, ret, script = self._set()
        response = _dispatch(script, "set_tracks_state", {"changes": [
            {"track_index": 2, "mute": True}]})
        assert response["status"] == "success", response
        assert bass.mute is True

    def test_rejection_through_the_dispatcher_is_an_error(self):
        guide, gtr, bass, ret, script = self._set()
        response = _dispatch(script, "set_tracks_state", {"changes": [
            {"track_index": 9, "mute": True}]})
        assert response["status"] == "error"
        assert "track_index 10 is out of range" in response["message"]


# ── get_recording_status ────────────────────────────────────────────


class TestRecordingStatus:
    def test_reports_transport_armed_tracks_and_warnings(self):
        a = _Track("Gtr L", arm=True, input_channel="1")
        b = _Track("Gtr R", arm=True, input_channel="1")
        guide = _Track("Guide", arm=True, input_type="No Input",
                       input_channel=None)
        auto = _Track("New MIDI", "midi")
        auto.implicit_arm = True
        listen = _Track("Listen", monitoring=IN)
        idle = _Track("Idle")
        script = _make_script([a, b, guide, auto, listen, idle,
                               _GroupTrack("Bus")], [_ReturnTrack()])
        script._song.record_mode = True

        result = script._get_recording_status()

        transport = result["transport"]
        assert transport["record_mode"] is True
        assert transport["metronome"] is True
        assert transport["exclusive_arm"] is True
        assert [t["name"] for t in result["armed_tracks"]] == [
            "Gtr L", "Gtr R", "Guide", "New MIDI"]
        assert result["armed_tracks"][0] == {
            "index": 0, "name": "Gtr L", "arm": True, "implicit_arm": False,
            "input": {"type": "Ext. In", "channel": "1"},
            "monitoring": "auto"}
        joined = " | ".join(result["warnings"])
        assert "'Guide' is armed with input 'No Input'" in joined
        assert "2 armed tracks share input 'Ext. In' / '1': Gtr L, Gtr R" in joined
        assert "'New MIDI' has implicit_arm set" in joined
        assert "'Listen' has monitoring 'In' while not armed" in joined
        assert "Arrangement record is on while the transport is stopped" in joined

    def test_quiet_set_has_no_warnings(self):
        script = _make_script([_Track("Gtr", arm=True), _Track("Bass")])
        script._song.record_mode = True
        script._song.is_playing = True
        result = script._get_recording_status()
        assert result["warnings"] == []
        assert len(result["armed_tracks"]) == 1

    def test_dispatcher_and_error_path(self):
        script = _make_script([_Track("Gtr")])
        assert _dispatch(script, "get_recording_status")["status"] == "success"
        script._song.tracks = None  # not iterable
        response = _dispatch(script, "get_recording_status")
        assert response["status"] == "error"


# ── list_tracks and get_track_info ──────────────────────────────────


class TestListTracks:
    def test_rows_for_every_kind(self):
        group = _GroupTrack("Gtrs", fold_state=1)
        group.devices = [object()]
        child = _Track("Gtr DI", arm=True)
        child.is_grouped = True
        child.group_track = group
        midi = _Track("Keys", "midi")
        midi.arrangement_clips = [object(), object()]
        ret = _ReturnTrack("Verb")
        script = _make_script([group, child, midi], [ret])

        rows = script._list_tracks()["tracks"]

        assert [r["name"] for r in rows] == ["Gtrs", "Gtr DI", "Keys",
                                            "Verb", "Master"]
        g, c, m, r, master = rows
        assert g["kind"] == "group" and g["folded"] is True
        assert g["arm"] is None and g["can_be_armed"] is False
        assert g["input"] is None and g["device_count"] == 1
        assert g["arrangement_clip_count"] is None
        assert c["group"] == "Gtrs" and c["arm"] is True
        assert c["input"] == {"type": "Ext. In", "channel": "1"}
        assert c["monitoring"] == "auto" and c["folded"] is None
        assert m["kind"] == "midi" and m["arrangement_clip_count"] == 2
        assert m["group"] is None and m["index"] == 2
        assert r["kind"] == "return" and r["return_track"] == "A"
        assert r["index"] is None
        assert master["kind"] == "master" and master["mute"] is None
        assert master["device_count"] == 2
        assert master["output"] == {"type": "1/2", "channel": None}

    def test_returns_and_master_can_be_left_out(self):
        script = _make_script([_Track("Gtr")], [_ReturnTrack()])
        rows = script._list_tracks(False, False)["tracks"]
        assert [r["name"] for r in rows] == ["Gtr"]

    def test_dispatcher_passes_flags(self):
        script = _make_script([_Track("Gtr")], [_ReturnTrack()])
        response = _dispatch(script, "list_tracks",
                             {"include_returns": False,
                              "include_master": False})
        assert len(response["result"]["tracks"]) == 1

    def test_error_path(self):
        script = _make_script()
        script._song.tracks = None
        with pytest.raises(TypeError):
            script._list_tracks()


class TestGetTrackInfoExtended:
    def test_adds_routing_group_and_fold_state(self):
        group = _GroupTrack("Gtrs")
        child = _Track("Gtr DI", monitoring=IN)
        child.is_grouped = True
        child.group_track = group
        child.take_lanes = [object(), object(), object()]
        child.arrangement_clips = [object()]
        script = _make_script([group, child])

        info = script._get_track_info(1)

        assert info["input"] == {"type": "Ext. In", "channel": "1"}
        assert info["output"] == {"type": "Master", "channel": "Track In"}
        assert info["monitoring"] == "in"
        assert info["can_be_armed"] is True
        assert info["is_foldable"] is False and info["fold_state"] is None
        assert info["is_grouped"] is True
        assert info["group_track"] == {"index": 0, "name": "Gtrs"}
        assert info["is_visible"] is True and info["is_frozen"] is False
        assert info["color"] == 123
        assert info["arrangement_clip_count"] == 1
        assert info["take_lane_count"] == 3
        assert info["clip_slots"] == []

    def test_group_track_info(self):
        script = _make_script([_GroupTrack("Gtrs", fold_state=0)])
        info = script._get_track_info(0)
        assert info["is_foldable"] is True and info["fold_state"] is False
        assert info["group_track"] is None and info["take_lane_count"] is None
        assert info["can_be_armed"] is False

    def test_clip_slots_can_be_left_out(self):
        track = _Track("Gtr")
        slot = MagicMock()
        slot.has_clip = False
        track.clip_slots = [slot]
        script = _make_script([track])
        response = _dispatch(script, "get_track_info",
                             {"track_index": 0, "include_clip_slots": False})
        assert "clip_slots" not in response["result"]
        assert len(script._get_track_info(0)["clip_slots"]) == 1


# ── dispatcher reachability for the new commands ────────────────────


class TestNewCommandsAreReachable:
    def test_set_track_input(self):
        track = _Track("Gtr", input_type="No Input", input_channel=None)
        script = _make_script([track])
        response = _dispatch(script, "set_track_input",
                             {"track_index": 0, "input_type": "Ext. In",
                              "input_channel": "2"})
        assert response["status"] == "success", response
        assert track.input_routing_channel.display_name == "2"

    def test_set_track_monitoring(self):
        track = _Track("Gtr")
        script = _make_script([track])
        response = _dispatch(script, "set_track_monitoring",
                             {"track_index": 0, "state": "off"})
        assert response["status"] == "success", response
        assert track.current_monitoring_state == OFF

    def test_get_track_routing_options(self):
        script = _make_script([_Track("Gtr")])
        response = _dispatch(script, "get_track_routing_options",
                             {"track_name": "gtr"})
        assert response["status"] == "success", response
        assert response["result"]["input"]["current"]["type"] == "Ext. In"

    @pytest.mark.parametrize("command,params", [
        ("set_track_mute", {"track_name": "nope"}),
        ("set_track_input", {"track_name": "nope", "input_type": "x"}),
        ("set_track_monitoring", {"track_name": "nope", "state": "in"}),
        ("get_track_routing_options", {"track_name": "nope"}),
    ])
    def test_errors_come_back_as_errors(self, command, params):
        script = _make_script([_Track("Gtr")])
        response = _dispatch(script, command, params)
        assert response["status"] == "error"
        assert "No track named 'nope'" in response["message"]
