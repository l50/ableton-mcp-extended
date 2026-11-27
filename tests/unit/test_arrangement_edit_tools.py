"""Unit tests for the MCP arrangement tools for the loop, exact clip beats,
arrangement clip notes, and trimming and splitting arrangement clips.

The connection is a fake that records what each tool sends and replays a
scripted reply, so the tests cover the wire payload and the text returned.
"""
import os
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

from MCP_Server.tools import arrangement  # noqa: E402

SIG_4_4 = {"signature_numerator": 4, "signature_denominator": 4}


class FakeAbleton:
    def __init__(self, replies=None):
        self.replies = dict(replies or {})
        self.calls = []

    def send_command(self, command_type, params=None):
        self.calls.append((command_type, params))
        reply = self.replies.get(command_type, {})
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def sent(self, command_type):
        return [p for c, p in self.calls if c == command_type]


def run(tool, replies=None, **kwargs):
    fake = FakeAbleton(replies)
    with patch("MCP_Server.connection.get_ableton_connection", return_value=fake):
        return tool(MagicMock(), **kwargs), fake


# ── #13 ──────────────────────────────────────────────────────────────


class TestSetArrangementLoopTool:
    def test_start_beat_zero_is_sent(self):
        replies = {"get_session_info": SIG_4_4,
                   "set_arrangement_loop": {"enabled": True, "start": 0.0, "length": 16.0}}
        out, fake = run(arrangement.set_arrangement_loop, replies, start_beat=0.0,
                        length_beats=16.0)
        assert fake.sent("set_arrangement_loop") == [{"enabled": True, "start": 0.0,
                                                      "length": 16.0}]
        assert out == "Loop enabled: bars 1-5 (start beat 0, length 16 beats)"

    def test_omitted_start_and_length_are_not_sent(self):
        replies = {"get_session_info": SIG_4_4,
                   "set_arrangement_loop": {"enabled": False, "start": 104.0, "length": 8.0,
                                            "requested": {"enabled": False, "start": 104.0,
                                                          "length": 8.0}}}
        out, fake = run(arrangement.set_arrangement_loop, replies, enabled=False)
        assert fake.sent("set_arrangement_loop") == [{"enabled": False}]
        assert out == "Loop disabled: bars 27-29 (start beat 104, length 8 beats)"

    def test_reply_reports_live_state_and_warns_on_difference(self):
        replies = {"get_session_info": SIG_4_4,
                   "set_arrangement_loop": {"enabled": True, "start": 8.0, "length": 12.0,
                                            "requested": {"enabled": True, "start": 8.0,
                                                          "length": 64.0}}}
        out, _ = run(arrangement.set_arrangement_loop, replies, start_beat=8.0,
                     length_beats=64.0)
        assert out.splitlines() == [
            "Loop enabled: bars 3-6 (start beat 8, length 12 beats)",
            "Warning: Live shows the loop above, but length 64 was requested"]

    def test_state_difference_is_warned(self):
        replies = {"get_session_info": SIG_4_4,
                   "set_arrangement_loop": {"enabled": False, "start": 0.0, "length": 4.0,
                                            "requested": {"enabled": True, "start": 0.0,
                                                          "length": 4.0}}}
        out, _ = run(arrangement.set_arrangement_loop, replies)
        assert out.endswith("but loop on was requested")

    @pytest.mark.parametrize("kwargs,message", [
        ({"start_beat": -1.0}, "start_beat must be >= 0"),
        ({"length_beats": 0.0}, "length_beats must be > 0"),
    ])
    def test_bad_values_are_refused(self, kwargs, message):
        out, fake = run(arrangement.set_arrangement_loop, {"get_session_info": SIG_4_4},
                        **kwargs)
        assert message in out
        assert fake.sent("set_arrangement_loop") == []


# ── #24 ──────────────────────────────────────────────────────────────


def _info(clips, transport=None, is_midi=True):
    return {
        "transport": dict({"tempo": 120.0, "is_playing": False, "current_time": 0.0,
                           "loop_enabled": True, "loop_start": 312.0, "loop_length": 16.0},
                          **SIG_4_4, **(transport or {})),
        "tracks": [{"index": 0, "name": "Bass", "is_midi": is_midi,
                    "arrangement_clips": clips}],
    }


class TestArrangementInfoBeats:
    def test_clip_at_312_to_328_shows_exact_beats(self):
        clip = {"index": 0, "name": "Riff", "start_time": 312.0, "end_time": 328.0,
                "muted": False, "looping": True, "loop_start": 0.0, "loop_end": 4.0,
                "start_marker": 1.5, "end_marker": 4.0}
        out, _ = run(arrangement.get_arrangement_info, {"get_arrangement_info": _info([clip])})
        lines = out.splitlines()
        assert '  1. "Riff" bars 79-83' in lines
        assert ("     beats 312-328 (length 16) | looping: yes, loop 0-4 | markers 1.5-4 | "
                "muted: no") in lines

    def test_fractional_beats_and_mute_are_exact(self):
        clip = {"index": 1, "name": "Fill", "start_time": 312.125, "end_time": 313.0 + 1 / 3,
                "muted": True, "looping": False, "loop_start": 0.25, "loop_end": 1.458333,
                "start_marker": 0.25, "end_marker": 1.458333}
        out, _ = run(arrangement.get_arrangement_info, {"get_arrangement_info": _info([clip])})
        assert ("beats 312.125-313.333333 (length 1.208333) | looping: no, loop 0.25-1.458333"
                " | markers 0.25-1.458333 | muted: yes") in out
        assert '2. "Fill" bars 79-79 [MUTED]' in out

    def test_unwarped_audio_marks_seconds(self):
        clip = {"index": 0, "name": "Vox", "start_time": 0.0, "end_time": 4.0, "is_audio": True,
                "warping": False, "file_path": "/S/Vox.wav", "looping": False,
                "loop_start": 60.0, "loop_end": 62.0, "start_marker": 60.0, "end_marker": 62.0}
        out, _ = run(arrangement.get_arrangement_info,
                     {"get_arrangement_info": _info([clip], is_midi=False)})
        assert "markers 60-62 | muted: no (unwarped: loop and markers in seconds" in out

    @pytest.mark.parametrize("enabled,state", [(True, "on"), (False, "off")])
    def test_transport_loop_shows_on_off_start_and_length_in_beats(self, enabled, state):
        out, _ = run(arrangement.get_arrangement_info,
                     {"get_arrangement_info": _info([], {"loop_enabled": enabled,
                                                         "loop_start": 0.0,
                                                         "loop_length": 6.5})})
        assert f"Loop: bars 1-2 | {state} | start beat 0, length 6.5 beats" in out

    def test_missing_values_print_as_unknown(self):
        clip = {"index": 0, "name": "Old", "start_time": 0.0, "end_time": 4.0}
        out, _ = run(arrangement.get_arrangement_info, {"get_arrangement_info": _info([clip])})
        assert "beats 0-4 (length 4) | looping: no, loop ?-? | markers ?-? | muted: no" in out


# ── #22 ──────────────────────────────────────────────────────────────

NOTES_REPLY = {
    "track": "Bass",
    "clip": {"index": 0, "name": "Riff", "start_time": 312.0, "end_time": 328.0,
             "looping": False, "loop_start": 0.0, "loop_end": 16.0, "start_marker": 0.0,
             "end_marker": 16.0, "muted": False},
    "range_start": 0.0, "range_end": 16.0, "note_count": 2,
    "notes": [
        {"pitch": 36, "start_time": 0.0, "duration": 1.0, "velocity": 100, "mute": False,
         "song_time": 312.0},
        {"pitch": 40, "start_time": 20.0, "duration": 0.25, "velocity": 64.5, "mute": True,
         "song_time": None},
    ],
}


class TestArrangementNoteTools:
    def test_add_notes_sends_zero_based_indices(self):
        notes = [{"pitch": 36, "start_time": 0.0, "duration": 1.0, "velocity": 100,
                  "mute": False}]
        out, fake = run(arrangement.add_notes_to_arrangement_clip,
                        {"add_notes_to_arrangement_clip": {"note_count": 1}},
                        track_index=2, clip_index=3, notes=notes)
        assert fake.calls == [("add_notes_to_arrangement_clip",
                               {"track_index": 1, "clip_index": 2, "notes": notes})]
        assert out == "Added 1 note(s) to arrangement clip 3 on track 2"

    def test_get_notes_prints_each_note(self):
        out, fake = run(arrangement.get_arrangement_clip_notes,
                        {"get_arrangement_clip_notes": NOTES_REPLY},
                        track_index=1, clip_name="Riff", start=0.0, end=32.0)
        assert fake.calls == [("get_arrangement_clip_notes", {
            "track_index": 0, "clip_name": "Riff", "clip_index": -1, "start": 0.0,
            "end": 32.0})]
        assert out.splitlines() == [
            "2 note(s) in 'Riff' on 'Bass', clip beats 0-16",
            'Clip: #1 "Riff" beats 312-328 (length 16) | looping: no, loop 0-16 | '
            "markers 0-16 | muted: no",
            "  pitch 36 | start 0 (song beat 312) | duration 1 | velocity 100 | muted: no",
            "  pitch 40 | start 20 (not played) | duration 0.25 | velocity 64.5 | muted: yes",
        ]

    def test_get_notes_by_index_with_default_range(self):
        _, fake = run(arrangement.get_arrangement_clip_notes,
                      {"get_arrangement_clip_notes": NOTES_REPLY}, track_index=1, clip_index=2)
        assert fake.sent("get_arrangement_clip_notes") == [
            {"track_index": 0, "clip_index": 1, "start": None, "end": None}]

    def test_remove_notes_reports_counts(self):
        reply = {"track": "Bass", "clip": {"name": "Riff"}, "range_start": 4.0,
                 "range_end": 8.0, "removed": 3, "remaining": 5}
        out, fake = run(arrangement.remove_arrangement_clip_notes,
                        {"remove_arrangement_clip_notes": reply},
                        track_index=1, clip_index=1, start=4.0, end=8.0, pitch=36)
        assert fake.calls == [("remove_arrangement_clip_notes", {
            "track_index": 0, "clip_index": 0, "start": 4.0, "end": 8.0, "pitch": 36})]
        assert out == ("Removed 3 pitch 36 note(s) starting in clip beats 4-8 of 'Riff' on "
                       "'Bass'; 5 note(s) remain in the clip")

    def test_live_error_is_returned_as_text(self):
        out, _ = run(arrangement.get_arrangement_clip_notes,
                     {"get_arrangement_clip_notes": RuntimeError("Clip 'X' is an audio clip")},
                     track_index=1, clip_index=1)
        assert out == "Error reading arrangement clip notes: Clip 'X' is an audio clip"

    def test_clip_target_is_required(self):
        out, fake = run(arrangement.remove_arrangement_clip_notes, track_index=1)
        assert out.startswith("Error removing arrangement clip notes: clip_index must be >= 1")
        assert fake.calls == []


# ── #23 ──────────────────────────────────────────────────────────────


def _clip(index, start, end, **extra):
    return dict({"index": index, "name": "Riff", "start_time": start, "end_time": end,
                 "looping": True, "loop_start": 0.0, "loop_end": 4.0, "start_marker": 0.0,
                 "end_marker": 4.0, "muted": False}, **extra)


class TestTrimSplitTools:
    def test_bounds_sends_markers_and_prints_result(self):
        reply = {"track": "Bass", "clip": _clip(0, 0.0, 16.0, start_marker=1.0,
                                                end_marker=3.0),
                 "previous": {"start_marker": 0.0, "end_marker": 4.0}}
        out, fake = run(arrangement.set_arrangement_clip_bounds,
                        {"set_arrangement_clip_bounds": reply},
                        track_index=1, clip_index=1, start_marker=1.0, end_marker=3.0)
        assert fake.calls == [("set_arrangement_clip_bounds", {
            "track_index": 0, "clip_index": 0, "start_marker": 1.0, "end_marker": 3.0})]
        assert out.splitlines()[:2] == [
            "Set markers of 'Riff' on 'Bass' (were 0-4)",
            'Now: #1 "Riff" beats 0-16 (length 16) | looping: yes, loop 0-4 | markers 1-3 | '
            "muted: no"]

    def test_bounds_omitted_marker_is_sent_as_none(self):
        _, fake = run(arrangement.set_arrangement_clip_bounds,
                      {"set_arrangement_clip_bounds": {"clip": _clip(0, 0.0, 4.0)}},
                      track_index=1, clip_name="Riff", end_marker=2.0)
        assert fake.sent("set_arrangement_clip_bounds") == [
            {"track_index": 0, "clip_name": "Riff", "clip_index": -1, "start_marker": None,
             "end_marker": 2.0}]

    def test_end_sends_song_beat_and_prints_result(self):
        reply = {"track": "Bass", "previous_end_time": 16.0, "method": "split",
                 "clip": _clip(0, 0.0, 10.0)}
        out, fake = run(arrangement.set_arrangement_clip_end,
                        {"set_arrangement_clip_end": reply},
                        track_index=1, clip_index=1, end_beat=10.0)
        assert fake.calls == [("set_arrangement_clip_end",
                               {"track_index": 0, "clip_index": 0, "end_beat": 10.0})]
        assert out.splitlines()[:2] == [
            "Moved the end of 'Riff' on 'Bass' from beat 16 to 10",
            'Now: #1 "Riff" beats 0-10 (length 10) | looping: yes, loop 0-4 | markers 0-4 | '
            "muted: no"]

    def test_end_unchanged(self):
        reply = {"track": "Bass", "previous_end_time": 16.0, "method": "unchanged",
                 "clip": _clip(0, 0.0, 16.0)}
        out, _ = run(arrangement.set_arrangement_clip_end, {"set_arrangement_clip_end": reply},
                     track_index=1, clip_index=1, end_beat=16.0)
        assert out == "'Riff' on 'Bass' already ends at beat 16"

    def test_split_sends_beat_and_prints_both_pieces(self):
        reply = {"track": "Bass", "split_at": 320.0,
                 "original": {"start_time": 312.0, "end_time": 328.0},
                 "left": _clip(0, 312.0, 320.0, looping=False, loop_end=8.0, end_marker=8.0),
                 "right": _clip(1, 320.0, 328.0, looping=False, loop_start=8.0, loop_end=16.0,
                                start_marker=8.0, end_marker=16.0)}
        out, fake = run(arrangement.split_arrangement_clip, {"split_arrangement_clip": reply},
                        track_index=1, clip_name="Riff", beat=320.0)
        assert fake.calls == [("split_arrangement_clip", {
            "track_index": 0, "clip_name": "Riff", "clip_index": -1, "beat": 320.0})]
        assert out.splitlines()[:3] == [
            "Split 'Riff' on 'Bass' (beats 312-328) at beat 320",
            'Left: #1 "Riff" beats 312-320 (length 8) | looping: no, loop 0-8 | markers 0-8 | '
            "muted: no",
            'Right: #2 "Riff" beats 320-328 (length 8) | looping: no, loop 8-16 | markers 8-16 '
            "| muted: no"]

    def test_split_error_is_returned_as_text(self):
        out, _ = run(arrangement.split_arrangement_clip,
                     {"split_arrangement_clip": RuntimeError("Beat 3 is not inside clip")},
                     track_index=1, clip_index=1, beat=3.0)
        assert out == "Error splitting arrangement clip: Beat 3 is not inside clip"
