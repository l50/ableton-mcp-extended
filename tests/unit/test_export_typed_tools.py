"""Issue 11, tool level: the new start, length and rendered-track parameters, and compatibility.

The exporter is stubbed for most of these (as in test_export_tools.py): they check which
range the tools work out from the Set and hand over. TestCompat runs the real exporter
against the FakeLive and checks that every call that worked before still works and the
documented reply fields are still there.
"""
import inspect
import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from MCP_Server import connection, live_export as le, tools  # noqa: E402
from tests.unit.test_export_tools import (  # noqa: E402,F401
    ARRANGEMENT, CTX, FakeAbleton, run, with_transport)
from tests.unit.test_export_ui_guard import FakeAbleton as GuardAbleton  # noqa: E402
from tests.unit.test_live_export import WAV_24, FakeClock, afinfo_for, make  # noqa: E402

TRACKS = {"tracks": [
    {"index": 1, "name": "Guitar", "kind": "midi"}, {"index": 2, "name": "Bass", "kind": "audio"},
    {"index": 3, "name": "Bass", "kind": "audio"}, {"index": None, "name": "A Reverb", "kind": "return"},
]}


def abl(**replies):
    return FakeAbleton({"get_arrangement_info": ARRANGEMENT, "list_tracks": TRACKS, **replies})


class TestTypedRangeParameters:
    def test_start_bar_and_length_bars_are_typed_as_beats(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), start_bar=5, length_bars=14)
        assert out["ok"] is True and out["render_range"] == "custom"
        req = seen["request"]
        assert (req.start_beats, req.length_beats) == (16.0, 56.0)
        assert req.expected_seconds == 28.0          # 56 beats at 120 BPM
        assert out["range_rounded"] is False
        assert [c for c, _ in fake.calls] == ["get_arrangement_info"]

    def test_a_length_alone_starts_at_the_start_of_the_song(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), length_bars=2)
        assert (seen["request"].start_beats, seen["request"].length_beats) == (0.0, 8.0)

    def test_beats_are_taken_as_given(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), start_beat=2.5, length_beats=7.25)
        assert (seen["request"].start_beats, seen["request"].length_beats) == (2.5, 7.25)

    def test_a_start_alone_runs_to_the_end_of_the_arrangement(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), start_beat=8.0)
        assert (seen["request"].start_beats, seen["request"].length_beats) == (8.0, 56.5)

    def test_bars_win_over_beats(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), start_bar=2, start_beat=100.0,
                              length_bars=1, length_beats=100.0)
        assert (seen["request"].start_beats, seen["request"].length_beats) == (4.0, 4.0)

    def test_a_named_range_beats_render_range(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), render_range="loop",
                              start_bar=1, length_bars=1)
        assert out["render_range"] == "custom"
        assert (seen["request"].start_beats, seen["request"].length_beats) == (0.0, 4.0)

    def test_a_named_range_with_selection_still_reads_the_set(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), render_range="selection",
                              start_bar=3, length_bars=2)
        assert seen["request"].typed_range is True
        assert (seen["request"].start_beats, seen["request"].length_beats) == (8.0, 8.0)

    def test_the_time_signature_sets_what_a_bar_is(self, run):
        fake = abl(get_arrangement_info=with_transport(signature_numerator=6, signature_denominator=8))
        out, fake, seen = run(tools.export.export_audio, fake=fake, start_bar=2, length_bars=4)
        req = seen["request"]
        assert req.signature == (6, 8)
        assert (req.start_beats, req.length_beats) == (3.0, 12.0)

    def test_the_tempo_sets_the_expected_length(self, run):
        fake = abl(get_arrangement_info=with_transport(tempo=90.0))
        out, fake, seen = run(tools.export.export_audio, fake=fake, start_bar=1, length_bars=14)
        assert seen["request"].expected_seconds == pytest.approx(56 * 60 / 90.0)

    def test_a_range_read_from_the_set_is_widened_to_the_sixteenth_grid(self, run):
        info = {"transport": ARRANGEMENT["transport"],
                "tracks": [{"arrangement_clips": [{"end_time": 10.1}]}]}
        out, fake, seen = run(tools.export.export_audio, fake=abl(get_arrangement_info=info))
        assert (seen["request"].start_beats, seen["request"].length_beats) == (0.0, 10.25)
        assert seen["request"].expected_seconds == pytest.approx(10.25 * 0.5)
        assert out["range_rounded"] is True

    def test_a_loop_that_starts_off_the_grid_is_widened_both_ways(self, run):
        fake = abl(get_arrangement_info=with_transport(loop_start=16.1, loop_length=7.9))
        out, fake, seen = run(tools.export.export_audio, fake=fake, render_range="loop")
        assert (seen["request"].start_beats, seen["request"].length_beats) == (16.0, 8.0)
        assert out["range_rounded"] is True

    @pytest.mark.parametrize("kwargs,message", [
        ({"start_beat": 0.1, "length_beats": 4.0}, "start_beats 0.1 is not a whole number of sixteenth"),
        ({"start_beat": 0.0, "length_beats": 7.3}, "length_beats 7.3 is not a whole number of sixteenth"),
        ({"start_beat": -1.0, "length_beats": 4.0}, "start_beats must be >= 0"),
        ({"length_beats": -4.0}, "length_beats must be > 0"),
    ])
    def test_a_named_range_off_the_grid_or_negative_is_refused_before_the_export(self, run, kwargs, message):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), **kwargs)
        assert out["ok"] is False and message in out["error"]
        assert out["render_range"] == "custom" or "request" not in seen
        assert "request" not in seen

    @pytest.mark.parametrize("kwargs,message", [
        ({"start_bar": -1, "length_bars": 1}, "start_bar must be >= 1"),
        ({"start_bar": 1, "length_bars": -2}, "length_bars must be >= 1"),
    ])
    def test_negative_bars_are_refused(self, run, kwargs, message):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), **kwargs)
        assert out["ok"] is False and message in out["error"] and "request" not in seen

    def test_a_start_past_the_end_of_the_arrangement_with_no_length_is_refused(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), start_beat=100.0)
        assert out["ok"] is False and "nothing to render from beat 100" in out["error"]

    def test_a_loop_with_no_length_is_refused(self, run):
        fake = abl(get_arrangement_info=with_transport(loop_length=0.0))
        out, fake, seen = run(tools.export.export_audio, fake=fake, render_range="loop")
        assert out["ok"] is False and "loop brace has no length" in out["error"]

    def test_session_clips_overriding_stop_a_named_range_too(self, run):
        fake = abl(get_arrangement_info=with_transport(back_to_arranger=True))
        out, fake, seen = run(tools.export.export_audio, fake=fake, start_bar=1, length_bars=1)
        assert out["ok"] is False and "Session clips are overriding" in out["error"]

    def test_export_video_takes_the_same_parameters(self, run):
        out, fake, seen = run(tools.export.export_video, fake=abl(), start_bar=2, length_bars=3,
                              rendered_track="Guitar")
        req = seen["request"]
        assert req.create_video is True
        assert (req.start_beats, req.length_beats, req.rendered_track) == (4.0, 12.0, "Guitar")


class TestRenderedTrackParameter:
    def test_main_is_the_default_and_needs_no_track_list(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl())
        assert seen["request"].track_mode == "main"
        assert "list_tracks" not in [c for c, _ in fake.calls]

    def test_all_expects_one_file_per_track_plus_the_main_mix(self, run):
        # 3 tracks and a return, and the Main mix under the plain name
        out, fake, seen = run(tools.export.export_audio, fake=abl(), rendered_track="all")
        assert seen["request"].track_mode == "all"
        assert seen["request"].expected_files == 5

    def test_all_does_not_check_the_file_count_when_the_set_has_a_group_track(self, run):
        rows = TRACKS["tracks"] + [{"index": 5, "name": "Bus", "kind": "group"}]
        out, fake, seen = run(tools.export.export_audio, fake=abl(list_tracks={"tracks": rows}),
                              rendered_track="all")
        assert seen["request"].expected_files is None

    def test_all_with_no_tracks_listed_checks_nothing(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl(list_tracks={"tracks": []}),
                              rendered_track="all")
        assert seen["request"].expected_files is None

    def test_a_named_track_is_checked_against_the_set(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), rendered_track="Guitar")
        assert out["ok"] is True
        assert seen["request"].track_name == "Guitar"
        assert ("list_tracks", {"include_returns": True, "include_master": False}) in fake.calls

    def test_a_return_track_can_be_named(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), rendered_track="A Reverb")
        assert out["ok"] is True

    def test_a_track_that_does_not_exist_stops_the_export_before_live(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), rendered_track="Drums")
        assert out["ok"] is False and "there is no track named 'Drums'" in out["error"]
        assert "request" not in seen

    def test_two_tracks_with_one_name_stop_the_export(self, run):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), rendered_track="Bass")
        assert out["ok"] is False and "2 tracks are named 'Bass'" in out["error"]
        assert "request" not in seen

    @pytest.mark.parametrize("name,mode", [("Master", "main"), ("all individual tracks", "all")])
    def test_the_names_of_live_s_own_entries_mean_the_entries(self, run, name, mode):
        out, fake, seen = run(tools.export.export_audio, fake=abl(), rendered_track=name)
        assert out["ok"] is True and seen["request"].track_mode == mode
        assert (("list_tracks", {"include_returns": True, "include_master": False})
                in fake.calls) is (mode == "all")


class TestCompat:
    OLD_PARAMETERS = ["ctx", "output_path", "format", "sample_rate", "bit_depth", "render_range",
                      "normalize", "overwrite", "render_timeout"]

    @pytest.mark.parametrize("tool", ["export_audio", "export_video"])
    def test_the_old_parameters_keep_their_order_and_defaults(self, tool):
        params = list(inspect.signature(getattr(tools.export, tool)).parameters.values())
        names = [p.name for p in params]
        assert names[:9] == self.OLD_PARAMETERS
        defaults = {p.name: p.default for p in params}
        assert defaults["format"] == "wav" and defaults["sample_rate"] == 44100
        assert defaults["bit_depth"] == 24 and defaults["render_range"] == "arrangement"
        assert defaults["normalize"] is False and defaults["overwrite"] is False
        assert defaults["render_timeout"] == (900.0 if tool == "export_audio" else 1800.0)
        assert names[9:] == ["start_bar", "start_beat", "length_bars", "length_beats", "rendered_track"]
        assert defaults["rendered_track"] == "Main" and defaults["start_bar"] == 0

    DOCUMENTED = ["ok", "status", "output_path", "output_files", "expected_path", "format",
                  "sample_rate", "bit_depth", "normalize", "create_video", "render_range",
                  "expected_seconds", "rendered_seconds", "length_verified", "dialog_settings",
                  "file_info", "steps", "warnings", "export_pending", "selection_cleared",
                  "transport_unchanged"]

    def run_real(self, tmp_path, monkeypatch, *args, duration=3.0, **kwargs):
        monkeypatch.setattr(sys, "platform", "darwin")
        clock = FakeClock()
        text = afinfo_for(48000, WAV_24).replace("3.0 sec", f"{duration} sec")
        live, exporter = make(clock, afinfo=text)
        fake = GuardAbleton()
        with patch.object(connection, "get_ableton_connection", return_value=fake), \
                patch.object(le, "LiveExporter", return_value=exporter):
            out = json.loads(tools.export.export_audio(CTX, *args, **kwargs))
        return out, live, fake

    def test_a_call_made_the_old_way_works_and_keeps_every_documented_reply_field(
            self, tmp_path, monkeypatch):
        out, live, fake = self.run_real(
            tmp_path, monkeypatch, str(tmp_path / "mix.wav"), "wav", 48000, 24, "arrangement",
            False, False, 60.0)
        assert out["ok"] is True, out
        assert [k for k in self.DOCUMENTED if k not in out] == []
        assert out["render_range"] == "arrangement"
        assert out["output_path"] == str(tmp_path / "mix.wav")
        assert out["length_verified"] is True and out["rendered_seconds"] == 3.0
        # The Set's 6 beats were typed (1 bar and 2 beats from bar 1), the loop never touched.
        assert live.typed == [("Render Start", [1, 1, 1]), ("Render Length", [1, 2, 0])]
        assert [c for c, _ in fake.calls if c in ("set_arrangement_loop", "set_view")] == []
        assert out["dialog_range"]["length_beats"] == 6.0

    @pytest.mark.parametrize("render_range", ["arrangement", "loop", "selection"])
    def test_every_render_range_still_works(self, tmp_path, monkeypatch, render_range):
        # the Set's 6-beat arrangement is 3 s, its 8-beat loop 4 s, at 120 BPM
        out, live, fake = self.run_real(tmp_path, monkeypatch, output_path=str(tmp_path / "mix.wav"),
                                        sample_rate=48000, render_range=render_range,
                                        duration=4.0 if render_range == "loop" else 3.0)
        assert out["ok"] is True, out
        assert out["render_range"] == render_range
        assert (live.typed == []) is (render_range == "selection")

    def test_an_all_tracks_call_returns_one_file_per_track_with_durations(self, tmp_path, monkeypatch):
        out, live, fake = self.run_real(tmp_path, monkeypatch, output_path=str(tmp_path / "mix.wav"),
                                        sample_rate=48000, rendered_track="all")
        assert out["ok"] is True, out
        assert len(out["files"]) == len(out["output_files"]) == 3   # two tracks and the Main mix
        assert all(f["duration_seconds"] == 3.0 and f["sample_rate"] == 48000 for f in out["files"])

    def test_the_documented_old_loop_restore_fields_are_gone(self, tmp_path, monkeypatch):
        out, live, fake = self.run_real(tmp_path, monkeypatch, output_path=str(tmp_path / "mix.wav"),
                                        sample_rate=48000)
        assert "loop_to_restore" not in out
