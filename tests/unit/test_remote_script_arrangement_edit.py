"""Unit tests for the Remote Script's loop read-back, arrangement clip notes,
and arrangement clip trim and split handlers.

The stubs follow Live. Song loop settings read back their old value until
the main-thread tick ends. Clip loop and marker settings read back at once
but only change the clip (its right edge, what a duplicate copies) when the
tick ends. A duplicate landing on part of another clip cuts that part out,
as pasting does, and the clip it cut keeps playing the rest as before.
"""
import copy as copy_module
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

TEMPO = 120.0


# ── Song with late loop settings ─────────────────────────────────────


def _late_song_setting(name):
    def get(self):
        return self._applied[name]

    def set(self, value):
        self._pending[name] = value

    return property(get, set)


class LateSong:
    """A Song whose loop settings, as in Live, keep their old values until the tick ends."""

    loop = _late_song_setting("loop")
    loop_start = _late_song_setting("loop_start")
    loop_length = _late_song_setting("loop_length")

    def __init__(self, loop=False, loop_start=0.0, loop_length=16.0, max_length=None):
        self._applied = {"loop": loop, "loop_start": loop_start, "loop_length": loop_length}
        self._pending = {}
        self.max_length = max_length
        self.tracks = []

    def commit(self):
        self._applied.update(self._pending)
        self._pending = {}
        if self.max_length is not None:
            self._applied["loop_length"] = min(self._applied["loop_length"], self.max_length)


# ── Clips and tracks that edit the way Live does ─────────────────────

_PAIRS = {"loop_start": ("loop_start", "loop_end"), "loop_end": ("loop_start", "loop_end"),
          "start_marker": ("start_marker", "end_marker"),
          "end_marker": ("start_marker", "end_marker")}


def _late_clip_setting(name):
    def get(self):
        return self._pending.get(name, self._committed[name])

    def set(self, value):
        if name in self.refuse:
            return
        if name in _PAIRS:
            low_name, high_name = _PAIRS[name]
            low = value if name == low_name else getattr(self, low_name)
            high = value if name == high_name else getattr(self, high_name)
            if low > high:
                raise RuntimeError("start after end")
        self._pending[name] = value

    return property(get, set)


class LiveClip:
    """An arrangement clip.

    Unlooped, its right edge follows its loop start and end (clip start and
    end); looped, its length is its own and the loop repeats inside it.
    Unwarped audio counts clip time in seconds of the file.
    """

    loop_start = _late_clip_setting("loop_start")
    loop_end = _late_clip_setting("loop_end")
    start_marker = _late_clip_setting("start_marker")
    end_marker = _late_clip_setting("end_marker")
    looping = _late_clip_setting("looping")

    def __init__(self, name="Riff", start_time=0.0, length=16.0, midi=True, looping=False,
                 loop=None, start_marker=None, warping=True, notes=()):
        units = 1.0 if midi or warping else 60.0 / TEMPO
        loop_start, loop_end = loop if loop is not None else (0.0, length * units)
        self._committed = {
            "loop_start": loop_start, "loop_end": loop_end,
            "start_marker": loop_start if start_marker is None else start_marker,
            "end_marker": loop_end, "looping": looping}
        self._pending = {}
        self._extent = length
        self.refuse = set()
        self.name = name
        self.start_time = start_time
        self.is_midi_clip = midi
        self.is_audio_clip = not midi
        self.warping = warping if not midi else False
        self.file_path = None if midi else "/Samples/" + name + ".wav"
        self.gain = 0.7
        self.pitch_coarse = 0
        self.pitch_fine = 0.0
        self.warp_mode = 4
        self.muted = False
        self.color = 0x112233
        self.notes = [dict(note, note_id=i) for i, note in enumerate(notes)]
        self.remove_does_nothing = False

    @property
    def units(self):
        """Clip-time units per song beat."""
        return 60.0 / TEMPO if self.is_audio_clip and not self.warping else 1.0

    @property
    def end_time(self):
        committed = self._committed
        if committed["looping"]:
            return self.start_time + self._extent
        return self.start_time + (committed["loop_end"] - committed["loop_start"]) / self.units

    def commit(self):
        was_looping, old_end = self._committed["looping"], self.end_time
        self._committed.update(self._pending)
        self._pending = {}
        if self._committed["looping"] and not was_looping:
            self._extent = old_end - self.start_time

    def copy(self, start_time):
        """A duplicate, made from the committed state only."""
        clip = LiveClip.__new__(LiveClip)
        clip.__dict__.update(copy_module.deepcopy(
            {k: v for k, v in self.__dict__.items() if k != "_pending"}))
        clip._pending = {}
        clip.start_time = start_time
        return clip

    def get_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        return tuple(types.SimpleNamespace(**note) for note in self.notes
                     if from_pitch <= note["pitch"] < from_pitch + pitch_span
                     and from_time <= note["start_time"] < from_time + time_span)

    def remove_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        if self.remove_does_nothing:
            return
        self.notes = [note for note in self.notes
                      if not (from_pitch <= note["pitch"] < from_pitch + pitch_span
                              and from_time <= note["start_time"] < from_time + time_span)]

    # Live's own edits when something lands on part of the clip.

    def cut_right(self, at):
        """Keep only the part before song beat *at*."""
        length = at - self.start_time
        if self._committed["looping"]:
            self._extent = length
        else:
            self._committed["loop_end"] = self._committed["loop_start"] + length * self.units
            self._committed["end_marker"] = self._committed["start_marker"] + length * self.units

    def cut_left(self, at):
        """Keep only the part from song beat *at*, still playing at the same times."""
        offset = at - self.start_time
        committed = self._committed
        if committed["looping"]:
            loop_start, loop_end = committed["loop_start"], committed["loop_end"]
            time = committed["start_marker"] + offset
            if time >= loop_end:
                time = loop_start + (time - loop_end) % (loop_end - loop_start)
            committed["start_marker"] = time
            self._extent -= offset
        else:
            committed["loop_start"] += offset * self.units
            committed["start_marker"] += offset * self.units
        self.start_time = at


class LiveTrack:
    def __init__(self, name="Bass", clips=(), cuts=True):
        self.name = name
        self.arrangement_clips = sorted(clips, key=lambda c: c.start_time)
        self.cuts = cuts
        # A broken Live whose cut moves what is left without moving its markers.
        self.cut_keeps_markers = False
        self.fail_duplicate_at = None
        self.duplicate_clip_to_arrangement = MagicMock(side_effect=self._duplicate)
        self.delete_clip = MagicMock(side_effect=self._delete)

    def commit(self):
        for clip in self.arrangement_clips:
            clip.commit()

    def _sort(self):
        self.arrangement_clips.sort(key=lambda c: c.start_time)

    def _duplicate(self, clip, destination_time):
        if self.fail_duplicate_at is not None and \
                abs(destination_time - self.fail_duplicate_at) < 1e-9:
            raise RuntimeError("duplicate refused")
        new = clip.copy(destination_time)
        if self.cuts:
            self._cut_out(destination_time, new.end_time)
        self.arrangement_clips.append(new)
        self._sort()

    def _delete(self, clip):
        self.arrangement_clips = [c for c in self.arrangement_clips if c is not clip]

    def _cut_out(self, low, high):
        for clip in list(self.arrangement_clips):
            start, end = clip.start_time, clip.end_time
            if end <= low or start >= high:
                continue
            if low <= start and end <= high:
                self._delete(clip)
            elif start < low and end > high:
                right = clip.copy(start)
                right.cut_left(high)
                clip.cut_right(low)
                self.arrangement_clips.append(right)
            elif start < low:
                clip.cut_right(low)
            else:
                # Live hands back a new clip object for what is left.
                rest = clip.copy(start)
                rest.cut_left(high)
                if self.cut_keeps_markers:
                    rest._committed = dict(clip._committed)
                self._delete(clip)
                self.arrangement_clips.append(rest)
        self._sort()


def make_script(song=None, tracks=()):
    script = AbletonMCP.__new__(AbletonMCP)
    if song is None:
        song = types.SimpleNamespace(tracks=list(tracks))
    script._song = song
    script.log_message = MagicMock()
    return script


def drive(steps, *parts):
    """Run a stepped handler, ending a Live tick (commit) at every yield."""
    try:
        while True:
            next(steps)
            for part in parts:
                part.commit()
    except StopIteration as done:
        return done.value


def committing_schedule(*parts):
    """schedule_message that ends the tick, then runs the scheduled function."""
    def schedule(_delay, fn):
        for part in parts:
            part.commit()
        fn()
    return MagicMock(side_effect=schedule)


def snapshot(clip):
    keys = ("name", "start_time", "end_time", "looping", "loop_start", "loop_end",
            "start_marker", "end_marker", "muted", "color")
    return {key: getattr(clip, key) for key in keys}


def note(pitch, start, duration=0.5, velocity=100, mute=False):
    return {"pitch": pitch, "start_time": start, "duration": duration,
            "velocity": velocity, "mute": mute}


RIFF = [note(36, 0.0, 1.0), note(38, 4.5, 0.25, 90), note(43, 8.0), note(40, 15.75, 0.25, 64, True)]


# ── #13 loop read-back ───────────────────────────────────────────────


class TestLoopReadback:
    def _send(self, song, params):
        script = make_script(song)
        script.schedule_message = committing_schedule(song)
        return script._process_command({"type": "set_arrangement_loop", "params": params})

    def test_enable_reports_applied_state(self):
        song = LateSong(loop=False, loop_start=280.0, loop_length=56.0)
        response = self._send(song, {"enabled": True})
        assert response["status"] == "success"
        assert response["result"]["enabled"] is True
        assert (response["result"]["start"], response["result"]["length"]) == (280.0, 56.0)

    def test_disable_reports_applied_state(self):
        song = LateSong(loop=True, loop_start=104.0, loop_length=8.0)
        response = self._send(song, {"enabled": False})
        assert response["result"]["enabled"] is False
        assert song.loop is False

    def test_move_reports_applied_region(self):
        song = LateSong(loop=True, loop_start=0.0, loop_length=16.0)
        response = self._send(song, {"enabled": True, "start": 32.0, "length": 8.0})
        assert (response["result"]["start"], response["result"]["length"]) == (32.0, 8.0)
        assert response["result"]["requested"] == {"enabled": True, "start": 32.0,
                                                   "length": 8.0}

    def test_loop_can_start_at_beat_zero(self):
        song = LateSong(loop=True, loop_start=64.0, loop_length=8.0)
        response = self._send(song, {"enabled": True, "start": 0.0, "length": 4.0})
        assert (response["result"]["start"], response["result"]["length"]) == (0.0, 4.0)

    def test_reply_shows_what_live_applied_when_it_differs(self):
        song = LateSong(loop=False, max_length=12.0)
        response = self._send(song, {"enabled": True, "start": 8.0, "length": 64.0})
        assert response["result"]["length"] == 12.0
        assert response["result"]["requested"]["length"] == 64.0

    def test_read_back_error_is_logged_and_reported(self):
        song = LateSong()
        script = make_script(song)

        def explode(_delay, fn):
            song._applied = {}
            fn()
        script.schedule_message = MagicMock(side_effect=explode)
        response = script._process_command({"type": "set_arrangement_loop",
                                            "params": {"enabled": True}})
        assert response["status"] == "error"
        script.log_message.assert_called()


# ── #23 clip info reports markers ────────────────────────────────────


class TestClipInfoMarkers:
    def test_markers_are_reported_with_the_existing_keys(self):
        clip = LiveClip(start_time=312.0, length=16.0, looping=True, loop=(0.0, 4.0),
                        start_marker=1.0)
        info = make_script()._get_arrangement_clip_info(clip)
        assert info == {"name": "Riff", "start_time": 312.0, "end_time": 328.0, "length": 16.0,
                        "is_midi": True, "is_audio": False, "muted": False, "color": 0x112233,
                        "looping": True, "loop_start": 0.0, "loop_end": 4.0,
                        "start_marker": 1.0, "end_marker": 4.0}

    def test_arrangement_info_lists_markers(self):
        track = LiveTrack(clips=[LiveClip(start_time=312.0, length=16.0)])
        track.has_midi_input, track.has_audio_input = True, False
        song = types.SimpleNamespace(
            tracks=[track], is_playing=False, tempo=TEMPO, signature_numerator=4,
            signature_denominator=4, current_song_time=0.0, song_length=400.0, loop=False,
            loop_start=0.0, loop_length=16.0, arrangement_overdub=False, back_to_arranger=False)
        clip = make_script(song)._get_arrangement_info(-1)["tracks"][0]["arrangement_clips"][0]
        assert (clip["start_marker"], clip["end_marker"]) == (0.0, 16.0)


# ── #22 notes ────────────────────────────────────────────────────────


class TestGetArrangementClipNotes:
    def _read(self, clip, **params):
        track = LiveTrack(clips=[clip])
        return make_script(tracks=[track])._get_arrangement_clip_notes(
            dict({"track_index": 0, "clip_index": 0}, **params))

    def test_reads_each_note_with_clip_and_song_beats(self):
        clip = LiveClip(start_time=312.0, length=16.0, notes=RIFF + [note(50, 20.0)])
        result = self._read(clip)
        assert result["note_count"] == 4
        assert (result["range_start"], result["range_end"]) == (0.0, 16.0)
        assert result["notes"] == [
            {"pitch": 36, "start_time": 0.0, "duration": 1.0, "velocity": 100, "mute": False,
             "song_time": 312.0},
            {"pitch": 38, "start_time": 4.5, "duration": 0.25, "velocity": 90, "mute": False,
             "song_time": 316.5},
            {"pitch": 43, "start_time": 8.0, "duration": 0.5, "velocity": 100, "mute": False,
             "song_time": 320.0},
            {"pitch": 40, "start_time": 15.75, "duration": 0.25, "velocity": 64, "mute": True,
             "song_time": 327.75},
        ]
        assert result["clip"]["index"] == 0
        assert result["clip"]["start_marker"] == 0.0

    def test_range_limits_the_notes(self):
        clip = LiveClip(start_time=312.0, length=16.0, notes=RIFF)
        result = self._read(clip, start=4.0, end=15.0)
        assert [n["pitch"] for n in result["notes"]] == [38, 43]

    def test_hidden_note_has_no_song_time(self):
        clip = LiveClip(start_time=312.0, length=16.0, notes=RIFF + [note(50, 20.0)])
        result = self._read(clip, start=0.0, end=32.0)
        assert result["notes"][-1] == dict(note(50, 20.0), song_time=None)

    def test_looped_clip_song_times_follow_start_marker_and_loop(self):
        clip = LiveClip(start_time=100.0, length=16.0, looping=True, loop=(0.0, 4.0),
                        start_marker=2.0, notes=[note(36, 0.0), note(38, 3.0)])
        result = self._read(clip)
        assert (result["range_start"], result["range_end"]) == (0.0, 4.0)
        times = {n["pitch"]: n["song_time"] for n in result["notes"]}
        assert times == {38: 101.0, 36: 102.0}

    def test_dict_notes_are_read_too(self):
        clip = LiveClip(start_time=0.0, length=4.0)
        clip.get_notes_extended = MagicMock(return_value=[note(60, 1.0)])
        result = self._read(clip)
        assert result["notes"] == [dict(note(60, 1.0), song_time=1.0)]
        clip.get_notes_extended.assert_called_once_with(0, 128, 0.0, 4.0)

    @pytest.mark.parametrize("clip,params,message", [
        (LiveClip(midi=False), {}, "audio clip"),
        (LiveClip(), {"start": 8.0, "end": 8.0}, "must be after"),
    ])
    def test_refusals_are_logged(self, clip, params, message):
        track = LiveTrack(clips=[clip])
        script = make_script(tracks=[track])
        with pytest.raises(ValueError, match=message):
            script._get_arrangement_clip_notes(dict({"track_index": 0, "clip_index": 0},
                                                    **params))
        script.log_message.assert_called()


class TestRemoveArrangementClipNotes:
    def _remove(self, clip, **params):
        track = LiveTrack(clips=[clip])
        script = make_script(tracks=[track])
        return drive(script._remove_arrangement_clip_notes(
            dict({"track_index": 0, "clip_index": 0}, **params)), track)

    def test_removes_only_the_range_and_the_rest_reads_back(self):
        clip = LiveClip(start_time=312.0, length=16.0, notes=RIFF)
        result = self._remove(clip, start=4.0, end=9.0)
        assert result["removed"] == 2
        assert [n["pitch"] for n in result["removed_notes"]] == [38, 43]
        assert result["remaining"] == 2
        left = make_script(tracks=[LiveTrack(clips=[clip])])._get_arrangement_clip_notes(
            {"track_index": 0, "clip_index": 0})
        assert [(n["pitch"], n["start_time"]) for n in left["notes"]] == [(36, 0.0),
                                                                           (40, 15.75)]

    def test_pitch_limits_the_removal(self):
        clip = LiveClip(start_time=0.0, length=16.0,
                        notes=[note(36, 0.0), note(38, 0.0), note(36, 8.0)])
        result = self._remove(clip, pitch=36)
        assert result["removed"] == 2
        assert [n["pitch"] for n in clip.notes] == [38]

    def test_note_left_in_range_is_an_error(self):
        clip = LiveClip(start_time=0.0, length=16.0, notes=RIFF)
        clip.remove_does_nothing = True
        with pytest.raises(RuntimeError, match="Live left 4 note"):
            self._remove(clip)

    def test_audio_clip_is_refused(self):
        with pytest.raises(ValueError, match="audio clip"):
            self._remove(LiveClip(midi=False))


# ── #23 bounds ───────────────────────────────────────────────────────


class TestSetArrangementClipBounds:
    def _bounds(self, track, **params):
        script = make_script(tracks=[track])
        return drive(script._set_arrangement_clip_bounds(
            dict({"track_index": 0, "clip_index": 0}, **params)), track)

    def test_looped_clip_keeps_its_loop(self):
        clip = LiveClip(start_time=64.0, length=16.0, looping=True, loop=(0.0, 4.0))
        track = LiveTrack(clips=[clip])
        result = self._bounds(track, start_marker=1.0, end_marker=3.0)
        assert (clip.start_marker, clip.end_marker) == (1.0, 3.0)
        assert (clip.looping, clip.loop_start, clip.loop_end) == (True, 0.0, 4.0)
        assert clip.end_time == 80.0
        assert result["previous"] == {"start_marker": 0.0, "end_marker": 4.0,
                                      "loop_start": 0.0, "loop_end": 4.0}
        assert result["clip"]["start_marker"] == 1.0

    def test_unlooped_clip_plays_its_new_markers(self):
        clip = LiveClip(start_time=64.0, length=16.0, notes=RIFF)
        track = LiveTrack(clips=[clip])
        result = self._bounds(track, start_marker=2.0, end_marker=10.0)
        assert (clip.looping, clip.loop_start, clip.loop_end) == (False, 2.0, 10.0)
        assert (clip.start_marker, clip.end_marker) == (2.0, 10.0)
        assert clip.end_time == 72.0
        assert result["previous_end_time"] == 80.0

    def test_one_marker_keeps_the_other(self):
        clip = LiveClip(start_time=0.0, length=16.0)
        self._bounds(LiveTrack(clips=[clip]), end_marker=24.0)
        assert (clip.start_marker, clip.end_marker, clip.end_time) == (0.0, 24.0, 24.0)

    def test_start_past_old_end_is_set_in_an_order_live_accepts(self):
        clip = LiveClip(start_time=0.0, length=4.0)
        self._bounds(LiveTrack(clips=[clip]), start_marker=6.0, end_marker=8.0)
        assert (clip.start_marker, clip.end_marker) == (6.0, 8.0)

    @pytest.mark.parametrize("params,message", [
        ({}, "Give start_marker"),
        ({"start_marker": 4.0, "end_marker": 2.0}, "must be after"),
    ])
    def test_bad_requests_are_refused(self, params, message):
        with pytest.raises(ValueError, match=message):
            self._bounds(LiveTrack(clips=[LiveClip()]), **params)

    def test_refused_write_is_put_back(self):
        clip = LiveClip(start_time=64.0, length=16.0, looping=True, loop=(0.0, 4.0))
        clip.refuse = {"end_marker"}
        before = snapshot(clip)
        with pytest.raises(RuntimeError, match="put back"):
            self._bounds(LiveTrack(clips=[clip]), start_marker=1.0, end_marker=3.0)
        assert snapshot(clip) == before


# ── #23 split and end ────────────────────────────────────────────────


def _events(script, clip):
    return script._note_events(clip)


class TestSplitArrangementClip:
    def _split(self, track, beat, clip_index=0):
        script = make_script(tracks=[track])
        return script, drive(script._split_arrangement_clip(
            {"track_index": 0, "clip_index": clip_index, "beat": beat}), track)

    def test_midi_clip_splits_into_two_that_play_the_same_notes(self):
        clip = LiveClip(start_time=312.0, length=16.0, notes=RIFF)
        after = LiveClip("Next", start_time=344.0, length=8.0)
        track = LiveTrack(clips=[clip, after])
        script = make_script(tracks=[track])
        played = _events(script, clip)
        script, result = self._split(track, 320.0)
        left, right = result["left"], result["right"]
        assert (left["start_time"], left["end_time"], left["index"]) == (312.0, 320.0, 0)
        assert (right["start_time"], right["end_time"], right["index"]) == (320.0, 328.0, 1)
        assert (right["start_marker"], right["loop_end"], right["looping"]) == (8.0, 16.0, False)
        assert result["original"] == {"start_time": 312.0, "end_time": 328.0}
        pieces = track.arrangement_clips
        assert [(c.start_time, c.end_time) for c in pieces] == [(312.0, 320.0), (320.0, 328.0),
                                                                (344.0, 352.0)]
        assert sorted(_events(script, pieces[0]) + _events(script, pieces[1])) == played
        assert pieces[2] is after and pieces[2].end_time == 352.0

    def test_looped_midi_clip_pieces_keep_the_loop(self):
        clip = LiveClip(start_time=0.0, length=16.0, looping=True, loop=(0.0, 4.0),
                        notes=[note(36, 0.0), note(38, 1.5), note(41, 3.0)])
        track = LiveTrack(clips=[clip])
        script = make_script(tracks=[track])
        played = _events(script, clip)
        script, result = self._split(track, 6.0)
        for piece in (result["left"], result["right"]):
            assert (piece["looping"], piece["loop_start"], piece["loop_end"]) == (True, 0.0, 4.0)
        assert result["right"]["start_marker"] == 2.0
        left, right = track.arrangement_clips
        assert sorted(_events(script, left) + _events(script, right)) == played

    def test_warped_audio_clip_splits_at_the_beat(self):
        clip = LiveClip("Gtr", start_time=32.0, length=16.0, midi=False, warping=True)
        track = LiveTrack(clips=[clip])
        _, result = self._split(track, 36.0)
        assert (result["left"]["end_time"], result["right"]["start_time"]) == (36.0, 36.0)
        assert result["right"]["start_marker"] == 4.0
        assert result["right"]["file_path"] == "/Samples/Gtr.wav"

    def test_unwarped_audio_clip_splits_at_the_matching_second(self):
        clip = LiveClip("Vox", start_time=32.0, length=16.0, midi=False, warping=False,
                        loop=(60.0, 68.0))
        track = LiveTrack(clips=[clip])
        _, result = self._split(track, 40.0)
        assert result["left"]["loop_end"] == pytest.approx(64.0)
        assert result["right"]["start_marker"] == pytest.approx(64.0)
        assert result["right"]["end_time"] == pytest.approx(48.0)

    @pytest.mark.parametrize("beat", [312.0, 328.0, 300.0, 400.0])
    def test_beat_must_be_inside_the_clip(self, beat):
        track = LiveTrack(clips=[LiveClip(start_time=312.0, length=16.0)])
        with pytest.raises(ValueError, match="not inside"):
            self._split(track, beat)
        assert len(track.arrangement_clips) == 1

    def test_original_is_left_alone_when_live_does_not_cut_the_copy(self):
        clip = LiveClip(start_time=312.0, length=16.0, notes=RIFF)
        before = snapshot(clip)
        track = LiveTrack(clips=[clip], cuts=False)
        with pytest.raises(RuntimeError, match="No clip starts at beat .*original clip was "
                                               "not changed"):
            self._split(track, 320.0)
        assert track.arrangement_clips == [clip]
        assert snapshot(clip) == before

    def test_wrong_content_after_the_cut_is_put_back(self):
        clip = LiveClip(start_time=312.0, length=16.0, notes=RIFF)
        before = snapshot(clip)
        track = LiveTrack(clips=[clip])
        track.cut_keeps_markers = True
        with pytest.raises(RuntimeError, match="differs from the original.*original clip was "
                                               "put back at beat 312"):
            self._split(track, 320.0)
        assert len(track.arrangement_clips) == 1
        restored = track.arrangement_clips[0]
        assert snapshot(restored) == before
        assert restored.notes == clip.notes

    def test_failed_put_back_keeps_the_backup(self):
        clip = LiveClip(start_time=312.0, length=16.0)
        track = LiveTrack(clips=[clip])
        track.cut_keeps_markers = True
        track.fail_duplicate_at = 312.0
        with pytest.raises(RuntimeError, match="an intact copy is kept at beat 344"):
            self._split(track, 320.0)
        assert [c.start_time for c in track.arrangement_clips] == [344.0]

    def test_mismatches_name_every_difference(self):
        script = make_script()
        original = LiveClip(start_time=0.0, length=16.0, notes=RIFF)
        before = script._clip_snapshot(original)
        events = script._note_events(original)
        left = LiveClip(start_time=0.0, length=8.0, notes=RIFF)
        right = LiveClip(start_time=8.0, length=10.0, loop=(0.0, 10.0), notes=RIFF)
        stray = LiveClip("Stray", start_time=12.0, length=2.0)
        track = LiveTrack(clips=[left, right, stray])
        wrong = script._split_mismatches(track, before, events, left, right, 0.0, 8.0, 16.0,
                                         8.0)
        assert "1 other clip(s) inside the original's range" in wrong
        assert "right ends at beat 18.0, not 16.0" in wrong
        assert any(w.startswith("right loop_end: expected 16.0, got 10.0") for w in wrong)
        assert any(w.startswith("the pieces play") for w in wrong)

    def test_clip_time_outside_the_clip_is_refused(self):
        script = make_script()
        with pytest.raises(ValueError, match="not inside"):
            script._clip_time_at(LiveClip(start_time=0.0, length=4.0), 9.0)

    def test_empty_loop_plays_nothing(self):
        clip = LiveClip(start_time=0.0, length=4.0, looping=True, loop=(2.0, 2.0),
                        notes=[note(36, 2.0)])
        script = make_script()
        assert script._first_song_time(clip, 2.0) is None
        assert script._note_events(clip) == []

    def test_too_many_notes_to_verify_is_refused(self):
        clip = LiveClip(start_time=0.0, length=64.0, looping=True, loop=(0.0, 1.0),
                        notes=[note(36, 0.0)])
        track = LiveTrack(clips=[clip])
        script = make_script(tracks=[track])
        script.MAX_NOTE_EVENTS = 10
        with pytest.raises(ValueError, match="too many to verify"):
            drive(script._split_arrangement_clip(
                {"track_index": 0, "clip_index": 0, "beat": 8.0}), track)
        assert track.arrangement_clips == [clip]


class TestSetArrangementClipEnd:
    def _end(self, track, end_beat, clip_index=0):
        script = make_script(tracks=[track])
        return script, drive(script._set_arrangement_clip_end(
            {"track_index": 0, "clip_index": clip_index, "end_beat": end_beat}), track)

    def test_looped_clip_is_trimmed_and_keeps_its_loop(self):
        clip = LiveClip(start_time=0.0, length=16.0, looping=True, loop=(0.0, 4.0),
                        start_marker=1.0, notes=[note(36, 0.0), note(38, 2.0)])
        after = LiveClip("Next", start_time=32.0, length=8.0)
        track = LiveTrack(clips=[clip, after])
        script = make_script(tracks=[track])
        played = [e for e in _events(script, clip) if e[0] < 10.0]
        script, result = self._end(track, 10.0)
        trimmed = track.arrangement_clips[0]
        assert result["method"] == "split"
        assert result["previous_end_time"] == 16.0
        assert (trimmed.start_time, trimmed.end_time) == (0.0, 10.0)
        assert (trimmed.looping, trimmed.loop_start, trimmed.loop_end,
                trimmed.start_marker) == (True, 0.0, 4.0, 1.0)
        assert _events(script, trimmed) == played
        assert [c.start_time for c in track.arrangement_clips] == [0.0, 32.0]

    def test_unlooped_clip_is_trimmed(self):
        clip = LiveClip(start_time=312.0, length=16.0, notes=RIFF)
        track = LiveTrack(clips=[clip])
        _, result = self._end(track, 324.0)
        trimmed = track.arrangement_clips[0]
        assert (trimmed.end_time, trimmed.looping, trimmed.loop_start,
                trimmed.start_marker) == (324.0, False, 0.0, 0.0)
        assert result["clip"]["end_time"] == 324.0
        assert len(track.arrangement_clips) == 1

    def test_unwarped_audio_clip_is_trimmed(self):
        clip = LiveClip("Vox", start_time=0.0, length=16.0, midi=False, warping=False,
                        loop=(10.0, 18.0))
        track = LiveTrack(clips=[clip])
        self._end(track, 12.0)
        trimmed = track.arrangement_clips[0]
        assert trimmed.end_time == pytest.approx(12.0)
        assert trimmed.loop_end == pytest.approx(16.0)

    def test_unlooped_clip_can_be_lengthened(self):
        clip = LiveClip(start_time=0.0, length=16.0)
        track = LiveTrack(clips=[clip])
        _, result = self._end(track, 20.0)
        assert result["method"] == "markers"
        assert (clip.end_time, clip.loop_end, clip.end_marker, clip.looping) == (20.0, 20.0,
                                                                                 20.0, False)

    def test_looped_clip_cannot_be_lengthened(self):
        clip = LiveClip(start_time=0.0, length=16.0, looping=True, loop=(0.0, 4.0))
        before = snapshot(clip)
        with pytest.raises(ValueError, match="no call that lengthens a looped"):
            self._end(LiveTrack(clips=[clip]), 20.0)
        assert snapshot(clip) == before

    def test_lengthening_into_another_clip_is_refused(self):
        track = LiveTrack(clips=[LiveClip(start_time=0.0, length=16.0),
                                 LiveClip("Next", start_time=18.0, length=4.0)])
        with pytest.raises(ValueError, match="would run into Next"):
            self._end(track, 20.0)

    def test_lengthening_live_refuses_is_put_back(self):
        clip = LiveClip(start_time=0.0, length=16.0)
        clip.refuse = {"loop_end"}
        before = snapshot(clip)
        with pytest.raises(RuntimeError, match="put back"):
            self._end(LiveTrack(clips=[clip]), 20.0)
        assert snapshot(clip) == before

    def test_same_end_changes_nothing(self):
        clip = LiveClip(start_time=0.0, length=16.0)
        track = LiveTrack(clips=[clip])
        _, result = self._end(track, 16.0)
        assert result["method"] == "unchanged"
        track.duplicate_clip_to_arrangement.assert_not_called()

    def test_end_at_or_before_start_is_refused(self):
        with pytest.raises(ValueError, match="must be after the clip's start"):
            self._end(LiveTrack(clips=[LiveClip(start_time=8.0)]), 8.0)


# ── command routing and errors ───────────────────────────────────────


def _one_step(result):
    yield
    return result


class TestDispatch:
    @pytest.mark.parametrize("command,handler", [
        ("remove_arrangement_clip_notes", "_remove_arrangement_clip_notes"),
        ("set_arrangement_clip_bounds", "_set_arrangement_clip_bounds"),
        ("set_arrangement_clip_end", "_set_arrangement_clip_end"),
        ("split_arrangement_clip", "_split_arrangement_clip"),
    ])
    def test_writes_run_as_stepped_handlers(self, command, handler):
        script = make_script()
        script.schedule_message = MagicMock(side_effect=lambda _delay, fn: fn())
        params = {"track_index": 1, "clip_index": 2}
        setattr(script, handler, MagicMock(return_value=_one_step({"ok": command})))
        response = script._process_command({"type": command, "params": params})
        assert response == {"status": "success", "result": {"ok": command}}
        getattr(script, handler).assert_called_once_with(params)
        assert [c.args[0] for c in script.schedule_message.call_args_list] == [0, 1]

    def test_loop_runs_as_a_stepped_handler(self):
        script = make_script()
        script.schedule_message = MagicMock(side_effect=lambda _delay, fn: fn())
        script._set_arrangement_loop_steps = MagicMock(return_value=_one_step({"ok": 1}))
        response = script._process_command({"type": "set_arrangement_loop", "params": {
            "enabled": False, "start": 1.0, "length": 2.0}})
        assert response == {"status": "success", "result": {"ok": 1}}
        script._set_arrangement_loop_steps.assert_called_once_with(False, 1.0, 2.0)

    def test_notes_read_runs_inline(self):
        script = make_script()
        script.schedule_message = MagicMock()
        script._get_arrangement_clip_notes = MagicMock(return_value={"notes": []})
        params = {"track_index": 0, "clip_name": "Riff"}
        response = script._process_command({"type": "get_arrangement_clip_notes",
                                            "params": params})
        assert response == {"status": "success", "result": {"notes": []}}
        script._get_arrangement_clip_notes.assert_called_once_with(params)
        script.schedule_message.assert_not_called()

    @pytest.mark.parametrize("handler,params", [
        ("_remove_arrangement_clip_notes", {}),
        ("_set_arrangement_clip_bounds", {"start_marker": 1.0}),
        ("_set_arrangement_clip_end", {"end_beat": 4.0}),
        ("_split_arrangement_clip", {"beat": 2.0}),
    ])
    def test_stepped_handler_errors_are_logged(self, handler, params):
        script = make_script(tracks=[LiveTrack(clips=[LiveClip()])])
        with pytest.raises(IndexError):
            next(getattr(script, handler)(dict(params, track_index=5, clip_index=0)))
        script.log_message.assert_called()

    @pytest.mark.parametrize("handler,name", [
        ("_set_arrangement_clip_end", "end_beat"),
        ("_split_arrangement_clip", "beat"),
    ])
    def test_missing_beat_is_refused(self, handler, name):
        script = make_script(tracks=[LiveTrack(clips=[LiveClip()])])
        with pytest.raises(ValueError, match="Give " + name):
            next(getattr(script, handler)({"track_index": 0, "clip_index": 0}))

    def test_read_error_is_logged(self):
        script = make_script(tracks=[])
        with pytest.raises(IndexError):
            script._get_arrangement_clip_notes({"track_index": 0, "clip_index": 0})
        script.log_message.assert_called()
