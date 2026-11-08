"""Unit tests for the Remote Script's session, track, clip, arrangement and
cue-point handlers, run against stub Live Song/Track/Clip objects."""
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

# ── stubs ────────────────────────────────────────────────────────────


class Param:
    def __init__(self, name, value=0.5, min=0.0, max=1.0):
        self.name = name
        self.value = value
        self.min = min
        self.max = max


class Mixer:
    def __init__(self, volume=0.85, panning=0.0, sends=()):
        self.volume = Param("Track Volume", volume, 0.0, 1.0)
        self.panning = Param("Track Panning", panning, -1.0, 1.0)
        self.sends = list(sends)


class Clip:
    def __init__(self, name="", start_time=0.0, end_time=4.0, is_midi=True, length=4.0):
        self.name = name
        self.start_time = start_time
        self.end_time = end_time
        self.length = length
        self.is_midi_clip = is_midi
        self.is_audio_clip = not is_midi
        self.muted = False
        self.color = 0
        self.looping = True
        self.loop_start = 0.0
        self.loop_end = end_time - start_time
        self.is_playing = False
        self.is_recording = False
        self.set_notes = MagicMock()
        self.clear_all_envelopes = MagicMock()
        self.create_automation_envelope = MagicMock()
        self.clear_envelope = MagicMock()


class Slot:
    def __init__(self, clip=None):
        self.clip = clip
        self.fire = MagicMock()
        self.stop = MagicMock()

    @property
    def has_clip(self):
        return self.clip is not None

    def create_clip(self, length):
        self.clip = Clip(name="New Clip", length=length)


class Device:
    def __init__(self, name="EQ Eight", class_name="Eq8", class_display_name="EQ Eight",
                 parameters=()):
        self.name = name
        self.class_name = class_name
        self.class_display_name = class_display_name
        self.can_have_chains = False
        self.can_have_drum_pads = False
        self.parameters = list(parameters)


class Track:
    def __init__(self, name="Track", slots=0, clips=None, arrangement_clips=None,
                 devices=None, midi=True):
        self.name = name
        self.is_foldable = False
        self.has_midi_input = midi
        self.has_audio_input = not midi
        self.mute = False
        self.solo = False
        self.arm = False
        self.mixer_device = Mixer()
        self.clip_slots = [Slot() for _ in range(slots)] if clips is None else \
            [Slot(c) for c in clips]
        self.arrangement_clips = list(arrangement_clips or [])
        self.devices = list(devices or [])
        self.view = types.SimpleNamespace(is_collapsed=False)
        self.create_midi_clip = MagicMock(side_effect=self._create_midi_clip)
        self.create_audio_clip = MagicMock(side_effect=self._create_audio_clip)
        self.duplicate_clip_to_arrangement = MagicMock(side_effect=self._duplicate)
        self.delete_clip = MagicMock()

    def _create_midi_clip(self, position, length):
        self.arrangement_clips.append(Clip("MIDI", position, position + length))

    def _create_audio_clip(self, file_path, position):
        self.arrangement_clips.append(Clip("Audio", position, position + 8.0, is_midi=False))

    def _duplicate(self, clip, destination_time):
        self.arrangement_clips.append(
            Clip(clip.name, destination_time, destination_time + clip.length))


class Cue:
    def __init__(self, name, time):
        self.name = name
        self.time = time
        self.jump = MagicMock()


class Song:
    def __init__(self, tracks=(), return_tracks=(), cue_points=()):
        self.tracks = list(tracks)
        self.return_tracks = list(return_tracks)
        self.master_track = Track("Master")
        self.cue_points = list(cue_points)
        self.tempo = 120.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.is_playing = False
        self.current_song_time = 0.0
        self.song_length = 64.0
        self.loop = False
        self.loop_start = 0.0
        self.loop_length = 16.0
        self.arrangement_overdub = False
        self.back_to_arranger = False
        self.view = types.SimpleNamespace(selected_track=None, follow_song=False)
        self.set_or_delete_cue = MagicMock()
        self.jump_to_next_cue = MagicMock(side_effect=lambda: setattr(self, "current_song_time", 32.0))
        self.jump_to_prev_cue = MagicMock(side_effect=lambda: setattr(self, "current_song_time", 8.0))

    def create_midi_track(self, index):
        track = Track(f"{len(self.tracks) + 1}-MIDI")
        if index == -1:
            self.tracks.append(track)
        else:
            self.tracks.insert(index, track)

    def start_playing(self):
        self.is_playing = True

    def stop_playing(self):
        self.is_playing = False


class Exploding:
    """Any attribute access raises, to drive handler error paths."""

    def __getattr__(self, name):
        raise RuntimeError("Live API exploded on " + name)

    def __setattr__(self, name, value):
        raise RuntimeError("Live API exploded on " + name)


def make_script(song=None):
    script = AbletonMCP.__new__(AbletonMCP)
    script._song = song if song is not None else Song()
    script.log_message = MagicMock()
    script.application = MagicMock()
    return script


# ── serialization helpers ────────────────────────────────────────────


class TestSerializers:
    def test_arrangement_clip_info(self):
        clip = Clip("Intro", 4.0, 12.0)
        info = make_script()._get_arrangement_clip_info(clip)
        assert info == {"name": "Intro", "start_time": 4.0, "end_time": 12.0, "length": 8.0,
                        "is_midi": True, "is_audio": False, "muted": False, "color": 0,
                        "looping": True, "loop_start": 0.0, "loop_end": 8.0}

    def test_arrangement_clip_info_error(self):
        info = make_script()._get_arrangement_clip_info(Exploding())
        assert info["name"] == "unknown"
        assert "exploded" in info["error"]

    def test_transport_info(self):
        song = Song()
        song.loop = True
        info = make_script(song)._get_transport_info()
        assert info["tempo"] == 120.0
        assert info["loop_enabled"] is True
        assert info["song_length"] == 64.0

    @pytest.mark.parametrize("method,args", [
        ("_get_transport_info", ()),
        ("_get_session_info", ()),
        ("_get_cue_points", ()),
        ("_set_song_time", (4.0,)),
        ("_set_arrangement_loop", (True,)),
        ("_set_tempo", (100.0,)),
        ("_start_playback", ()),
        ("_stop_playback", ()),
        ("_jump_to_cue", ("next",)),
        ("_create_cue_point", (4.0,)),
        ("_delete_cue_point", (4.0,)),
        ("_create_midi_track", (-1,)),
    ])
    def test_song_errors_are_logged_and_reraised(self, method, args):
        script = make_script(Exploding())
        with pytest.raises(RuntimeError, match="exploded"):
            getattr(script, method)(*args)
        script.log_message.assert_called()


# ── session and track handlers ───────────────────────────────────────


class TestSessionAndTracks:
    def test_session_info(self):
        song = Song(tracks=[Track("A"), Track("B")], return_tracks=[Track("Rev")])
        info = make_script(song)._get_session_info()
        assert info == {"tempo": 120.0, "signature_numerator": 4, "signature_denominator": 4,
                        "track_count": 2, "return_track_count": 1,
                        "master_track": {"name": "Master", "volume": 0.85, "panning": 0.0}}

    def test_track_info_lists_clips_and_devices(self):
        clip = Clip("Loop", length=8.0)
        track = Track("Keys", clips=[clip, None], devices=[Device()])
        info = make_script(Song([track]))._get_track_info(0)
        assert info["clip_slots"] == [
            {"index": 0, "has_clip": True,
             "clip": {"name": "Loop", "length": 8.0, "is_playing": False, "is_recording": False}},
            {"index": 1, "has_clip": False, "clip": None},
        ]
        assert info["devices"] == [{"index": 0, "name": "EQ Eight", "class_name": "Eq8",
                                    "type": "unknown"}]

    @pytest.mark.parametrize("index", [-1, 1])
    def test_track_info_out_of_range(self, index):
        script = make_script(Song([Track()]))
        with pytest.raises(IndexError):
            script._get_track_info(index)
        script.log_message.assert_called()

    @pytest.mark.parametrize("index,expected_index", [(-1, 2), (0, 0)])
    def test_create_midi_track(self, index, expected_index):
        song = Song([Track("A"), Track("B")])
        result = make_script(song)._create_midi_track(index)
        assert result == {"index": expected_index, "name": "3-MIDI"}
        assert len(song.tracks) == 3

    def test_set_track_name(self):
        song = Song([Track("A")])
        assert make_script(song)._set_track_name(0, "Drums") == {"name": "Drums"}
        assert song.tracks[0].name == "Drums"

    def test_set_track_name_out_of_range(self):
        with pytest.raises(IndexError):
            make_script(Song())._set_track_name(0, "x")


class TestMixer:
    def _song(self):
        return Song([Track("Drums")], return_tracks=[Track("Reverb")])

    def test_get_volume_includes_return_tracks(self):
        result = make_script(self._song())._get_track_volume(1)
        assert result == {"track_name": "Reverb", "volume": 0.85, "volume_min": 0.0,
                          "volume_max": 1.0, "panning": 0.0, "panning_min": -1.0,
                          "panning_max": 1.0}

    @pytest.mark.parametrize("requested,stored", [(0.5, 0.5), (1.7, 1.0), (-2.0, 0.0)])
    def test_set_volume_clamps(self, requested, stored):
        song = self._song()
        result = make_script(song)._set_track_volume(0, requested)
        assert result == {"track_name": "Drums", "volume": stored}
        assert song.tracks[0].mixer_device.volume.value == stored

    @pytest.mark.parametrize("requested,stored", [(-0.25, -0.25), (3.0, 1.0), (-3.0, -1.0)])
    def test_set_panning_clamps(self, requested, stored):
        song = self._song()
        result = make_script(song)._set_track_panning(1, requested)
        assert result == {"track_name": "Reverb", "panning": stored}

    @pytest.mark.parametrize("method,args", [
        ("_get_track_volume", (2,)),
        ("_set_track_volume", (-1, 0.5)),
        ("_set_track_panning", (5, 0.0)),
    ])
    def test_out_of_range(self, method, args):
        script = make_script(self._song())
        with pytest.raises(IndexError, match=r"out of range \(0-1\)"):
            getattr(script, method)(*args)


# ── session clip handlers ────────────────────────────────────────────


class TestSessionClips:
    def test_create_clip(self):
        song = Song([Track(slots=2)])
        result = make_script(song)._create_clip(0, 1, 8.0)
        assert result == {"name": "New Clip", "length": 8.0}

    @pytest.mark.parametrize("method,args", [
        ("_create_clip", (0, 0, 4.0)),
        ("_add_notes_to_clip", (0, 1, [])),
        ("_set_clip_name", (0, 1, "x")),
        ("_fire_clip", (0, 1)),
    ])
    def test_slot_state_errors(self, method, args):
        song = Song([Track(clips=[Clip("Full"), None])])
        with pytest.raises(Exception, match="already has a clip|No clip in slot"):
            getattr(make_script(song), method)(*args)

    @pytest.mark.parametrize("method,args", [
        ("_create_clip", (1, 0, 4.0)),
        ("_create_clip", (0, 5, 4.0)),
        ("_add_notes_to_clip", (1, 0, [])),
        ("_add_notes_to_clip", (0, -1, [])),
        ("_set_clip_name", (1, 0, "x")),
        ("_set_clip_name", (0, 9, "x")),
        ("_fire_clip", (-1, 0)),
        ("_fire_clip", (0, 9)),
        ("_stop_clip", (3, 0)),
        ("_stop_clip", (0, 9)),
    ])
    def test_index_errors(self, method, args):
        song = Song([Track(slots=1)])
        with pytest.raises(IndexError):
            getattr(make_script(song), method)(*args)

    def test_add_notes_converts_to_live_tuples(self):
        clip = Clip()
        song = Song([Track(clips=[clip])])
        notes = [{"pitch": 64, "start_time": 1.0, "duration": 0.5, "velocity": 90, "mute": True},
                 {}]
        assert make_script(song)._add_notes_to_clip(0, 0, notes) == {"note_count": 2}
        clip.set_notes.assert_called_once_with(
            ((64, 1.0, 0.5, 90, True), (60, 0.0, 0.25, 100, False)))

    def test_set_clip_name(self):
        clip = Clip("Old")
        assert make_script(Song([Track(clips=[clip])]))._set_clip_name(0, 0, "New") == {"name": "New"}
        assert clip.name == "New"

    def test_fire_and_stop(self):
        clip = Clip()
        track = Track(clips=[clip])
        script = make_script(Song([track]))
        assert script._fire_clip(0, 0) == {"fired": True}
        assert script._stop_clip(0, 0) == {"stopped": True}
        track.clip_slots[0].fire.assert_called_once()
        track.clip_slots[0].stop.assert_called_once()

    def test_tempo_and_transport(self):
        song = Song()
        script = make_script(song)
        assert script._set_tempo(98.5) == {"tempo": 98.5}
        assert script._start_playback() == {"playing": True}
        assert script._stop_playback() == {"playing": False}


# ── arrangement handlers ─────────────────────────────────────────────


class TestResolveArrangementClip:
    def _script(self):
        clips = [Clip("Intro", 0, 4), Clip("Verse", 4, 8), Clip("Verse", 8, 12)]
        return make_script(Song([Track("Keys", arrangement_clips=clips)]))

    def test_by_name(self):
        track, clip = self._script()._resolve_arrangement_clip(0, clip_name="Intro")
        assert (track.name, clip.start_time) == ("Keys", 0)

    def test_by_index(self):
        _, clip = self._script()._resolve_arrangement_clip(0, clip_index=2)
        assert clip.start_time == 8

    @pytest.mark.parametrize("kwargs,error,match", [
        ({"track_index": 3, "clip_index": 0}, IndexError, r"Track index 3 out of range \(0-0\)"),
        ({"track_index": 0, "clip_name": "Chorus"}, ValueError, "No arrangement clip named 'Chorus'"),
        ({"track_index": 0, "clip_name": "Verse"}, ValueError, "Ambiguous: 2 clips named 'Verse'"),
        ({"track_index": 0}, ValueError, "Either clip_index or clip_name"),
        ({"track_index": 0, "clip_index": 3}, IndexError, r"Clip index 3 out of range \(0-2\)"),
    ])
    def test_errors(self, kwargs, error, match):
        with pytest.raises(error, match=match):
            self._script()._resolve_arrangement_clip(**kwargs)

    def test_check_overlap(self):
        track = Track(arrangement_clips=[Clip("A", 0, 4), Clip("", 4, 8), Clip("C", 8, 12)])
        assert make_script()._check_overlap(track, 2.0, 4.0) == ["A", "(unnamed)"]


class TestArrangementClips:
    def test_arrangement_info_out_of_range(self):
        with pytest.raises(IndexError):
            make_script(Song([Track()]))._get_arrangement_info(4)

    def test_validate_rejects_return_and_master(self):
        ret = Track("Reverb")
        song = Song([ret], return_tracks=[ret])
        with pytest.raises(ValueError, match="return track 'Reverb'"):
            make_script(song)._validate_not_return_or_master(0)
        song = Song([Track()])
        song.master_track = song.tracks[0]
        with pytest.raises(ValueError, match="master track"):
            make_script(song)._validate_not_return_or_master(0)

    def test_create_midi_clip_reports_overlap(self):
        track = Track(arrangement_clips=[Clip("Old", 0, 8)])
        result = make_script(Song([track]))._create_arrangement_clip(0, 4.0, 8.0)
        track.create_midi_clip.assert_called_once_with(4.0, 8.0)
        assert result["name"] == "MIDI"
        assert result["start_time"] == 4.0
        assert result["overlapped_clips"] == ["Old"]

    def test_create_midi_clip_falls_back_when_clip_not_found(self):
        track = Track()
        track.create_midi_clip = MagicMock()
        result = make_script(Song([track]))._create_arrangement_clip(0, 4.0, 8.0)
        assert result == {"start_time": 4.0, "length": 8.0, "is_midi": True,
                          "overlapped_clips": []}

    def test_create_audio_clip(self):
        track = Track(midi=False)
        result = make_script(Song([track]))._create_arrangement_audio_clip(0, 16.0, "/a.wav")
        track.create_audio_clip.assert_called_once_with("/a.wav", 16.0)
        assert result["is_audio"] is True and result["start_time"] == 16.0

    def test_create_audio_clip_falls_back_when_clip_not_found(self):
        track = Track(midi=False)
        track.create_audio_clip = MagicMock()
        result = make_script(Song([track]))._create_arrangement_audio_clip(0, 16.0, "/a.wav")
        assert result == {"start_time": 16.0, "file_path": "/a.wav", "is_audio": True}

    @pytest.mark.parametrize("method,args", [
        ("_create_arrangement_clip", (2, 0.0, 4.0)),
        ("_create_arrangement_audio_clip", (-1, 0.0, "/a.wav")),
        ("_duplicate_to_arrangement", (2, 0, 0.0)),
        ("_duplicate_to_arrangement", (0, 5, 0.0)),
    ])
    def test_index_errors(self, method, args):
        script = make_script(Song([Track(slots=1)]))
        with pytest.raises(IndexError):
            getattr(script, method)(*args)
        script.log_message.assert_called()

    def test_duplicate_to_arrangement(self):
        clip = Clip("Hook", length=4.0)
        track = Track(clips=[clip])
        result = make_script(Song([track]))._duplicate_to_arrangement(0, 0, 32.0)
        track.duplicate_clip_to_arrangement.assert_called_once_with(clip, 32.0)
        assert result["name"] == "Hook" and result["duplicated"] is True

    def test_duplicate_falls_back_when_clip_not_found(self):
        track = Track(clips=[Clip("Hook")])
        track.duplicate_clip_to_arrangement = MagicMock()
        result = make_script(Song([track]))._duplicate_to_arrangement(0, 0, 32.0)
        assert result == {"start_time": 32.0, "duplicated": True}

    def test_duplicate_empty_slot(self):
        with pytest.raises(ValueError, match="No clip in slot 0"):
            make_script(Song([Track(slots=1)]))._duplicate_to_arrangement(0, 0, 0.0)

    def test_add_notes_to_arrangement_clip(self):
        clip = Clip("Bass", 0, 8)
        script = make_script(Song([Track(arrangement_clips=[clip])]))
        result = script._add_notes_to_arrangement_clip(0, 0, [{"pitch": 36}])
        assert result == {"note_count": 1}
        clip.set_notes.assert_called_once_with(((36, 0.0, 0.25, 100, False),))

    def test_add_notes_rejects_audio_clip(self):
        script = make_script(Song([Track(arrangement_clips=[Clip(is_midi=False)])]))
        with pytest.raises(ValueError, match="not a MIDI clip"):
            script._add_notes_to_arrangement_clip(0, 0, [])

    def test_delete_arrangement_clip(self):
        clip = Clip("Intro")
        track = Track(arrangement_clips=[clip])
        assert make_script(Song([track]))._delete_arrangement_clip(0, clip_name="Intro") == \
            {"deleted": True}
        track.delete_clip.assert_called_once_with(clip)

    def test_delete_arrangement_clip_error(self):
        with pytest.raises(IndexError):
            make_script(Song([Track()]))._delete_arrangement_clip(0, clip_index=0)

    def test_set_arrangement_clip_property(self):
        clip = Clip()
        script = make_script(Song([Track(arrangement_clips=[clip])]))
        assert script._set_arrangement_clip_property(0, 0, "muted", True) == \
            {"property": "muted", "value": True}
        assert clip.muted is True

    def test_set_arrangement_clip_property_rejects_unknown(self):
        script = make_script(Song([Track(arrangement_clips=[Clip()])]))
        with pytest.raises(ValueError, match="Property 'start_time' not allowed"):
            script._set_arrangement_clip_property(0, 0, "start_time", 1.0)


class TestViews:
    def test_set_view(self):
        script = make_script()
        app = script.application.return_value
        app.view.is_view_visible.return_value = True
        assert script._set_view("Session") == {"visible": True}
        app.view.show_view.assert_called_once_with("Session")

    def test_set_view_error(self):
        script = make_script()
        script.application.side_effect = RuntimeError("no app")
        with pytest.raises(RuntimeError):
            script._set_view("Session")

    @pytest.mark.parametrize("action,method,args", [
        ("zoom_in", "zoom_view", (1, "Arranger", False)),
        ("zoom_out", "zoom_view", (0, "Arranger", False)),
        ("scroll_right", "scroll_view", (1, "Arranger", False)),
        ("scroll_left", "scroll_view", (0, "Arranger", False)),
    ])
    def test_zoom_and_scroll(self, action, method, args):
        script = make_script()
        assert script._control_arrangement_view(action) == {"action": action, "done": True}
        getattr(script.application.return_value.view, method).assert_called_once_with(*args)

    @pytest.mark.parametrize("action,expected", [("follow_on", True), ("follow_off", False)])
    def test_follow(self, action, expected):
        song = Song()
        song.view.follow_song = not expected
        make_script(song)._control_arrangement_view(action)
        assert song.view.follow_song is expected

    @pytest.mark.parametrize("action,collapsed", [("collapse_track", True), ("expand_track", False)])
    def test_collapse_expand(self, action, collapsed):
        song = Song([Track(), Track()])
        song.tracks[1].view.is_collapsed = not collapsed
        make_script(song)._control_arrangement_view(action, 1)
        assert song.tracks[1].view.is_collapsed is collapsed

    @pytest.mark.parametrize("action,track_index,error", [
        ("collapse_track", 3, IndexError),
        ("expand_track", -1, IndexError),
        ("wiggle", 0, ValueError),
    ])
    def test_errors(self, action, track_index, error):
        with pytest.raises(error):
            make_script(Song([Track()]))._control_arrangement_view(action, track_index)


class TestClipAutomation:
    def _setup(self):
        clip = Clip("Lead")
        device = Device(parameters=[Param("Frequency"), Param("Gain")])
        track = Track("Synth", arrangement_clips=[clip], devices=[device])
        track.mixer_device = Mixer(sends=[Param("Send A")])
        return make_script(Song([track])), track, clip

    def test_clear_all(self):
        script, _, clip = self._setup()
        assert script._manage_clip_automation(0, 0, "clear_all") == {"action": "clear_all", "done": True}
        clip.clear_all_envelopes.assert_called_once()

    @pytest.mark.parametrize("name,expected", [
        ("track volume", "Track Volume"),
        ("Send A", "Send A"),
        ("gain", "Gain"),
    ])
    def test_create_finds_mixer_send_and_device_params(self, name, expected):
        script, _, clip = self._setup()
        result = script._manage_clip_automation(0, 0, "create", name)
        assert result == {"action": "create", "parameter": expected, "done": True}
        assert clip.create_automation_envelope.call_args[0][0].name == expected

    def test_clear_single_envelope(self):
        script, _, clip = self._setup()
        result = script._manage_clip_automation(0, 0, "clear", "Track Panning")
        assert result == {"action": "clear", "parameter": "Track Panning", "done": True}
        assert clip.clear_envelope.call_args[0][0].name == "Track Panning"

    def test_unknown_parameter(self):
        script, _, _ = self._setup()
        with pytest.raises(ValueError, match="Parameter 'Cutoff' not found on track 'Synth'"):
            script._manage_clip_automation(0, 0, "create", "Cutoff")

    def test_unknown_action(self):
        script, _, _ = self._setup()
        with pytest.raises(ValueError, match="Unknown action: wipe"):
            script._manage_clip_automation(0, 0, "wipe", "Gain")


# ── transport position and cue points ────────────────────────────────


class TestCuePoints:
    def test_get_cue_points(self):
        song = Song(cue_points=[Cue("Intro", 0.0), Cue("Drop", 32.0)])
        assert make_script(song)._get_cue_points() == {"cue_points": [
            {"name": "Intro", "time": 0.0}, {"name": "Drop", "time": 32.0}]}

    def test_set_song_time(self):
        song = Song()
        assert make_script(song)._set_song_time(12.0) == {"time": 12.0}

    @pytest.mark.parametrize("start,length,expected", [
        (None, None, {"enabled": True, "start": 0.0, "length": 16.0}),
        (8.0, 4.0, {"enabled": True, "start": 8.0, "length": 4.0}),
    ])
    def test_set_arrangement_loop(self, start, length, expected):
        song = Song()
        assert make_script(song)._set_arrangement_loop(True, start, length) == expected

    @pytest.mark.parametrize("direction,time", [("next", 32.0), ("prev", 8.0)])
    def test_jump_by_direction(self, direction, time):
        song = Song()
        assert make_script(song)._jump_to_cue(direction) == {"direction": direction, "time": time}

    def test_jump_by_name(self):
        drop = Cue("Drop", 32.0)
        song = Song(cue_points=[Cue("Intro", 0.0), drop])
        assert make_script(song)._jump_to_cue(name="Drop") == {"name": "Drop", "time": 32.0}
        drop.jump.assert_called_once()

    @pytest.mark.parametrize("kwargs,match", [
        ({"name": "Bridge"}, "Cue point 'Bridge' not found"),
        ({}, "Provide direction"),
    ])
    def test_jump_errors(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            make_script(Song(cue_points=[Cue("Intro", 0.0)]))._jump_to_cue(**kwargs)

    def test_create_rejects_existing_position(self):
        song = Song(cue_points=[Cue("Drop", 32.0)])
        with pytest.raises(ValueError, match="already exists at this position: Drop"):
            make_script(song)._create_cue_point(32.001, "Again")
        song.set_or_delete_cue.assert_not_called()

    def test_create_without_name(self):
        song = Song()
        assert make_script(song)._create_cue_point(16.0) == {"time": 16.0, "name": ""}
        assert song.current_song_time == 16.0
        song.set_or_delete_cue.assert_called_once()

    def test_delete(self):
        song = Song(cue_points=[Cue("Intro", 0.0), Cue("Drop", 32.0)])
        assert make_script(song)._delete_cue_point(32.0) == {"deleted": True}
        assert song.current_song_time == 32.0
        song.set_or_delete_cue.assert_called_once()

    def test_delete_missing(self):
        with pytest.raises(ValueError, match="No cue point at this position"):
            make_script(Song(cue_points=[Cue("Intro", 0.0)]))._delete_cue_point(8.0)
