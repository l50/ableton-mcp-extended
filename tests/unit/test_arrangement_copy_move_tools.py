"""Unit tests for the copy_arrangement_clip_region and move_arrangement_clip
MCP tools: command construction, index conversion and reply text."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_mock_fastmcp = MagicMock()
_mock_fastmcp.FastMCP.return_value.tool.return_value = lambda fn: fn
sys.modules['mcp'] = MagicMock()
sys.modules['mcp.server'] = MagicMock()
sys.modules['mcp.server.fastmcp'] = _mock_fastmcp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from MCP_Server.tools.arrangement import (  # noqa: E402
    copy_arrangement_clip_region,
    get_arrangement_info,
    move_arrangement_clip,
)

COPY_REPLY = {
    "name": "Vocals 0001", "start_time": 64.0, "length": 1.88, "is_audio": True,
    "file_path": "/Coven/Samples/Recorded/Vocals 0001.wav", "warping": False,
    "gain": 0.4, "pitch_coarse": 0, "pitch_fine": 0.0, "source_clip": "Vocals 0001",
    "track": "Vocals", "overlapped_clips": [],
}

MOVE_REPLY = {
    "name": "Riff", "start_time": 32.0, "moved_from": 0.0, "is_audio": False,
    "is_midi": True, "track": "Keys", "overlapped_clips": [],
}


@pytest.fixture
def ableton():
    conn = MagicMock()
    with patch('MCP_Server.connection.get_ableton_connection', return_value=conn), \
            patch('MCP_Server.tools.arrangement._get_time_signature', return_value=(4, 4)):
        yield conn


class TestCopyRegion:
    def test_command_by_index_bar(self, ableton):
        ableton.send_command.return_value = COPY_REPLY
        out = copy_arrangement_clip_region(MagicMock(), track_index=3, clip_index=2,
                                           region_start_seconds=66.10,
                                           region_end_seconds=67.04, destination_bar=17)
        ableton.send_command.assert_called_once_with("copy_arrangement_audio_region", {
            "track_index": 2, "clip_index": 1,
            "destination_time": 64.0, "region_start": 66.10, "region_end": 67.04})
        assert "Copied 66.1-67.04s of 'Vocals 0001' on 'Vocals'" in out
        assert "bar 17 (beat 64.0)" in out
        assert "unwarped (plays at original speed)" in out
        assert "Vocals 0001.wav" in out and "gain 0.4" in out
        assert "Warning" not in out
        assert out.endswith("arrangement view.")

    def test_command_by_name_beat(self, ableton):
        ableton.send_command.return_value = dict(
            COPY_REPLY, warping=True, overlapped_clips=["Chorus"], start_time=6.5)
        out = copy_arrangement_clip_region(MagicMock(), track_index=1,
                                           clip_name="Vocals 0001",
                                           region_start_seconds=1.0, region_end_seconds=2.0,
                                           destination_beat=6.5)
        ableton.send_command.assert_called_once_with("copy_arrangement_audio_region", {
            "track_index": 0, "clip_name": "Vocals 0001", "clip_index": -1,
            "destination_time": 6.5, "region_start": 1.0, "region_end": 2.0})
        assert "to bar 2 (beat 6.5)" in out and "| warped |" in out
        assert "Warning: overlapped existing clips: Chorus" in out

    def test_requires_a_clip_target(self, ableton):
        out = copy_arrangement_clip_region(MagicMock(), track_index=1,
                                           region_start_seconds=0.0, region_end_seconds=1.0)
        assert out.startswith("Error copying arrangement clip region: clip_index must be >= 1")
        ableton.send_command.assert_not_called()

    def test_live_error_is_reported(self, ableton):
        ableton.send_command.side_effect = Exception("Region is not inside the file")
        out = copy_arrangement_clip_region(MagicMock(), track_index=1, clip_index=1,
                                           region_start_seconds=0.0, region_end_seconds=1.0)
        assert out == "Error copying arrangement clip region: Region is not inside the file"


class TestMove:
    def test_command_same_track_bar(self, ableton):
        ableton.send_command.return_value = MOVE_REPLY
        out = move_arrangement_clip(MagicMock(), track_index=2, clip_index=1, destination_bar=9)
        ableton.send_command.assert_called_once_with("move_arrangement_clip", {
            "track_index": 1, "clip_index": 0, "destination_time": 32.0})
        assert out.startswith("Moved 'Riff' on 'Keys' from bar 1 (beat 0.0) to bar 9")
        assert "Audio:" not in out

    def test_unnamed_clip(self, ableton):
        ableton.send_command.return_value = dict(MOVE_REPLY, name="")
        out = move_arrangement_clip(MagicMock(), track_index=2, clip_index=1, destination_bar=9)
        assert out.startswith("Moved '(unnamed)' on 'Keys'")

    def test_command_by_name_beat(self, ableton):
        ableton.send_command.return_value = dict(
            COPY_REPLY, moved_from=8.0, overlapped_clips=["Verse"])
        out = move_arrangement_clip(MagicMock(), track_index=1, clip_name="Vocals 0001",
                                    destination_beat=64.0)
        ableton.send_command.assert_called_once_with("move_arrangement_clip", {
            "track_index": 0, "clip_name": "Vocals 0001", "clip_index": -1,
            "destination_time": 64.0})
        assert "on 'Vocals' from bar 3 (beat 8.0) to bar 17 (beat 64.0)" in out
        assert "Warning: overlapped existing clips: Verse" in out

    def test_requires_a_clip_target(self, ableton):
        out = move_arrangement_clip(MagicMock(), track_index=1, destination_bar=2)
        assert out.startswith("Error moving arrangement clip: clip_index must be >= 1")
        ableton.send_command.assert_not_called()


class TestArrangementInfoAudioSource:
    def _info(self, ableton, clip):
        ableton.send_command.return_value = {
            "transport": {"tempo": 120, "signature_numerator": 4,
                          "signature_denominator": 4},
            "tracks": [{"index": 0, "name": "Vocals", "is_midi": False,
                        "arrangement_clips": [clip]}],
        }
        return get_arrangement_info(MagicMock(), track_index=1)

    def test_unwarped_clip_shows_file_window(self, ableton):
        out = self._info(ableton, {"name": "Vocals 0001", "start_time": 0, "end_time": 4,
                                   "file_path": "/S/Vocals 0001.wav", "warping": False,
                                   "loop_start": 60.0, "loop_end": 70.5})
        assert '"Vocals 0001" bars 1-2 [Vocals 0001.wav, unwarped, file 60.000-70.500s]' in out

    def test_warped_clip_shows_file(self, ableton):
        out = self._info(ableton, {"name": "Loop", "start_time": 0, "end_time": 4,
                                   "file_path": "/S/Loop.wav", "warping": True})
        assert "[Loop.wav, warped]" in out

    def test_midi_clip_unchanged(self, ableton):
        out = self._info(ableton, {"name": "Riff", "start_time": 0, "end_time": 4})
        assert '"Riff" bars 1-2\n' in out
