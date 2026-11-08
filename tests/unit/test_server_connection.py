"""Unit tests for the MCP server's socket layer, connection pool and entry point.

No real socket is opened: ``socket.create_connection`` and the socket object
are replaced with fakes that replay scripted ``recv`` chunks.
"""
import asyncio
import json
import os
import socket
import sys
from unittest.mock import MagicMock, call, patch

import pytest

if "MCP_Server.server" not in sys.modules:
    _mock_fastmcp = MagicMock()
    _mock_fastmcp.FastMCP.return_value.tool.return_value = lambda fn: fn
    sys.modules["mcp"] = MagicMock()
    sys.modules["mcp.server"] = MagicMock()
    sys.modules["mcp.server.fastmcp"] = _mock_fastmcp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import MCP_Server  # noqa: E402
from MCP_Server import server  # noqa: E402


class _FakeSocket:
    """Socket double: ``recv`` replays scripted chunks or raises exceptions."""

    def __init__(self, chunks=(), sendall_error=None, close_error=None):
        self.chunks = list(chunks)
        self.sent = []
        self.timeouts = []
        self.sendall_error = sendall_error
        self.close_error = close_error
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self, _size):
        item = self.chunks.pop(0) if self.chunks else b""
        if isinstance(item, BaseException):
            raise item
        return item

    def sendall(self, data):
        if self.sendall_error is not None:
            raise self.sendall_error
        self.sent.append(data)

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture(autouse=True)
def _reset_global_connection():
    server._ableton_connection = None
    server._invalidate_external_plugin_cache()
    yield
    server._ableton_connection = None
    server._invalidate_external_plugin_cache()


def _conn(sock=None):
    return server.AbletonConnection(host="localhost", port=9877, sock=sock)


# ── AbletonConnection.connect / disconnect ───────────────────────────


class TestConnect:
    def test_already_connected_short_circuits(self):
        conn = _conn(sock=_FakeSocket())
        with patch.object(server.socket, "create_connection") as create:
            assert conn.connect() is True
        create.assert_not_called()

    def test_successful_connect_uses_bounded_timeout(self):
        sock = _FakeSocket()
        conn = _conn()
        with patch.object(server.socket, "create_connection", return_value=sock) as create:
            assert conn.connect() is True
        create.assert_called_once_with(("localhost", 9877), timeout=server.CONNECT_TIMEOUT_SECONDS)
        assert conn.sock is sock
        assert sock.timeouts == [None]

    def test_failed_connect_returns_false(self):
        conn = _conn()
        with patch.object(server.socket, "create_connection", side_effect=ConnectionRefusedError("no")):
            assert conn.connect() is False
        assert conn.sock is None


class TestDisconnect:
    def test_closes_socket(self):
        sock = _FakeSocket()
        conn = _conn(sock=sock)
        conn.disconnect()
        assert sock.closed and conn.sock is None

    def test_close_error_still_clears_socket(self):
        conn = _conn(sock=_FakeSocket(close_error=OSError("bad fd")))
        conn.disconnect()
        assert conn.sock is None

    def test_noop_without_socket(self):
        _conn().disconnect()


# ── AbletonConnection.receive_full_response ──────────────────────────


class TestReceiveFullResponse:
    def test_reassembles_json_split_across_chunks(self):
        sock = _FakeSocket([b'{"status": "suc', b'cess", "result": {}}'])
        data = _conn().receive_full_response(sock)
        assert json.loads(data) == {"status": "success", "result": {}}
        assert sock.timeouts == [15.0]

    @pytest.mark.parametrize("chunks,message", [
        ([b""], "Connection closed before receiving any data"),
        ([b'{"partial": ', b""], "Incomplete JSON response received"),
        ([socket.timeout()], "No data received"),
        ([b'{"partial": ', socket.timeout()], "Incomplete JSON response received"),
    ])
    def test_failure_modes(self, chunks, message):
        with pytest.raises(Exception, match=message):
            _conn().receive_full_response(_FakeSocket(chunks))

    def test_connection_reset_is_reraised(self):
        with pytest.raises(ConnectionResetError):
            _conn().receive_full_response(_FakeSocket([ConnectionResetError("reset")]))


# ── AbletonConnection.send_command ───────────────────────────────────


class TestSendCommand:
    def test_raises_when_cannot_connect(self):
        conn = _conn()
        with patch.object(conn, "connect", return_value=False):
            with pytest.raises(ConnectionError, match="Not connected"):
                conn.send_command("get_session_info")

    def test_read_command_round_trip(self):
        sock = _FakeSocket([b'{"status": "success", "result": {"tempo": 120}}'])
        conn = _conn(sock=sock)
        with patch("time.sleep") as sleep:
            result = conn.send_command("get_session_info")
        assert result == {"tempo": 120}
        assert json.loads(sock.sent[0]) == {"type": "get_session_info", "params": {}}
        assert sock.timeouts[0] == 10.0
        sleep.assert_not_called()

    def test_modifying_command_waits_and_uses_longer_timeout(self):
        sock = _FakeSocket([b'{"status": "success", "result": {"tempo": 90}}'])
        conn = _conn(sock=sock)
        with patch("time.sleep") as sleep:
            result = conn.send_command("set_tempo", {"tempo": 90})
        assert result == {"tempo": 90}
        assert json.loads(sock.sent[0]) == {"type": "set_tempo", "params": {"tempo": 90}}
        assert sock.timeouts[0] == 15.0
        assert sleep.call_args_list == [call(0.1), call(0.1)]

    def test_missing_result_returns_empty_dict(self):
        conn = _conn(sock=_FakeSocket([b'{"status": "success"}']))
        assert conn.send_command("get_cue_points") == {}

    def test_remote_error_status_raises_and_drops_socket(self):
        conn = _conn(sock=_FakeSocket([b'{"status": "error", "message": "Track index out of range"}']))
        with pytest.raises(Exception, match="Communication error with Ableton: Track index out of range"):
            conn.send_command("get_track_info", {"track_index": 99})
        assert conn.sock is None

    @pytest.mark.parametrize("error,message", [
        (socket.timeout(), "Timeout waiting for Ableton response"),
        (BrokenPipeError("pipe"), "Connection to Ableton lost: pipe"),
    ])
    def test_socket_errors_are_wrapped(self, error, message):
        conn = _conn(sock=_FakeSocket(sendall_error=error))
        with pytest.raises(Exception, match=message):
            conn.send_command("get_session_info")
        assert conn.sock is None

    def test_invalid_json_is_wrapped(self):
        conn = _conn(sock=_FakeSocket())
        with patch.object(conn, "receive_full_response", return_value=b"{not json"):
            with pytest.raises(Exception, match="Invalid response from Ableton"):
                conn.send_command("get_session_info")
        assert conn.sock is None


# ── get_ableton_connection ───────────────────────────────────────────


class _FakeConnection:
    """Stand-in for AbletonConnection created by get_ableton_connection."""

    instances = []
    connect_results = []
    validate_errors = []
    connect_raises = None

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sock = _FakeSocket()
        self.disconnected = False
        _FakeConnection.instances.append(self)

    def connect(self):
        if _FakeConnection.connect_raises is not None:
            raise _FakeConnection.connect_raises
        return _FakeConnection.connect_results.pop(0) if _FakeConnection.connect_results else True

    def send_command(self, command_type, params=None):
        assert command_type == "get_session_info"
        if _FakeConnection.validate_errors:
            err = _FakeConnection.validate_errors.pop(0)
            if err is not None:
                raise err
        return {}

    def disconnect(self):
        self.disconnected = True


@pytest.fixture
def fake_connection_cls():
    _FakeConnection.instances = []
    _FakeConnection.connect_results = []
    _FakeConnection.validate_errors = []
    _FakeConnection.connect_raises = None
    with patch.object(server, "AbletonConnection", _FakeConnection), \
            patch.object(server.time, "sleep") as sleep:
        _FakeConnection.sleep = sleep
        yield _FakeConnection


class TestGetAbletonConnection:
    def test_reuses_live_connection(self, fake_connection_cls):
        existing = MagicMock()
        server._ableton_connection = existing
        assert server.get_ableton_connection() is existing
        existing.sock.settimeout.assert_called_once_with(1.0)
        existing.sock.sendall.assert_called_once_with(b"")
        assert fake_connection_cls.instances == []

    def test_replaces_dead_connection(self, fake_connection_cls):
        dead = MagicMock()
        dead.sock.sendall.side_effect = BrokenPipeError("dead")
        dead.disconnect.side_effect = OSError("also dead")
        server._ableton_connection = dead
        server._external_plugin_cache["plugins"] = [{"name": "stale"}]

        conn = server.get_ableton_connection()

        assert conn is fake_connection_cls.instances[0]
        assert (conn.host, conn.port) == (server.ABLETON_HOST, server.ABLETON_PORT)
        dead.disconnect.assert_called_once()
        assert server._external_plugin_cache["plugins"] is None

    def test_retries_until_connect_succeeds(self, fake_connection_cls):
        fake_connection_cls.connect_results = [False, True]
        conn = server.get_ableton_connection()
        assert conn is fake_connection_cls.instances[1]
        fake_connection_cls.sleep.assert_called_once_with(server.CONNECT_RETRY_DELAY_SECONDS)

    def test_retries_after_failed_validation(self, fake_connection_cls):
        fake_connection_cls.validate_errors = [RuntimeError("bad handshake"), None]
        conn = server.get_ableton_connection()
        assert fake_connection_cls.instances[0].disconnected is True
        assert conn is fake_connection_cls.instances[1]

    def test_gives_up_after_all_attempts(self, fake_connection_cls):
        fake_connection_cls.connect_results = [False] * server.CONNECT_ATTEMPTS
        with pytest.raises(Exception, match="Could not connect to Ableton at localhost:9877"):
            server.get_ableton_connection()
        assert len(fake_connection_cls.instances) == server.CONNECT_ATTEMPTS
        assert fake_connection_cls.sleep.call_count == server.CONNECT_ATTEMPTS - 1
        assert server._ableton_connection is None

    def test_unexpected_connect_error_is_cleaned_up(self, fake_connection_cls):
        fake_connection_cls.connect_raises = RuntimeError("kaboom")
        with pytest.raises(Exception, match="Could not connect"):
            server.get_ableton_connection()
        assert all(c.disconnected for c in fake_connection_cls.instances)


# ── lifespan and entry point ─────────────────────────────────────────


class TestServerLifespan:
    def _run(self):
        async def body():
            async with server.server_lifespan(None) as ctx:
                return ctx
        return asyncio.run(body())

    def test_yields_empty_context_and_disconnects(self):
        conn = MagicMock()
        server._ableton_connection = conn
        server._external_plugin_cache["plugins"] = [{"name": "x"}]

        assert self._run() == {}

        conn.disconnect.assert_called_once()
        assert server._ableton_connection is None
        assert server._external_plugin_cache["plugins"] is None

    def test_no_connection_to_close(self):
        assert self._run() == {}


class TestEntryPoint:
    def test_load_supervisor_module_as_package(self):
        assert server._load_supervisor_module() is sys.modules["MCP_Server.supervisor"]

    def test_load_supervisor_module_as_script(self, monkeypatch):
        monkeypatch.delattr(MCP_Server, "supervisor", raising=False)
        monkeypatch.setitem(sys.modules, "MCP_Server.supervisor", None)
        monkeypatch.setattr(sys, "path", list(sys.path))
        with patch.dict(sys.modules):
            sys.modules.pop("supervisor", None)
            mod = server._load_supervisor_module()
            assert mod.__name__ == "supervisor"
            assert hasattr(mod, "Supervisor")
        assert sys.path[0] == os.path.dirname(os.path.abspath(server.__file__))

    def test_run_worker_sets_up_logging_and_runs_mcp(self):
        fake_sup = MagicMock()
        with patch.object(server, "_load_supervisor_module", return_value=fake_sup), \
                patch.object(server.mcp, "run") as run:
            server._run_worker()
        fake_sup.setup_file_logging.assert_called_once_with("AbletonMCPWorker")
        fake_sup.install_signal_logging.assert_called_once_with(
            fake_sup.setup_file_logging.return_value)
        run.assert_called_once_with()

    @pytest.mark.parametrize("argv,env", [
        (["server.py", "--worker"], None),
        (["server.py"], "1"),
    ])
    def test_main_runs_worker_in_process(self, monkeypatch, argv, env):
        monkeypatch.setattr(sys, "argv", argv)
        if env is None:
            monkeypatch.delenv("ABLETON_MCP_NO_SUPERVISOR", raising=False)
        else:
            monkeypatch.setenv("ABLETON_MCP_NO_SUPERVISOR", env)
        with patch.object(server, "_run_worker") as run_worker, \
                patch.object(server, "_load_supervisor_module") as load:
            server.main()
        run_worker.assert_called_once()
        load.assert_not_called()

    def test_main_launches_supervisor_with_worker_command(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["server.py"])
        monkeypatch.delenv("ABLETON_MCP_NO_SUPERVISOR", raising=False)
        fake_sup = MagicMock()
        fake_sup.run_supervisor.return_value = 3
        with patch.object(server, "_load_supervisor_module", return_value=fake_sup):
            with pytest.raises(SystemExit) as exc:
                server.main()
        assert exc.value.code == 3
        server_file = os.path.abspath(server.__file__)
        fake_sup.run_supervisor.assert_called_once_with(
            [sys.executable, server_file, "--worker"],
            cwd=os.path.dirname(os.path.dirname(server_file)),
        )
