"""Issue 11: the export types its render range, picks the rendered track, waits out the render.

One class per step of the issue's Proposal, run against the FakeLive of
test_live_export.py (no osascript, no screen):

1. the Export dialog opens without touching the arrangement selection or the loop;
2. Rendered Track is chosen (Main, a named track, or All Individual Tracks) and read back;
3. Render Start and Render Length are typed into the dialog and read back before Export;
4. the Save panel is filled, then the tool waits until the render window has closed and
   every output file has stopped growing;
5. only then does it return, with paths, durations and sample rate, and while a render is
   pending it restores and changes nothing.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from MCP_Server import live_export as le  # noqa: E402
from tests.unit.test_live_export import (  # noqa: E402
    WAV_24, FakeClock, afinfo_for, make, request)

CHANGES_LIVE = ("menu", "keystroke", "press", "set_toggle", "set_choice", "set_time_field",
                "save_panel", "cancel", "stop_transport")


@pytest.fixture
def clock():
    return FakeClock()


def typed_request(tmp_path, start=0.0, length=56.0, **kw):
    return request(tmp_path, start_beats=start, length_beats=length, **kw)


# ── position fields ──────────────────────────────────────────────


@pytest.mark.parametrize("beats,signature,one_based,fields", [
    (0.0, (4, 4), True, (1, 1, 1)),
    (56.0, (4, 4), False, (14, 0, 0)),
    (2.25, (4, 4), True, (1, 3, 2)),
    (16.0, (4, 4), True, (5, 1, 1)),
    (0.25, (4, 4), False, (0, 0, 1)),
    (3.0, (3, 4), False, (1, 0, 0)),
    (3.0, (6, 8), False, (1, 0, 0)),
    (0.5, (6, 8), False, (0, 1, 0)),
])
def test_beats_to_fields_and_back(beats, signature, one_based, fields):
    assert le.beats_to_fields(beats, signature, one_based) == fields
    assert le.fields_to_beats(fields, signature, one_based) == beats
    assert le.format_fields(fields) == ".".join(str(v) for v in fields)


# ── step 1: open the dialog without touching selection or loop ───


class TestStep1OpenDialogUntouched:
    def test_only_the_export_menu_item_is_clicked_for_a_typed_range(self, tmp_path, clock):
        live, exporter = make(clock)
        result = exporter.run(typed_request(tmp_path))
        assert result["ok"] is True, result
        assert [m["path"] for m in live.driven("menu")] == [["File", "Export Audio/Video"]]
        assert not getattr(live, "unexpected_menu", [])

    def test_no_select_loop_navigate_or_playback_menu_is_ever_used(self, tmp_path, clock):
        live, exporter = make(clock)
        exporter.run(typed_request(tmp_path, start=16.0, length=8.0))
        used = [tuple(m["path"]) for m in live.driven("menu")]
        assert all(path[0] == "File" for path in used)
        assert ("Edit", "Select Loop") not in used

    def test_loop_and_time_selection_read_back_the_same_afterwards(self, tmp_path, clock):
        live, exporter = make(clock, loop_on=True)
        before = (live.loop_on, live.time_selection, live.playing)
        result = exporter.run(typed_request(tmp_path))
        assert result["ok"] is True, result
        assert (live.loop_on, live.time_selection, live.playing) == before
        assert result["selection_cleared"] is True and result["transport_unchanged"] is True
        assert result["time_selection_after"] is False

    def test_the_guard_still_runs_before_any_click(self, tmp_path, clock):
        live, exporter = make(clock, key_map=True)
        result = exporter.run(typed_request(tmp_path))
        assert result["ok"] is False and "Nothing was clicked" in result["error"]
        assert live.driven(*CHANGES_LIVE) == []

    def test_the_request_has_no_select_loop_switch(self, tmp_path):
        assert not hasattr(request(tmp_path), "select_loop")


# ── step 2: Rendered Track ───────────────────────────────────────


class TestStep2RenderedTrack:
    def choice(self, live):
        return live.controls[le.RENDERED_TRACK]["value"]

    def test_main_is_the_first_entry(self, tmp_path, clock):
        live, exporter = make(clock)
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True, result
        assert self.choice(live) == "Main"
        assert result["rendered_track"] == "Main"
        assert result["dialog_settings"]["Rendered Track Chooser"] == "Main"

    def test_a_named_track_is_picked_by_name_and_read_back(self, tmp_path, clock):
        live, exporter = make(clock)
        result = exporter.run(request(tmp_path, rendered_track="Bass"))
        assert result["ok"] is True, result
        assert self.choice(live) == "Bass"
        assert result["rendered_track"] == "Bass"
        assert result["dialog_settings"]["Rendered Track Chooser"] == "Bass"
        step = next(s for s in result["steps"] if s.get("label") == le.RENDERED_TRACK)
        assert step["values"] == ["Bass"] and step["after"] == "Bass"

    @pytest.mark.parametrize("spelling", ["all", "ALL", "All Individual Tracks"])
    def test_all_picks_all_individual_tracks(self, tmp_path, clock, spelling):
        live, exporter = make(clock)
        result = exporter.run(request(tmp_path, name="mix", rendered_track=spelling))
        assert result["ok"] is True, result
        assert self.choice(live) == "All Individual Tracks"
        assert result["rendered_track"] == "all"

    def test_the_choice_follows_the_current_one_as_the_list_rows_shift(self, tmp_path, clock):
        # Live lays its chooser list out around the current choice, so the row
        # of a given entry moves. The tool never relies on a row: each choice is
        # made by name (or Live's bus by being first) and read back, whatever
        # the chooser showed before.
        live, exporter = make(clock)
        for i, (wanted, shown) in enumerate([
                ("Bass", "Bass"), ("all", "All Individual Tracks"), ("Main", "Main"),
                ("Guitar", "Guitar"), ("Main", "Main")]):
            result = exporter.run(request(tmp_path, name=f"mix{i}", rendered_track=wanted,
                                          overwrite=True))
            assert result["ok"] is True, (wanted, result)
            assert self.choice(live) == shown
            live.windows = [w for w in live.windows if w["name"] == "My Song"]

    def test_a_chooser_that_does_not_take_the_choice_fails_before_export(self, tmp_path, clock):
        live, exporter = make(clock, stuck_choices=(le.RENDERED_TRACK,))
        result = exporter.run(request(tmp_path, rendered_track="Bass"))
        assert result["ok"] is False
        assert "could not set 'Rendered Track Chooser'" in result["error"]
        assert [c["label"] for c in live.driven("press")
                if c["label"] == le.EXPORT_BUTTON] == []

    def test_a_track_that_is_not_in_the_list_fails_before_export(self, tmp_path, clock):
        live, exporter = make(clock)
        result = exporter.run(request(tmp_path, rendered_track="No Such Track"))
        assert result["ok"] is False
        assert "No Such Track" in result["error"]
        assert "choices seen" in result["error"]
        assert live.driven("save_panel") == []
        assert self.choice(live) != "No Such Track"

    @pytest.mark.parametrize("given,mode,name", [
        ("Main", "main", ""), ("master", "main", ""), (" Bass ", "track", "Bass"),
        ("all", "all", ""), ("All Individual Tracks", "all", ""),
    ])
    def test_rendered_track_spellings(self, tmp_path, given, mode, name):
        req = request(tmp_path, rendered_track=given).validate()
        assert (req.track_mode, req.track_name) == (mode, name)

    def test_an_empty_rendered_track_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="rendered_track must be"):
            request(tmp_path, rendered_track="  ").validate()


# ── step 3: type Render Start and Render Length ──────────────────


class TestStep3TypedRange:
    def test_start_and_length_are_typed_then_read_back_before_export(self, tmp_path, clock):
        live, exporter = make(clock)
        result = exporter.run(typed_request(tmp_path, start=16.0, length=56.0))
        assert result["ok"] is True, result
        assert live.typed == [(le.RENDER_START, [5, 1, 1]), (le.RENDER_LENGTH, [14, 0, 0])]
        assert live.fields == {le.RENDER_START: [5, 1, 1], le.RENDER_LENGTH: [14, 0, 0]}
        ops = live.ops()
        last_typed = max(i for i, op in enumerate(ops) if op == "set_time_field")
        reads = [i for i, op in enumerate(ops) if op == "get_time_field" and i > last_typed]
        export = next(i for i, c in enumerate(live.calls)
                      if c["op"] == "press" and c["label"] == le.EXPORT_BUTTON)
        assert len(reads) == 2 and max(reads) < export
        assert result["dialog_range"] == {"start": "5.1.1", "length": "14.0.0",
                                          "start_beats": 16.0, "length_beats": 56.0}

    def test_the_range_is_typed_after_the_other_dialog_settings(self, tmp_path, clock):
        live, exporter = make(clock)
        exporter.run(typed_request(tmp_path))
        ops = live.ops()
        assert max(i for i, op in enumerate(ops) if op in ("set_choice", "set_toggle")) \
            < min(i for i, op in enumerate(ops) if op == "set_time_field")

    def test_a_field_that_does_not_take_its_value_fails_before_export(self, tmp_path, clock):
        live, exporter = make(clock, stuck_fields=(le.RENDER_LENGTH,))
        result = exporter.run(typed_request(tmp_path))
        assert result["ok"] is False
        assert "'Render Length' reads 4.0.0 after typing 14.0.0" in result["error"]
        assert live.driven("save_panel") == []
        assert [c for c in live.driven("press") if c["label"] == le.EXPORT_BUTTON] == []
        assert result["export_pending"] is False and le.EXPORT_DIALOG_TITLE not in live.names()

    def test_a_start_moved_by_typing_the_length_is_caught_by_reading_both_after_typing(
            self, tmp_path, clock):
        live, exporter = make(clock, start_follows_length=True)
        result = exporter.run(typed_request(tmp_path, start=16.0))
        assert result["ok"] is False
        assert "'Render Start' reads 1.1.1 after typing 5.1.1" in result["error"]

    def test_fields_the_driver_cannot_find_fail_before_export(self, tmp_path, clock):
        live, exporter = make(clock, fields_unreadable=True)
        result = exporter.run(typed_request(tmp_path))
        assert result["ok"] is False
        assert "could not type 'Render Start'" in result["error"]
        assert live.driven("save_panel") == []

    @pytest.mark.parametrize("start,length,name", [
        (0.1, 8.0, "start_beats"), (0.0, 7.3, "length_beats"), (1 / 3, 4.0, "start_beats"),
    ])
    def test_a_range_off_the_sixteenth_grid_is_refused_not_rounded(self, tmp_path, start, length, name):
        with pytest.raises(ValueError, match=f"{name} .* not a whole number of sixteenth notes"):
            typed_request(tmp_path, start=start, length=length).validate()

    @pytest.mark.parametrize("start,length,message", [
        (-1.0, 4.0, "start_beats must be >= 0"), (0.0, 0.0, "length_beats must be > 0"),
    ])
    def test_a_range_must_be_positive(self, tmp_path, start, length, message):
        with pytest.raises(ValueError, match=message):
            typed_request(tmp_path, start=start, length=length).validate()

    def test_start_and_length_come_together(self, tmp_path):
        with pytest.raises(ValueError, match="given together"):
            request(tmp_path, start_beats=4.0).validate()

    def test_a_six_eight_set_is_typed_in_its_own_bars(self, tmp_path, clock):
        live, exporter = make(clock)
        result = exporter.run(typed_request(tmp_path, start=3.0, length=12.0, signature=(6, 8)))
        assert result["ok"] is True, result
        assert live.typed == [(le.RENDER_START, [2, 1, 1]), (le.RENDER_LENGTH, [4, 0, 0])]

    def test_with_no_range_asked_for_the_dialog_is_left_alone_and_reported(self, tmp_path, clock):
        live, exporter = make(clock)
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True, result
        assert live.typed == []
        assert result["dialog_range"]["start"] == "1.1.1" and result["dialog_range"]["length"] == "4.0.0"

    def test_an_unreadable_prefilled_range_is_a_warning_only(self, tmp_path, clock):
        live, exporter = make(clock, fields_unreadable=True)
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True, result
        assert "dialog_range" not in result
        assert any("render range was not read" in w for w in result["warnings"])

    @pytest.mark.parametrize("bad", ["fast", [1, 2], None])
    def test_a_field_reply_that_is_not_three_numbers_is_an_error(self, tmp_path, clock, bad):
        live, exporter = make(clock)
        original = live.op_get_time_field
        live.op_get_time_field = lambda window, label: dict(original(window, label), fields=bad)
        result = exporter.run(typed_request(tmp_path))
        assert result["ok"] is False and "could not read 'Render Start'" in result["error"]

    def test_non_numeric_field_parts_are_an_error(self, tmp_path, clock):
        live, exporter = make(clock)
        live.op_get_time_field = lambda window, label: {"ok": True, "fields": ["a", "b", "c"]}
        result = exporter.run(typed_request(tmp_path))
        assert result["ok"] is False and "could not read" in result["error"]

    def test_unsupported_signature(self, tmp_path):
        with pytest.raises(ValueError, match="unsupported time signature"):
            typed_request(tmp_path, signature=(4, 3)).validate()


# ── step 4: Save panel, then wait out the render ─────────────────


class TestStep4SaveAndWait:
    def test_the_save_panel_gets_the_folder_and_name_after_export(self, tmp_path, clock):
        live, exporter = make(clock)
        result = exporter.run(typed_request(tmp_path, name="take", overwrite=True))
        assert result["ok"] is True, result
        save = live.driven("save_panel")[0]
        assert (save["directory"], save["filename"], save["replace"]) == (str(tmp_path), "take", True)
        assert live.ops().index("save_panel") > max(
            i for i, c in enumerate(live.calls) if c["op"] == "press")

    def test_it_waits_until_the_files_stop_growing_and_the_render_window_is_gone(
            self, tmp_path, clock):
        live, exporter = make(clock, grow_polls=4, render_polls=2)
        result = exporter.run(typed_request(tmp_path))
        assert result["ok"] is True, result
        assert live.render_complete is True
        assert [n for n in live.names() if n in ("", le.EXPORT_DIALOG_TITLE)] == []
        final = os.path.getsize(result["output_path"])
        assert result["files"][0]["size_bytes"] == final
        assert final == len(live.render_bytes)

    def test_a_render_that_never_closes_its_window_times_out_pending(self, tmp_path, clock):
        live, exporter = make(clock, window_stays=True)
        result = exporter.run(typed_request(tmp_path, render_timeout=8))
        assert result["ok"] is False and result["export_pending"] is True
        assert "render did not finish within 8s" in result["error"]
        assert os.path.exists(tmp_path / "song.wav")

    def test_a_render_that_writes_nothing_times_out(self, tmp_path, clock):
        live, exporter = make(clock, never_writes=True)
        result = exporter.run(typed_request(tmp_path, render_timeout=5))
        assert result["ok"] is False and "no output file was produced within 5s" in result["error"]

    def test_all_individual_tracks_waits_for_every_file(self, tmp_path, clock):
        live, exporter = make(clock, grow_polls=3, render_polls=1,
                              afinfo=afinfo_for(48000, WAV_24))
        result = exporter.run(request(tmp_path, name="mix", rendered_track="all",
                                      expected_seconds=3.0, expected_files=2))
        assert result["ok"] is True, result
        assert live.render_complete is True
        assert sorted(os.path.basename(p) for p in result["output_files"]) == [
            "mix Bass.wav", "mix Guitar.wav"]
        assert [f["duration_seconds"] for f in result["files"]] == [3.0, 3.0]
        assert result["length_verified"] is True
        assert result["output_path"] == result["output_files"][0]
        assert all(f["size_bytes"] == len(live.render_bytes) for f in result["files"])

    def test_all_individual_tracks_with_the_wrong_file_count_fails(self, tmp_path, clock):
        live, exporter = make(clock, afinfo=afinfo_for(48000, WAV_24))
        result = exporter.run(request(tmp_path, name="mix", rendered_track="all", expected_files=5))
        assert result["ok"] is False
        assert "wrote 2 file(s)" in result["error"] and "5 were expected" in result["error"]

    def test_old_files_in_the_folder_are_not_counted_as_output(self, tmp_path, clock):
        old = tmp_path / "mix Old.wav"
        old.write_bytes(b"x" * 10)
        os.utime(old, (1.0, 1.0))
        (tmp_path / "other Bass.wav").write_bytes(b"y")
        live, exporter = make(clock, afinfo=afinfo_for(48000, WAV_24))
        result = exporter.run(request(tmp_path, name="mix", rendered_track="all"))
        assert result["ok"] is True, result
        assert [os.path.basename(p) for p in result["output_files"]] == [
            "mix Bass.wav", "mix Guitar.wav"]


# ── step 5: return only when done; change nothing while pending ──


class TestStep5ReturnAndPending:
    def test_the_reply_lists_paths_durations_and_sample_rate(self, tmp_path, clock):
        live, exporter = make(clock, afinfo=afinfo_for(48000, WAV_24))
        result = exporter.run(typed_request(tmp_path, expected_seconds=3.0))
        assert result["ok"] is True, result
        path = str(tmp_path / "song.wav")
        assert result["output_files"] == [path] and result["output_path"] == path
        assert result["files"] == [{"path": path, "size_bytes": len(live.render_bytes),
                                    "duration_seconds": 3.0, "sample_rate": 48000,
                                    "length_verified": True, "bit_depth": 24, "channels": 2}]
        assert result["rendered_seconds"] == 3.0 and result["length_verified"] is True

    def test_it_does_not_return_before_the_render_is_complete(self, tmp_path, clock):
        live, exporter = make(clock, grow_polls=5, render_polls=3)
        result = exporter.run(typed_request(tmp_path))
        assert result["ok"] is True and live.render_complete is True

    def test_a_file_of_the_wrong_length_is_a_failure_not_a_success(self, tmp_path, clock):
        live, exporter = make(clock, afinfo=afinfo_for(48000, WAV_24))
        result = exporter.run(typed_request(tmp_path, expected_seconds=28.0))
        assert result["ok"] is False and "it is 3.00 s long, expected 28.00 s" in result["error"]

    def test_nothing_is_changed_or_restored_while_a_render_is_pending(self, tmp_path, clock):
        live, exporter = make(clock, window_stays=True, time_selection=True, loop_on=True)
        result = exporter.run(typed_request(tmp_path, render_timeout=6))
        assert result["export_pending"] is True and result["ok"] is False
        timed_out = max(i for i, c in enumerate(live.calls) if c["op"] == "status")
        # After the render wait gave up, the only thing sent to Live is the
        # cancel of its own export windows: no menu, no selection clearing, no
        # Stop, no typing.
        after = [c for c in live.calls[timed_out:] if c["op"] in CHANGES_LIVE]
        assert {c["op"] for c in after} <= {"cancel"}
        assert [m for m in live.driven("menu") if m["path"][0] != "File"] == []
        assert live.driven("stop_transport") == []
        assert live.time_selection is True and live.loop_on is True
        assert result["selection_cleared"] is False and result["transport_unchanged"] is None
        assert any("not safe to touch while the export is pending" in w for w in result["warnings"])

    def test_the_next_export_is_refused_until_live_is_clear_and_clicks_nothing(self, tmp_path, clock):
        live, exporter = make(clock, window_stays=True)
        first = exporter.run(typed_request(tmp_path, render_timeout=6))
        assert first["export_pending"] is True
        driven = len(live.driven(*CHANGES_LIVE))
        again = exporter.run(typed_request(tmp_path, name="again.wav"))
        assert again["ok"] is False and "an earlier export did not finish cleanly" in again["error"]
        assert len(live.driven(*CHANGES_LIVE)) == driven
        le._pending_baseline = None

    def test_a_failed_export_before_the_save_panel_cancels_its_dialog_and_changes_nothing_else(
            self, tmp_path, clock):
        live, exporter = make(clock, stuck_fields=(le.RENDER_START,), loop_on=True)
        result = exporter.run(typed_request(tmp_path, start=4.0))
        assert result["ok"] is False and result["export_pending"] is False
        assert live.loop_on is True
        assert [m["path"] for m in live.driven("menu")] == [["File", "Export Audio/Video"]]


# ── edges ────────────────────────────────────────────────────────


class TestEdges:
    def test_start_and_length_must_be_numbers(self, tmp_path):
        with pytest.raises(ValueError, match="must be numbers"):
            request(tmp_path, start_beats="soon", length_beats=4.0).validate()

    def test_a_ui_read_that_raises_after_the_export_is_a_warning_not_a_crash(self, tmp_path, clock):
        live, exporter = make(clock)
        original = live.op_ui_state
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("System Events went away")
            return original()

        live.op_ui_state = flaky
        result = exporter.run(typed_request(tmp_path))
        assert result["ok"] is True
        assert result["selection_cleared"] is False and result["transport_unchanged"] is None
        assert any("could not read Live's UI after the export" in w for w in result["warnings"])

    def test_a_file_that_vanishes_between_listing_and_stat_is_skipped(self, tmp_path, clock):
        os.symlink(tmp_path / "gone.wav", tmp_path / "mix Ghost.wav")
        live, exporter = make(clock)
        req = request(tmp_path, name="mix", rendered_track="all").validate()
        assert exporter._output_files(req, 0.0) == []

    def test_the_size_of_a_missing_file_is_zero(self, tmp_path):
        assert le._size(str(tmp_path / "nope.wav")) == 0
