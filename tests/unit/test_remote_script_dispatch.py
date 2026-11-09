"""Unit tests for the Remote Script's command routing, socket server and lifecycle.

Live's ``_Framework`` is stubbed; scripts are built with ``__new__`` and the
ControlSurface methods they rely on (``log_message``, ``schedule_message``,
``show_message``) are set on the instance, so these tests do not depend on
which ``_Framework`` stub another test module installed first.
"""
import json
import os
import socket
import sys
import types
from unittest.mock import MagicMock, call, patch

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

import AbletonMCP_Remote_Script as rs  # noqa: E402
from AbletonMCP_Remote_Script import AbletonMCP  # noqa: E402


def _make_script(run_scheduled=True):
    script = AbletonMCP.__new__(AbletonMCP)
    script._song = MagicMock()
    script.log_message = MagicMock()
    script.show_message = MagicMock()
    if run_scheduled:
        script.schedule_message = MagicMock(side_effect=lambda _delay, fn: fn())
    else:
        script.schedule_message = MagicMock()
    return script


NOTES = [{"pitch": 60, "start_time": 0.0, "duration": 1.0, "velocity": 100, "mute": False}]

# (command, params, handler attribute, expected positional args)
READ_COMMANDS = [
    ("get_session_info", {}, "_get_session_info", ()),
    ("get_track_info", {"track_index": 2}, "_get_track_info", (2,)),
    ("get_track_info", {}, "_get_track_info", (0,)),
    ("get_track_volume", {"track_index": 4}, "_get_track_volume", (4,)),
    ("get_browser_item", {"uri": "u", "path": "p"}, "_get_browser_item", ("u", "p")),
    ("get_browser_tree", {"category_type": "drums"}, "get_browser_tree", ("drums",)),
    ("get_browser_items_at_path", {"path": "drums/kits"}, "get_browser_items_at_path",
     ("drums/kits",)),
    ("get_arrangement_info", {}, "_get_arrangement_info", (-1,)),
    ("get_cue_points", {}, "_get_cue_points", ()),
    ("get_device_parameters",
     {"track_index": 1, "device_index": 2, "chain_index": 0, "show_all": True},
     "_get_device_parameters", (1, 2, 0, True)),
    ("get_chain_info", {"track_index": 1, "device_index": 2, "chain_index": 3},
     "_get_chain_info", (1, 2, 3)),
    ("get_drum_pad_info", {"track_index": 1, "device_index": 2}, "_get_drum_pad_info", (1, 2)),
]

WRITE_COMMANDS = [
    ("create_midi_track", {"index": 2}, "_create_midi_track", (2,)),
    ("create_midi_track", {}, "_create_midi_track", (-1,)),
    ("set_track_name", {"track_index": 1, "name": "Lead"}, "_set_track_name", (1, "Lead")),
    ("create_clip", {"track_index": 1, "clip_index": 2, "length": 8.0}, "_create_clip",
     (1, 2, 8.0)),
    ("add_notes_to_clip", {"track_index": 1, "clip_index": 2, "notes": NOTES},
     "_add_notes_to_clip", (1, 2, NOTES)),
    ("set_clip_name", {"track_index": 1, "clip_index": 2, "name": "Verse"}, "_set_clip_name",
     (1, 2, "Verse")),
    ("set_tempo", {"tempo": 99.0}, "_set_tempo", (99.0,)),
    ("fire_clip", {"track_index": 1, "clip_index": 2}, "_fire_clip", (1, 2)),
    ("stop_clip", {"track_index": 1, "clip_index": 2}, "_stop_clip", (1, 2)),
    ("start_playback", {}, "_start_playback", ()),
    ("stop_playback", {}, "_stop_playback", ()),
    ("load_browser_item", {"track_index": 1, "item_uri": "uri"}, "_load_browser_item",
     (1, "uri")),
    ("set_song_time", {"time": 4.0}, "_set_song_time", (4.0,)),
    ("set_arrangement_loop", {"enabled": False, "start": 1.0, "length": 2.0},
     "_set_arrangement_loop", (False, 1.0, 2.0)),
    ("jump_to_cue", {"direction": "next"}, "_jump_to_cue", ("next", None)),
    ("create_cue_point", {"time": 4.0, "name": "Drop"}, "_create_cue_point", (4.0, "Drop")),
    ("delete_cue_point", {"time": 4.0}, "_delete_cue_point", (4.0,)),
    ("create_arrangement_clip", {"track_index": 1, "position": 4.0, "length": 8.0},
     "_create_arrangement_clip", (1, 4.0, 8.0)),
    ("create_arrangement_audio_clip", {"track_index": 1, "position": 4.0, "file_path": "/a.wav"},
     "_create_arrangement_audio_clip", (1, 4.0, "/a.wav")),
    ("duplicate_to_arrangement", {"track_index": 1, "clip_index": 2, "destination_time": 8.0},
     "_duplicate_to_arrangement", (1, 2, 8.0)),
    ("delete_arrangement_clip", {"track_index": 1, "clip_name": "Intro"},
     "_delete_arrangement_clip", (1, None, "Intro")),
    ("set_arrangement_clip_property",
     {"track_index": 1, "clip_index": 2, "property": "muted", "value": True},
     "_set_arrangement_clip_property", (1, 2, "muted", True, None)),
    ("set_view", {"view_name": "Session"}, "_set_view", ("Session",)),
    ("control_arrangement_view", {"action": "zoom_in", "track_index": 3},
     "_control_arrangement_view", ("zoom_in", 3)),
    ("manage_clip_automation",
     {"track_index": 1, "clip_index": 2, "action": "clear", "parameter_name": "Volume"},
     "_manage_clip_automation", (1, 2, "clear", "Volume", None)),
    ("add_notes_to_arrangement_clip", {"track_index": 1, "clip_index": 2, "notes": NOTES},
     "_add_notes_to_arrangement_clip", (1, 2, NOTES)),
    ("set_device_parameter",
     {"track_index": 1, "device_index": 2, "chain_index": 0, "parameter_name": "Cutoff",
      "parameter_index": None, "value": 0.5},
     "_set_device_parameter", (1, 2, 0, "Cutoff", None, 0.5)),
    ("set_device_enabled", {"track_index": 1, "device_index": 2, "enabled": False},
     "_set_device_enabled", (1, 2, None, False)),
    ("delete_device", {"track_index": 1, "device_index": 2}, "_delete_device", (1, 2)),
    ("delete_track", {"track_index": 3}, "_delete_track", (3,)),
    ("set_track_volume", {"track_index": 1, "volume": 0.5}, "_set_track_volume", (1, 0.5)),
    ("set_track_panning", {"track_index": 1, "panning": -0.5}, "_set_track_panning", (1, -0.5)),
    ("navigate_preset", {"track_index": 1, "device_index": 0, "direction": "next"},
     "_navigate_preset", (1, 0, None, "next")),
]


def _ids(cases):
    return [f"{c[0]}-{i}" for i, c in enumerate(cases)]


class TestProcessCommandRouting:
    @pytest.mark.parametrize("command,params,handler,args", READ_COMMANDS, ids=_ids(READ_COMMANDS))
    def test_read_commands_run_inline(self, command, params, handler, args):
        script = _make_script()
        setattr(script, handler, MagicMock(return_value={"ok": command}))

        response = script._process_command({"type": command, "params": params})

        assert response == {"status": "success", "result": {"ok": command}}
        getattr(script, handler).assert_called_once_with(*args)
        script.schedule_message.assert_not_called()

    @pytest.mark.parametrize("command,params,handler,args", WRITE_COMMANDS,
                             ids=_ids(WRITE_COMMANDS))
    def test_write_commands_run_on_main_thread(self, command, params, handler, args):
        script = _make_script()
        setattr(script, handler, MagicMock(return_value={"ok": command}))

        response = script._process_command({"type": command, "params": params})

        assert response == {"status": "success", "result": {"ok": command}}
        getattr(script, handler).assert_called_once_with(*args)
        assert script.schedule_message.call_args[0][0] == 0

    def test_write_runs_directly_when_already_on_main_thread(self):
        script = _make_script()
        script.schedule_message = MagicMock(side_effect=AssertionError("main thread"))
        script._set_tempo = MagicMock(return_value={"tempo": 140.0})

        response = script._process_command({"type": "set_tempo", "params": {"tempo": 140.0}})

        assert response == {"status": "success", "result": {"tempo": 140.0}}

    def test_write_handler_error_is_reported(self):
        script = _make_script()
        script._set_tempo = MagicMock(side_effect=ValueError("tempo out of range"))

        response = script._process_command({"type": "set_tempo", "params": {"tempo": 1.0}})

        assert response == {"status": "error", "result": {}, "message": "tempo out of range"}

    def test_write_times_out_when_main_thread_never_runs(self):
        script = _make_script(run_scheduled=False)
        with patch.object(rs.queue.Queue, "get", side_effect=rs.queue.Empty):
            response = script._process_command({"type": "set_tempo", "params": {"tempo": 1.0}})
        assert response["status"] == "error"
        assert response["message"] == "Timeout waiting for operation to complete"

    def test_write_with_none_result_passes_none_through(self):
        script = _make_script()
        script._start_playback = MagicMock(return_value=None)
        response = script._process_command({"type": "start_playback"})
        assert response == {"status": "success", "result": None}

    def test_unknown_command(self):
        script = _make_script()
        response = script._process_command({"type": "explode"})
        assert response == {"status": "error", "result": {}, "message": "Unknown command: explode"}

    def test_read_handler_error_is_reported(self):
        script = _make_script()
        script._get_track_info = MagicMock(side_effect=IndexError("Track index out of range"))
        response = script._process_command({"type": "get_track_info", "params": {"track_index": 9}})
        assert response["status"] == "error"
        assert response["message"] == "Track index out of range"

    @pytest.mark.parametrize("command", ["get_browser_categories", "get_browser_items"])
    def test_legacy_browser_commands_without_handlers_fail_cleanly(self, command):
        script = _make_script()
        response = script._process_command({"type": command, "params": {}})
        assert response["status"] == "error"


# ── socket server ────────────────────────────────────────────────────


class TestHandleClient:
    def _client(self, chunks, sendall_side_effect=None):
        client = MagicMock()
        client.recv.side_effect = list(chunks)
        if sendall_side_effect is not None:
            client.sendall.side_effect = sendall_side_effect
        return client

    def test_reassembles_split_command_and_replies(self):
        script = _make_script()
        script.running = True
        script._process_command = MagicMock(return_value={"status": "success", "result": {"a": 1}})
        client = self._client([b'{"type": "get_ses', b'sion_info", "params": {}}', b""])

        script._handle_client(client)

        script._process_command.assert_called_once_with(
            {"type": "get_session_info", "params": {}})
        assert json.loads(client.sendall.call_args[0][0]) == {"status": "success", "result": {"a": 1}}
        client.settimeout.assert_called_once_with(None)
        client.close.assert_called_once()

    def test_python2_style_str_chunks_and_sendall(self):
        script = _make_script()
        script.running = True
        script._process_command = MagicMock(return_value={"status": "success", "result": {}})
        client = self._client(['{"type": "x"}', ""], sendall_side_effect=[AttributeError(), None])

        script._handle_client(client)

        assert client.sendall.call_args_list[1] == call('{"status": "success", "result": {}}')

    def test_recv_error_sends_error_response_and_stops(self):
        script = _make_script()
        script.running = True
        client = self._client([OSError("connection reset")])

        script._handle_client(client)

        assert json.loads(client.sendall.call_args[0][0]) == {
            "status": "error", "message": "connection reset"}
        client.close.assert_called_once()

    def test_decode_error_reports_and_keeps_reading(self):
        script = _make_script()
        script.running = True
        script._process_command = MagicMock(return_value={"status": "success", "result": {}})
        client = self._client([b"\xff", b'{"type": "x"}', b""])

        script._handle_client(client)

        assert client.sendall.call_count == 2
        assert json.loads(client.sendall.call_args_list[0][0][0])["status"] == "error"

    def test_unsendable_error_breaks_loop(self):
        script = _make_script()
        script.running = True
        client = self._client([OSError("boom")], sendall_side_effect=OSError("dead"))
        script._handle_client(client)
        client.close.assert_called_once()

    def test_python2_error_fallback_failure_is_logged(self):
        script = _make_script()
        script.running = True
        client = self._client([OSError("boom")],
                              sendall_side_effect=[AttributeError(), RuntimeError("dead")])
        client.close.side_effect = OSError("already closed")

        script._handle_client(client)

        logged = [c.args[0] for c in script.log_message.call_args_list]
        assert any(m.startswith("Error in client handler") for m in logged)
        assert logged[-1] == "Client handler stopped"

    def test_stops_when_not_running(self):
        script = _make_script()
        script.running = False
        client = self._client([])
        script._handle_client(client)
        client.recv.assert_not_called()


class TestServerThread:
    def test_accepts_clients_and_survives_errors(self):
        script = _make_script()
        script.running = True
        script.client_threads = []
        client = MagicMock()

        def stop_and_timeout():
            script.running = False
            raise socket.timeout()

        accept_effects = [
            (client, ("127.0.0.1", 5555)),
            socket.timeout(),
            OSError("accept failed"),
            stop_and_timeout,
        ]

        def accept():
            effect = accept_effects.pop(0)
            if callable(effect):
                return effect()
            if isinstance(effect, BaseException):
                raise effect
            return effect

        script.server = MagicMock()
        script.server.accept.side_effect = accept

        with patch.object(rs.threading, "Thread") as thread_cls, \
                patch.object(rs.time, "sleep") as sleep:
            thread_cls.return_value.is_alive.return_value = False
            script._server_thread()

        script.server.settimeout.assert_called_once_with(1.0)
        thread_cls.assert_called_once_with(target=script._handle_client, args=(client,))
        thread_cls.return_value.start.assert_called_once()
        assert script.client_threads == []
        sleep.assert_called_once_with(0.5)
        script.show_message.assert_called_once_with("AbletonMCP: Client connected")

    def test_accept_error_after_shutdown_is_not_logged(self):
        script = _make_script()
        script.running = True
        script.client_threads = []

        def fail_after_stop():
            script.running = False
            raise OSError("socket closed")

        script.server = MagicMock()
        script.server.accept.side_effect = fail_after_stop
        with patch.object(rs.time, "sleep"):
            script._server_thread()
        logged = [c.args[0] for c in script.log_message.call_args_list]
        assert not any(m.startswith("Server accept error") for m in logged)

    def test_fatal_error_is_logged(self):
        script = _make_script()
        script.server = MagicMock()
        script.server.settimeout.side_effect = OSError("bad socket")
        script._server_thread()
        script.log_message.assert_called_with("Server thread error: bad socket")


class TestStartServer:
    def test_binds_listens_and_starts_thread(self):
        script = _make_script()
        sock = MagicMock()
        with patch.object(rs.socket, "socket", return_value=sock), \
                patch.object(rs.threading, "Thread") as thread_cls:
            script.start_server()
        sock.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind.assert_called_once_with((rs.HOST, rs.DEFAULT_PORT))
        sock.listen.assert_called_once_with(5)
        assert script.running is True
        thread_cls.assert_called_once_with(target=script._server_thread)
        assert thread_cls.return_value.daemon is True
        thread_cls.return_value.start.assert_called_once()

    def test_bind_failure_is_shown(self):
        script = _make_script()
        sock = MagicMock()
        sock.bind.side_effect = OSError("address in use")
        with patch.object(rs.socket, "socket", return_value=sock):
            script.start_server()
        script.show_message.assert_called_once_with(
            "AbletonMCP: Error starting server - address in use")


# ── lifecycle ────────────────────────────────────────────────────────


class _LifecycleControlSurface:
    def __init__(self, c_instance):
        self.c_instance = c_instance

    def disconnect(self):
        self.base_disconnected = True


class _TestableMCP(AbletonMCP):
    song_obj = MagicMock(name="song")

    def log_message(self, msg):
        self.__dict__.setdefault("logged", []).append(msg)

    def show_message(self, msg):
        self.__dict__.setdefault("shown", []).append(msg)

    def song(self):
        return self.song_obj


class TestLifecycle:
    def test_create_instance(self):
        with patch.object(rs, "AbletonMCP") as cls:
            assert rs.create_instance("c") is cls.return_value
        cls.assert_called_once_with("c")

    def test_init_caches_song_and_starts_server(self):
        with patch.object(rs, "ControlSurface", _LifecycleControlSurface), \
                patch.object(_TestableMCP, "start_server") as start:
            script = _TestableMCP("c_instance")
        assert script.c_instance == "c_instance"
        assert script._song is _TestableMCP.song_obj
        assert script.running is False and script.client_threads == []
        start.assert_called_once_with()
        assert script.shown == ["AbletonMCP: Listening for commands on port 9877"]

    def test_disconnect_closes_server_and_joins_thread(self):
        script = _TestableMCP.__new__(_TestableMCP)
        script.running = True
        script.server = MagicMock()
        script.server_thread = MagicMock()
        script.server_thread.is_alive.return_value = True
        stuck = MagicMock()
        stuck.is_alive.return_value = True
        done = MagicMock()
        done.is_alive.return_value = False
        script.client_threads = [stuck, done]

        with patch.object(rs, "ControlSurface", _LifecycleControlSurface):
            script.disconnect()

        assert script.running is False
        script.server.close.assert_called_once()
        script.server_thread.join.assert_called_once_with(1.0)
        assert script.logged.count("Client thread still alive during disconnect") == 1
        assert script.base_disconnected is True
        assert script.logged[-1] == "AbletonMCP disconnected"

    def test_disconnect_tolerates_close_error_and_missing_thread(self):
        script = _TestableMCP.__new__(_TestableMCP)
        script.server = MagicMock()
        script.server.close.side_effect = OSError("already closed")
        script.server_thread = None
        script.client_threads = []
        with patch.object(rs, "ControlSurface", _LifecycleControlSurface):
            script.disconnect()
        assert script.base_disconnected is True
