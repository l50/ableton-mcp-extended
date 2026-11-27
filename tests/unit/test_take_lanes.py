"""Take-lane behavior with explicit Live fakes; never contact Live."""
import json
import sys
import types
from unittest.mock import MagicMock

import pytest

_framework = types.ModuleType('_Framework.ControlSurface')
_framework.ControlSurface = type('ControlSurface', (), {})
sys.modules.setdefault('_Framework', types.ModuleType('_Framework'))
sys.modules.setdefault('_Framework.ControlSurface', _framework)
_mock_mcp = MagicMock()
_mock_mcp.FastMCP.return_value.tool.return_value = lambda fn: fn
sys.modules.setdefault('mcp', MagicMock())
sys.modules.setdefault('mcp.server', MagicMock())
sys.modules.setdefault('mcp.server.fastmcp', _mock_mcp)

from AbletonMCP_Remote_Script import AbletonMCP


class Clip:
    def __init__(self, name, start=0., end=4., audio=True):
        self.name = name
        self.start_time = start
        self.end_time = end
        self.is_audio_clip = audio
        if audio:
            self.file_path = '/Samples/' + name + '.wav'


class Lane:
    def __init__(self, name, clips=()):
        self.name = name
        self.arrangement_clips = list(clips)


class Track:
    """Only confirmed API members: deliberately no delete_take_lane method."""
    def __init__(self):
        self.name = 'Test'
        self.arrangement_clips = [Clip('Main')]
        self.take_lanes = [Lane('Keep', [Clip('Keep')]),
                           Lane('Edit', [Clip('Remove'), Clip('Stay', 8., 12.)]),
                           Lane('Empty')]


@pytest.fixture
def script():
    obj = AbletonMCP.__new__(AbletonMCP)
    obj._song = types.SimpleNamespace(tracks=[Track()], return_tracks=[])
    obj.log_message = MagicMock()
    obj.schedule_message = MagicMock(side_effect=lambda _, fn: fn())
    return obj


def command(script, name, **params):
    return script._process_command({'type': name, 'params': dict(track_index=0, **params)})


def test_list_metadata(script):
    reply = command(script, 'get_take_lanes')
    assert reply['status'] == 'success', reply
    lanes = reply['result']['take_lanes']
    assert [lane['name'] for lane in lanes] == ['Keep', 'Edit', 'Empty']
    assert lanes[1] == {'lane_index': 1, 'name': 'Edit', 'clips': [
        {'clip_index': 0, 'name': 'Remove', 'start': 0., 'end': 4., 'file': '/Samples/Remove.wav'},
        {'clip_index': 1, 'name': 'Stay', 'start': 8., 'end': 12., 'file': '/Samples/Stay.wav'}]}
    assert lanes[2]['clips'] == []
    script.schedule_message.assert_not_called()


def test_list_metadata_midi_and_no_lanes(script):
    track = script._song.tracks[0]
    track.take_lanes[0].arrangement_clips = [Clip('MIDI', audio=False)]
    assert command(script, 'get_take_lanes')['result']['take_lanes'][0]['clips'][0]['file'] is None
    track.take_lanes = []
    assert command(script, 'get_take_lanes')['result']['take_lanes'] == []


def test_invalid_track_and_unavailable_api(script):
    assert script._process_command({'type': 'get_take_lanes', 'params': {'track_index': -1}})['status'] == 'error'
    del script._song.tracks[0].take_lanes
    result = command(script, 'get_take_lanes')
    assert result['status'] == 'error'
    assert 'take lanes' in result['message']


def test_tools_register_with_real_fastmcp():
    import subprocess
    from pathlib import Path
    proc = subprocess.run([sys.executable, '-c', '''
import asyncio
from MCP_Server.app import mcp
import MCP_Server.tools
names = {tool.name for tool in asyncio.run(mcp.list_tools())}
assert 'list_take_lanes' in names
assert not {'delete_take_lane_clip', 'delete_take_lane'} & names
print('take lane tools registered')
'''], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert 'take lane tools registered' in proc.stdout


def test_tools_translate_indices(monkeypatch):
    from MCP_Server import connection
    from MCP_Server.tools import take_lanes
    conn = MagicMock()
    conn.send_command.return_value = {'track_index': 1, 'take_lanes': [
        {'lane_index': 0, 'clips': [{'clip_index': 1, 'start': 8., 'end': 12.}]}]}
    monkeypatch.setattr(connection, 'get_ableton_connection', lambda: conn)
    result = json.loads(take_lanes.list_take_lanes(None, 2))
    conn.send_command.assert_called_once_with('get_take_lanes', {'track_index': 1})
    assert result == {'track_index': 2, 'take_lanes': [
        {'lane_index': 1, 'clips': [{'clip_index': 2, 'start': 8., 'end': 12.}]}]}


@pytest.mark.parametrize('value', [0, -1, True, 1.5])
def test_tools_invalid_indices_fail_before_connect(monkeypatch, value):
    from MCP_Server import connection
    from MCP_Server.tools import take_lanes
    conn = MagicMock()
    monkeypatch.setattr(connection, 'get_ableton_connection', conn)
    assert take_lanes.list_take_lanes(None, value).startswith('Error ')
    conn.assert_not_called()


def test_list_metadata_main_lane_unchanged(script):
    track = script._song.tracks[0]
    main = list(track.arrangement_clips)
    metadata = [dict(clip.__dict__) for clip in main]
    first = command(script, 'get_take_lanes')
    second = command(script, 'get_take_lanes')
    assert first == second
    assert track.arrangement_clips == main
    assert [dict(clip.__dict__) for clip in main] == metadata
