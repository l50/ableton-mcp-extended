"""Unit tests for the Remote Script's arrangement clip region copy and move,
run against stub Live objects that duplicate, trim and delete clips the way
Live does."""
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


class WarpMarker:
    def __init__(self, sample_time, beat_time):
        self.sample_time = sample_time
        self.beat_time = beat_time


def _deferred(prop, low=None, high=None):
    """A clip setting that, as in Live, reads back at once but takes effect
    (end_time, duplicates) only when the main-thread tick ends."""

    def get(self):
        return self._pending.get(prop, self._committed[prop])

    def set(self, value):
        if prop not in self.refuse:
            start = value if prop == low else getattr(self, low) if low else None
            end = value if prop == high else getattr(self, high) if high else None
            if start is not None and end is not None and start > end:
                raise RuntimeError("start after end")
            self._pending[prop] = value

    return property(get, set)


class Clip:
    """An arrangement clip whose right edge follows its committed markers.

    Unwarped audio counts in seconds of the file, so its length in beats
    follows the tempo; warped audio and MIDI count in beats. Marker and loop
    edits wait for commit(), which the step driver calls on every yield.
    """

    loop_start = _deferred("loop_start", "loop_start", "loop_end")
    loop_end = _deferred("loop_end", "loop_start", "loop_end")
    start_marker = _deferred("start_marker", "start_marker", "end_marker")
    end_marker = _deferred("end_marker", "start_marker", "end_marker")
    looping = _deferred("looping")

    def __init__(self, name="Take", start_time=0.0, audio=True, warping=False,
                 loop_start=0.0, loop_end=10.0, looping=False, warp_markers=None,
                 file_path="/Samples/Recorded/Take.wav", sample_length=48000 * 120,
                 sample_rate=48000.0, gain=0.7, pitch_coarse=0, pitch_fine=0.0,
                 warp_mode=4):
        self._committed = {"loop_start": loop_start, "loop_end": loop_end,
                           "start_marker": loop_start, "end_marker": loop_end,
                           "looping": looping}
        self._pending = {}
        self.refuse = set()
        self.name = name
        self.start_time = start_time
        self.is_audio_clip = audio
        self.is_midi_clip = not audio
        self.warping = warping
        self.warp_markers = list(warp_markers or [])
        self.file_path = file_path if audio else None
        self.sample_length = sample_length
        self.sample_rate = sample_rate
        self.gain = gain
        self.pitch_coarse = pitch_coarse
        self.pitch_fine = pitch_fine
        self.warp_mode = warp_mode
        self.muted = False
        self.color = 0x112233
        self.tempo = TEMPO

    def commit(self):
        self._committed.update(self._pending)
        self._pending = {}

    @property
    def end_time(self):
        span = self._committed["loop_end"] - self._committed["loop_start"]
        if self.is_audio_clip and not self.warping:
            span = span * self.tempo / 60.0
        return self.start_time + span

    def copy_to(self, start_time):
        """A duplicate, made from the committed state only."""
        clip = Clip.__new__(Clip)
        clip.__dict__.update(self.__dict__)
        clip._committed = dict(self._committed)
        clip._pending = {}
        clip.warp_markers = list(self.warp_markers)
        clip.refuse = set(self.refuse)
        clip.start_time = start_time
        return clip


class _LongerClip(Clip):
    """A copy that comes out 4 beats longer than its markers say."""

    @property
    def end_time(self):
        return Clip.end_time.fget(self) + 4.0


class Track:
    def __init__(self, name="Vocals", clips=(), midi=False):
        self.name = name
        self.has_midi_input = midi
        self.has_audio_input = not midi
        self.arrangement_clips = list(clips)
        self.duplicate_clip_to_arrangement = MagicMock(side_effect=self._duplicate)
        self.delete_clip = MagicMock(side_effect=self._delete)
        self.mutate_copies = None
        self.tempo_at = lambda time: TEMPO

    def _duplicate(self, clip, destination_time):
        copy = clip.copy_to(destination_time)
        copy.tempo = self.tempo_at(destination_time)
        if self.mutate_copies:
            self.mutate_copies(copy)
        end = copy.end_time
        # Live overwrites what the copy lands on; drop fully covered clips.
        self.arrangement_clips = [
            c for c in self.arrangement_clips
            if not (c.start_time >= destination_time and c.end_time <= end)]
        self.arrangement_clips.append(copy)
        self.arrangement_clips.sort(key=lambda c: c.start_time)

    def _delete(self, clip):
        self.arrangement_clips.remove(clip)


class Song:
    def __init__(self, tracks=(), return_tracks=()):
        self.tracks = list(tracks)
        self.return_tracks = list(return_tracks)
        self.master_track = Track("Master")
        self.tempo = TEMPO


def make_script(*tracks):
    script = AbletonMCP.__new__(AbletonMCP)
    script._song = Song(tracks)
    script.log_message = MagicMock()
    return script


def unwarped_take(**kw):
    """A 120 s unwarped take showing 60-70 s of its file at beat 32."""
    kw.setdefault("start_time", 32.0)
    kw.setdefault("loop_start", 60.0)
    kw.setdefault("loop_end", 70.0)
    return Clip(**kw)


def warped_take(**kw):
    """A warped take: 2 beats per second up to 10 s, then 1 beat per second."""
    markers = [WarpMarker(0.0, 0.0), WarpMarker(10.0, 20.0), WarpMarker(20.0, 30.0)]
    kw.setdefault("warping", True)
    kw.setdefault("warp_markers", markers)
    kw.setdefault("loop_start", 0.0)
    kw.setdefault("loop_end", 30.0)
    return Clip(**kw)


def drive(script, steps):
    """Run a stepped handler the way _run_steps does, ending a tick per yield."""
    try:
        while True:
            next(steps)
            for track in script._song.tracks:
                for clip in track.arrangement_clips:
                    clip.commit()
    except StopIteration as done:
        return done.value


def region(script, **params):
    base = {"track_index": 0, "clip_index": 0, "region_start": 66.10, "region_end": 67.04,
            "destination_time": 8.0}
    base.update(params)
    return drive(script, script._copy_arrangement_audio_region(base))


def move(script, **params):
    base = {"track_index": 0, "clip_index": 0, "destination_time": 100.0}
    base.update(params)
    return drive(script, script._move_arrangement_clip(base))


def clip_at(track, start):
    matches = [c for c in track.arrangement_clips if abs(c.start_time - start) < 1e-6]
    assert len(matches) == 1, [c.start_time for c in track.arrangement_clips]
    return matches[0]


# ── region copy ──────────────────────────────────────────────────────


class TestCopyRegion:
    def test_unwarped_region_copies_exact_seconds_of_same_file(self):
        take = unwarped_take()
        track = Track(clips=[take])
        result = region(make_script(track))

        copy = clip_at(track, 8.0)
        assert copy.file_path == take.file_path
        assert copy.warping is False
        assert (copy.loop_start, copy.loop_end) == (66.10, 67.04)
        assert (copy.start_marker, copy.end_marker) == (66.10, 67.04)
        assert copy.end_time - copy.start_time == pytest.approx(0.94 * TEMPO / 60.0)
        assert (copy.gain, copy.pitch_coarse, copy.pitch_fine, copy.warp_mode) == \
            (0.7, 0, 0.0, 4)
        assert result["region_start"] == 66.10 and result["region_end"] == 67.04
        assert result["file_path"] == take.file_path and result["warping"] is False
        assert result["source_clip"] == "Take" and result["track"] == "Vocals"
        assert result["overlapped_clips"] == []

    def test_source_is_untouched_and_staging_copy_is_removed(self):
        take = unwarped_take()
        track = Track(clips=[take])
        region(make_script(track))

        assert len(track.arrangement_clips) == 2
        assert (take.start_time, take.loop_start, take.loop_end) == (32.0, 60.0, 70.0)
        staged = track.duplicate_clip_to_arrangement.call_args_list[0][0][1]
        # Past the take's end (beat 52) and the reserved destination range.
        assert staged >= 52.0 + 16.0
        track.delete_clip.assert_called_once()

    def test_warped_region_maps_seconds_through_warp_markers(self):
        take = warped_take(looping=True)
        track = Track(clips=[take])
        region(make_script(track), region_start=5.0, region_end=15.0, destination_time=64.0)

        copy = clip_at(track, 64.0)
        # 5 s -> beat 10 (2 beats/s); 15 s -> beat 25 (1 beat/s after 10 s).
        assert (copy.loop_start, copy.loop_end) == (10.0, 25.0)
        assert copy.looping is False and copy.warping is True
        assert copy.end_time - copy.start_time == pytest.approx(15.0)

    def test_warped_region_extrapolates_past_last_marker(self):
        take = warped_take(loop_end=40.0)
        track = Track(clips=[take])
        region(make_script(track), region_start=25.0, region_end=30.0, destination_time=64.0)
        copy = clip_at(track, 64.0)
        assert (copy.loop_start, copy.loop_end) == (35.0, 40.0)

    def test_warp_markers_as_dicts(self):
        take = warped_take(warp_markers=[{"sample_time": 0.0, "beat_time": 0.0},
                                         {"sample_time": 4.0, "beat_time": 8.0}])
        track = Track(clips=[take])
        region(make_script(track), region_start=1.0, region_end=2.0, destination_time=64.0)
        assert (clip_at(track, 64.0).loop_start, clip_at(track, 64.0).loop_end) == (2.0, 4.0)

    def test_warped_without_markers_is_refused(self):
        track = Track(clips=[warped_take(warp_markers=[WarpMarker(0.0, 0.0)])])
        with pytest.raises(ValueError, match="fewer than two warp markers"):
            region(make_script(track), region_start=1.0, region_end=2.0)
        track.duplicate_clip_to_arrangement.assert_not_called()

    def test_region_before_current_window_sets_start_first(self):
        take = unwarped_take()
        track = Track(clips=[take])
        region(make_script(track), region_start=10.0, region_end=11.0)
        copy = clip_at(track, 8.0)
        assert (copy.loop_start, copy.loop_end) == (10.0, 11.0)

    def test_region_after_current_window_sets_end_first(self):
        take = unwarped_take()
        track = Track(clips=[take])
        region(make_script(track), region_start=90.0, region_end=91.5)
        copy = clip_at(track, 8.0)
        assert (copy.start_marker, copy.end_marker) == (90.0, 91.5)

    def test_by_clip_name(self):
        track = Track(clips=[Clip("Other", 0.0), unwarped_take(name="Vocals 0001")])
        result = region(make_script(track), clip_index=-1, clip_name="Vocals 0001",
                        destination_time=200.0)
        assert result["source_clip"] == "Vocals 0001"
        assert result["name"] == "Vocals 0001"
        assert clip_at(track, 200.0).name == "Vocals 0001"

    def test_overlap_reported(self):
        neighbour = Clip("Chorus", 8.5, loop_start=0.0, loop_end=1.0)
        track = Track(clips=[unwarped_take(), neighbour])
        result = region(make_script(track))
        assert result["overlapped_clips"] == ["Chorus"]

    @pytest.mark.parametrize("start,end", [(-1.0, 1.0), (5.0, 5.0), (6.0, 5.0), (100.0, 121.0)])
    def test_region_outside_file_refused(self, start, end):
        track = Track(clips=[unwarped_take()])
        with pytest.raises(ValueError, match="not inside the clip's audio file"):
            region(make_script(track), region_start=start, region_end=end)
        track.duplicate_clip_to_arrangement.assert_not_called()

    def test_midi_source_refused(self):
        track = Track(clips=[Clip(audio=False)], midi=True)
        with pytest.raises(ValueError, match="MIDI clip"):
            region(make_script(track))

    @pytest.mark.parametrize("destination", [-4.0, 1576800.0 - 1.0])
    def test_destination_outside_live_range_refused_before_any_change(self, destination):
        take = unwarped_take()
        track = Track(clips=[take])
        with pytest.raises(ValueError, match="outside Live's range"):
            region(make_script(track), destination_time=destination)
        track.duplicate_clip_to_arrangement.assert_not_called()
        assert track.arrangement_clips == [take]

    def test_unwarped_copy_to_spot_with_other_tempo(self):
        take = unwarped_take()
        track = Track(clips=[take])
        track.tempo_at = lambda time: 60.0 if time < 30.0 else TEMPO
        result = region(make_script(track))

        copy = clip_at(track, 8.0)
        assert (copy.start_marker, copy.end_marker) == (66.10, 67.04)
        assert copy.end_time - copy.start_time == pytest.approx(0.94)
        assert result["start_time"] == 8.0
        assert len(track.arrangement_clips) == 2

    def test_live_refusing_markers_aborts_before_destination(self):
        take = unwarped_take()
        take.refuse = {"loop_start", "start_marker"}
        track = Track(clips=[take])
        with pytest.raises(RuntimeError, match="did not narrow"):
            region(make_script(track))
        # Only the staged copy was made, and it was removed again.
        assert track.duplicate_clip_to_arrangement.call_count == 1
        assert track.arrangement_clips == [take]

    def test_copy_differing_from_source_is_reported(self):
        track = Track(clips=[unwarped_take()])
        calls = []

        def drop_gain(copy):
            calls.append(copy)
            if len(calls) == 2:
                copy.gain = 0.1

        take = track.arrangement_clips[0]
        track.mutate_copies = drop_gain
        with pytest.raises(RuntimeError, match="differs from what was asked, so it was "
                                               "removed: gain"):
            region(make_script(track))
        assert track.arrangement_clips == [take]

    def test_missing_copy_is_reported(self):
        track = Track(clips=[unwarped_take()])
        track.duplicate_clip_to_arrangement = MagicMock()
        with pytest.raises(RuntimeError, match="did not create the copy"):
            region(make_script(track))


# ── move ─────────────────────────────────────────────────────────────


class TestMove:
    def test_move_later_on_same_track(self):
        take = unwarped_take()
        track = Track(clips=[take])
        result = move(make_script(track))

        assert take not in track.arrangement_clips
        moved = clip_at(track, 100.0)
        assert (moved.loop_start, moved.loop_end, moved.file_path) == \
            (60.0, 70.0, take.file_path)
        assert result["moved_from"] == 32.0 and result["staged_at"] is None
        assert result["start_time"] == 100.0 and result["overlapped_clips"] == []
        assert track.duplicate_clip_to_arrangement.call_count == 1

    def test_move_overlapping_itself_stages_first(self):
        take = unwarped_take()  # beats 32-52
        track = Track(clips=[take])
        result = move(make_script(track), destination_time=40.0)

        assert [c.start_time for c in track.arrangement_clips] == [40.0]
        assert result["staged_at"] >= 60.0 + 16.0
        assert track.duplicate_clip_to_arrangement.call_count == 2
        assert track.delete_clip.call_count == 2

    def test_move_warped_clip(self):
        take = warped_take(start_time=0.0)
        track = Track("A", [take])
        result = move(make_script(track), destination_time=64.0)
        assert take not in track.arrangement_clips
        assert clip_at(track, 64.0).warping is True
        assert result["track"] == "A"

    def test_move_midi_clip(self):
        midi = Clip("Riff", 0.0, audio=False, loop_end=4.0)
        track = Track("Keys", [midi], midi=True)
        result = move(make_script(track), destination_time=16.0)
        assert result["is_midi"] is True
        assert [c.start_time for c in track.arrangement_clips] == [16.0]

    def test_move_overlap_with_other_clip_reported(self):
        take = unwarped_take()
        other = Clip("Verse", 104.0, loop_start=0.0, loop_end=1.0)
        track = Track(clips=[take, other])
        result = move(make_script(track))
        assert result["overlapped_clips"] == ["Verse"]

    @pytest.mark.parametrize("start,destination", [(0.0, -2.0), (32.0, 1576800.0 - 5.0)])
    def test_destination_outside_live_range_refused_before_any_change(self, start,
                                                                        destination):
        take = unwarped_take(start_time=start)
        track = Track(clips=[take])
        with pytest.raises(ValueError, match="outside Live's range"):
            move(make_script(track), destination_time=destination)
        track.duplicate_clip_to_arrangement.assert_not_called()
        track.delete_clip.assert_not_called()
        assert track.arrangement_clips == [take]

    def test_unwarped_move_to_spot_with_other_tempo(self):
        take = unwarped_take()
        track = Track(clips=[take])
        track.tempo_at = lambda time: 90.0 if time >= 100.0 else TEMPO
        move(make_script(track))
        moved = clip_at(track, 100.0)
        assert track.arrangement_clips == [moved]
        assert (moved.start_marker, moved.end_marker) == (60.0, 70.0)
        assert moved.end_time - moved.start_time == pytest.approx(15.0)

    def test_unwarped_move_onto_itself_with_other_staging_tempo(self):
        take = unwarped_take()  # beats 32-52
        track = Track(clips=[take])
        track.tempo_at = lambda time: 60.0 if time >= 70.0 else TEMPO
        result = move(make_script(track), destination_time=40.0)
        assert result["staged_at"] >= 70.0
        moved = clip_at(track, 40.0)
        assert track.arrangement_clips == [moved]
        assert (moved.start_marker, moved.end_marker) == (60.0, 70.0)

    def test_warped_length_mismatch_still_refused(self):
        take = warped_take(start_time=0.0)
        track = Track(clips=[take])
        track.mutate_copies = lambda copy: setattr(copy, "__class__", _LongerClip)
        with pytest.raises(RuntimeError, match="did not match the original, so it was "
                                               "removed: length"):
            move(make_script(track))
        assert track.arrangement_clips == [take]

    def test_move_to_same_place_refused(self):
        track = Track(clips=[unwarped_take()])
        with pytest.raises(ValueError, match="already at beat"):
            move(make_script(track), destination_time=32.0)
        track.duplicate_clip_to_arrangement.assert_not_called()

    def test_bad_copy_is_removed_and_original_kept(self):
        take = unwarped_take()
        track = Track(clips=[take])
        track.mutate_copies = lambda copy: setattr(copy, "pitch_coarse", 3)
        with pytest.raises(RuntimeError, match="did not match the original"):
            move(make_script(track))
        assert track.arrangement_clips == [take]

    def test_failed_final_copy_keeps_staged_copy(self):
        take = unwarped_take()
        track = Track(clips=[take])
        calls = []

        def break_second(copy):
            calls.append(copy)
            if len(calls) == 2:
                copy.gain = 0.0

        track.mutate_copies = break_second
        with pytest.raises(RuntimeError, match="intact copy at beat"):
            move(make_script(track), destination_time=40.0)
        assert take not in track.arrangement_clips
        assert len(track.arrangement_clips) == 1
        assert track.arrangement_clips[0].gain == 0.7


class TestStepRunner:
    @staticmethod
    def _steps(fail_at=None):
        for step in range(2):
            if step == fail_at:
                raise ValueError("step {0} failed".format(step))
            yield
        return {"ok": True}

    def _run(self, steps):
        script = make_script()
        script.schedule_message = MagicMock(side_effect=lambda _delay, fn: fn())
        replies = []
        script._run_steps(steps, types.SimpleNamespace(put=replies.append))
        return script, replies

    def test_each_yield_waits_one_tick_then_replies(self):
        script, replies = self._run(self._steps())
        assert replies == [{"status": "success", "result": {"ok": True}}]
        assert [c.args[0] for c in script.schedule_message.call_args_list] == [1, 1]

    @pytest.mark.parametrize("fail_at", [0, 1])
    def test_error_in_any_step_is_the_reply(self, fail_at):
        script, replies = self._run(self._steps(fail_at))
        assert replies == [{"status": "error", "message": "step {0} failed".format(fail_at)}]
        script.log_message.assert_called()

    def test_marker_edits_are_committed_before_the_copy_is_made(self):
        take = unwarped_take()
        track = Track(clips=[take])
        script = make_script(track)
        steps = script._copy_arrangement_audio_region(
            {"track_index": 0, "clip_index": 0, "region_start": 66.10, "region_end": 67.04,
             "destination_time": 8.0})
        # Without a tick between narrowing and duplicating, Live copies the
        # full-length clip; the handler must notice rather than report success.
        with pytest.raises(RuntimeError, match="differs from what was asked"):
            for _ in steps:
                pass


class TestDispatch:
    @pytest.mark.parametrize("command,handler", [
        ("copy_arrangement_audio_region", "_copy_arrangement_audio_region"),
        ("move_arrangement_clip", "_move_arrangement_clip"),
    ])
    def test_routes_to_stepped_handler(self, command, handler):
        def steps(params):
            assert params == {"x": 1}
            yield
            return {"ok": True}

        script = make_script()
        setattr(script, handler, MagicMock(side_effect=steps))
        script.schedule_message = MagicMock(side_effect=lambda _delay, fn: fn())
        response = script._process_command({"type": command, "params": {"x": 1}})
        assert response == {"status": "success", "result": {"ok": True}}
        getattr(script, handler).assert_called_once_with({"x": 1})


class TestArrangementInfo:
    def test_audio_clip_info_reports_file_and_warping(self):
        info = make_script()._get_arrangement_clip_info(unwarped_take())
        assert info["file_path"] == "/Samples/Recorded/Take.wav"
        assert info["warping"] is False

    def test_midi_clip_info_has_no_file(self):
        info = make_script()._get_arrangement_clip_info(Clip(audio=False))
        assert "file_path" not in info
