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
        self.deleted = []

    def delete_clip(self, clip):
        for lane in self.take_lanes:
            if clip in lane.arrangement_clips:
                lane.arrangement_clips.remove(clip)
                self.deleted.append(clip)
                return
        raise AssertionError('attempt to delete main-lane or unknown clip')


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


def test_delete_clip_only_selected_take(script):
    track = script._song.tracks[0]
    main = list(track.arrangement_clips)
    kept = track.take_lanes[0].arrangement_clips[0]
    result = command(script, 'delete_take_lane_clip', lane_index=1, clip_index=0)
    assert result['status'] == 'success', result
    assert result['result']['deleted_clip']['name'] == 'Remove'
    assert [c.name for c in track.take_lanes[1].arrangement_clips] == ['Stay']
    assert track.take_lanes[0].arrangement_clips == [kept]
    assert track.arrangement_clips == main
    assert [c.name for c in track.deleted] == ['Remove']
    script.schedule_message.assert_called_once()


@pytest.mark.parametrize('params', [
    {'lane_index': -1, 'clip_index': 0}, {'lane_index': 3, 'clip_index': 0},
    {'lane_index': 1, 'clip_index': -1}, {'lane_index': 1, 'clip_index': 2},
    {'lane_index': 2, 'clip_index': 0}, {'clip_index': 0}, {'lane_index': 1},
    {'lane_index': True, 'clip_index': 0}, {'lane_index': 1.5, 'clip_index': 0},
])
def test_invalid_clip_targets_never_delete(script, params):
    track = script._song.tracks[0]
    result = command(script, 'delete_take_lane_clip', **params)
    assert result['status'] == 'error', result
    assert track.deleted == []
    assert track.arrangement_clips[0].name == 'Main'


def test_invalid_track_and_unavailable_api(script):
    assert script._process_command({'type': 'get_take_lanes', 'params': {'track_index': -1}})['status'] == 'error'
    del script._song.tracks[0].take_lanes
    result = command(script, 'get_take_lanes')
    assert result['status'] == 'error'
    assert 'take lanes' in result['message']


@pytest.mark.parametrize('lane_index', [0, 2])
def test_invalid_lane_deletion_is_explicit_without_fabricated_api(script, lane_index):
    track = script._song.tracks[0]
    before = list(track.take_lanes)
    reply = command(script, 'delete_take_lane', lane_index=lane_index)
    assert reply['status'] == 'error', reply
    assert ('not empty' if lane_index == 0 else 'this Live version offers no way to delete a take lane') in reply['message']
    assert track.take_lanes == before
    assert track.deleted == []
    assert track.arrangement_clips[0].name == 'Main'


@pytest.mark.parametrize('name,args,wire', [
    ('list_take_lanes', {'track_index': 2}, {'track_index': 1}),
    ('delete_take_lane_clip', {'track_index': 2, 'lane_index': 3, 'clip_index': 4},
     {'track_index': 1, 'lane_index': 2, 'clip_index': 3}),
    ('delete_take_lane', {'track_index': 2, 'lane_index': 3}, {'track_index': 1, 'lane_index': 2}),
])
def test_tools_translate_indices(monkeypatch, name, args, wire):
    from MCP_Server import connection
    from MCP_Server.tools import take_lanes
    conn = MagicMock()
    conn.send_command.return_value = {'take_lanes': [{'lane_index': 0, 'clips': [{'clip_index': 1}]}]}
    monkeypatch.setattr(connection, 'get_ableton_connection', lambda: conn)
    result = json.loads(getattr(take_lanes, name)(None, **args))
    conn.send_command.assert_called_once_with('get_take_lanes' if name == 'list_take_lanes' else name, wire)
    assert result['take_lanes'][0]['lane_index'] == 1
    assert result['take_lanes'][0]['clips'][0]['clip_index'] == 2


@pytest.mark.parametrize('field', ['track_index', 'lane_index', 'clip_index'])
@pytest.mark.parametrize('value', [0, -1, True, 1.5])
def test_tools_invalid_indices_fail_before_connect(monkeypatch, field, value):
    from MCP_Server import connection
    from MCP_Server.tools import take_lanes
    conn = MagicMock()
    monkeypatch.setattr(connection, 'get_ableton_connection', conn)
    args = dict(track_index=1, lane_index=1, clip_index=1)
    args[field] = value
    assert take_lanes.delete_take_lane_clip(None, **args).startswith('Error ')
    conn.assert_not_called()


def test_tools_propagate_unsupported_error(monkeypatch):
    from MCP_Server import connection
    from MCP_Server.tools import take_lanes
    conn = MagicMock()
    conn.send_command.side_effect = RuntimeError('this Live version offers no way to delete a take lane')
    monkeypatch.setattr(connection, 'get_ableton_connection', lambda: conn)
    assert 'this Live version offers no way' in take_lanes.delete_take_lane(None, 1, 1)


def test_invalid_main_lane_alias_is_refused(script):
    track = script._song.tracks[0]
    track.take_lanes[0].arrangement_clips = list(track.arrangement_clips)
    result = command(script, 'delete_take_lane_clip', lane_index=0, clip_index=0)
    assert result['status'] == 'error'
    assert 'main-lane' in result['message']
    assert track.deleted == []
    assert track.arrangement_clips[0].name == 'Main'


def test_invalid_live_clip_deletion_error_propagates(script):
    def refuse(clip):
        raise RuntimeError('Track is frozen')
    track = script._song.tracks[0]
    track.delete_clip = refuse
    result = command(script, 'delete_take_lane_clip', lane_index=1, clip_index=0)
    assert result['status'] == 'error'
    assert result['message'] == 'Track is frozen'
    assert [c.name for c in track.take_lanes[1].arrangement_clips] == ['Remove', 'Stay']


def test_tools_register_with_real_fastmcp():
    import subprocess
    from pathlib import Path
    proc = subprocess.run([sys.executable, '-c', '''
import asyncio
from MCP_Server.app import mcp
import MCP_Server.tools
names = {tool.name for tool in asyncio.run(mcp.list_tools())}
assert {'list_take_lanes', 'delete_take_lane_clip', 'delete_take_lane'} <= names
print('take lane tools registered')
'''], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert 'take lane tools registered' in proc.stdout
