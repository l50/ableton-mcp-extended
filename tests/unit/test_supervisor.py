"""Unit tests for the crash-resilient stdio supervisor (MCP_Server/supervisor.py).

The supervisor's threads are exercised by calling their target methods
directly against fake worker processes, so no real subprocess is spawned.
"""
import io
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

# Importing MCP_Server runs its __init__, which imports server.py and with it
# mcp.server.fastmcp. Stub mcp like the other server tests so this file also
# runs on its own, not only after another module has installed the stub.
if "MCP_Server.server" not in sys.modules:
    _mock_fastmcp = MagicMock()
    _mock_fastmcp.FastMCP.return_value.tool.return_value = lambda fn: fn
    sys.modules["mcp"] = MagicMock()
    sys.modules["mcp.server"] = MagicMock()
    sys.modules["mcp.server.fastmcp"] = _mock_fastmcp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from MCP_Server import supervisor as sup  # noqa: E402

# ── fakes ────────────────────────────────────────────────────────────


class _FakeStdin(io.BytesIO):
    """Worker stdin that records writes and can be made to fail."""

    def __init__(self, fail_with=None):
        super().__init__()
        self.fail_with = fail_with
        self.closed_by_test = False

    def write(self, data):
        if self.fail_with is not None:
            raise self.fail_with
        return super().write(data)

    def close(self):
        self.closed_by_test = True


class _FakeProc:
    def __init__(self, pid=4242, rc=None, stdout_lines=(), stdin=None):
        self.pid = pid
        self._rc = rc
        self.stdin = stdin if stdin is not None else _FakeStdin()
        self.stdout = io.BytesIO(b"".join(stdout_lines))
        self.wait_calls = []
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self._rc

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _line(obj):
    return (json.dumps(obj) + "\n").encode("utf-8")


def _host_lines(sup_obj):
    data = sup_obj.host_out.getvalue().decode("utf-8")
    return [json.loads(x) for x in data.splitlines() if x]


@pytest.fixture
def logger():
    log = logging.getLogger("test-supervisor")
    log.handlers = []
    log.propagate = False
    return log


@pytest.fixture
def supervisor(logger):
    s = sup.Supervisor(["python", "worker.py"], logger, cwd="/tmp")
    s.host_in = io.BytesIO()
    s.host_out = io.BytesIO()
    return s


# ── logging helpers ──────────────────────────────────────────────────


class TestLogging:
    def test_default_log_path_is_repo_logs_dir(self):
        path = sup.default_log_path()
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(sup.__file__)))
        assert path == os.path.join(repo_root, "logs", "ableton-mcp.log")

    def test_setup_file_logging_writes_to_env_path(self, tmp_path, monkeypatch):
        log_file = tmp_path / "nested" / "mcp.log"
        monkeypatch.setenv(sup.LOG_ENV, str(log_file))
        name = "test-setup-file-logging"
        logging.getLogger(name).handlers = []

        log = sup.setup_file_logging(name)
        log.propagate = False
        log.info("hello from test")
        for h in log.handlers:
            h.flush()

        assert log.level == logging.INFO
        assert log_file.exists()
        assert "hello from test" in log_file.read_text()
        for h in list(log.handlers):
            h.close()
            log.removeHandler(h)

    def test_setup_file_logging_falls_back_when_unwritable(self, monkeypatch):
        monkeypatch.setenv(sup.LOG_ENV, "/unwritable/dir/mcp.log")
        name = "test-setup-file-logging-fallback"
        logging.getLogger(name).handlers = []
        with patch.object(sup.os, "makedirs", side_effect=OSError("read-only")):
            log = sup.setup_file_logging(name)
        assert log.handlers == []


class TestInstallSignalLogging:
    def _capture(self, logger, **kwargs):
        installed = {}

        def fake_signal(sig, handler):
            installed[sig] = handler

        with patch.object(sup.signal, "signal", side_effect=fake_signal):
            sup.install_signal_logging(logger, **kwargs)
        return installed

    def test_installs_handler_for_each_signal(self, logger):
        installed = self._capture(logger, signals=(signal.SIGTERM, signal.SIGHUP))
        assert set(installed) == {signal.SIGTERM, signal.SIGHUP}

    def test_handler_logs_flushes_runs_hook_and_reraises(self, logger):
        handler_obj = MagicMock()
        logger.addHandler(handler_obj)
        handler_obj.level = logging.NOTSET
        before_exit = MagicMock()
        installed = self._capture(logger, signals=(signal.SIGTERM,), before_exit=before_exit)

        with patch.object(sup.signal, "signal") as mock_signal, \
                patch.object(sup.os, "kill") as mock_kill, \
                patch.object(sup.os, "getpid", return_value=99):
            installed[signal.SIGTERM](signal.SIGTERM, None)

        handler_obj.flush.assert_called()
        before_exit.assert_called_once_with(signal.SIGTERM)
        mock_signal.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
        mock_kill.assert_called_once_with(99, signal.SIGTERM)

    def test_handler_survives_unknown_signal_and_failing_hooks(self, logger, caplog):
        bad_handler = MagicMock()
        bad_handler.level = logging.NOTSET
        bad_handler.flush.side_effect = RuntimeError("flush failed")
        logger.addHandler(bad_handler)
        installed = self._capture(
            logger, signals=(signal.SIGTERM,),
            before_exit=MagicMock(side_effect=RuntimeError("hook failed")))

        with patch.object(sup.signal, "signal"), patch.object(sup.os, "kill") as mock_kill:
            installed[signal.SIGTERM](12345, None)

        assert mock_kill.call_args[0][1] == 12345
        logged = [c.args[0] for c in bad_handler.handle.call_args_list]
        assert any("12345" in r.getMessage() for r in logged)

    def test_registration_errors_are_ignored(self, logger):
        with patch.object(sup.signal, "signal", side_effect=ValueError("not main thread")):
            sup.install_signal_logging(logger, signals=(signal.SIGTERM,))


# ── message classification ───────────────────────────────────────────


class TestParsing:
    @pytest.mark.parametrize("raw,expected", [
        (b'{"id": 1, "method": "x"}', {"id": 1, "method": "x"}),
        (b"not json", None),
        (b"[1, 2]", None),
        (b"\xff\xfe", None),
    ])
    def test_parse(self, raw, expected):
        assert sup._parse(raw) == expected

    @pytest.mark.parametrize("msg,is_resp,is_req", [
        ({"id": 1, "result": {}}, True, False),
        ({"id": 1, "error": {}}, True, False),
        ({"id": 1, "method": "tools/call"}, False, True),
        ({"method": "notifications/initialized"}, False, False),
    ])
    def test_classification(self, msg, is_resp, is_req):
        assert sup._is_response(msg) is is_resp
        assert sup._is_request(msg) is is_req


# ── host side ────────────────────────────────────────────────────────


class TestHostWrites:
    def test_write_host_appends_newline(self, supervisor):
        supervisor.write_host(b'{"a": 1}')
        assert supervisor.host_out.getvalue() == b'{"a": 1}\n'

    def test_write_host_keeps_existing_newline(self, supervisor):
        supervisor.write_host(b'{"a": 1}\n')
        assert supervisor.host_out.getvalue() == b'{"a": 1}\n'

    def test_write_host_ignores_broken_pipe(self, supervisor):
        supervisor.host_out = MagicMock()
        supervisor.host_out.write.side_effect = BrokenPipeError()
        supervisor.write_host(b"x")  # must not raise

    def test_fail_request_sends_jsonrpc_error(self, supervisor):
        supervisor.fail_request(7, "boom")
        assert _host_lines(supervisor) == [{
            "jsonrpc": "2.0", "id": 7,
            "error": {"code": -32000, "message": "boom"},
        }]


# ── worker side ──────────────────────────────────────────────────────


class TestSpawn:
    def test_spawn_starts_process_and_threads(self, supervisor):
        proc = _FakeProc(pid=11)
        with patch.object(sup.subprocess, "Popen", return_value=proc) as popen, \
                patch.object(sup.threading, "Thread") as thread_cls:
            result = supervisor.spawn()

        assert result is proc
        assert supervisor.generation == 1
        popen.assert_called_once()
        assert popen.call_args.args[0] == ["python", "worker.py"]
        assert popen.call_args.kwargs["cwd"] == "/tmp"
        targets = [c.kwargs["target"] for c in thread_cls.call_args_list]
        assert targets == [supervisor.pump_worker_stdout, supervisor.watch_worker]
        assert all(c.kwargs["args"] == (proc, 1) for c in thread_cls.call_args_list)
        assert thread_cls.return_value.start.call_count == 2


class TestSendToWorker:
    def test_forwards_request_and_tracks_pending(self, supervisor):
        proc = _FakeProc()
        supervisor.proc = proc
        line = _line({"jsonrpc": "2.0", "id": 3, "method": "tools/call"})

        supervisor.send_to_worker(line, sup._parse(line))

        assert proc.stdin.getvalue() == line
        assert supervisor.pending == {3: True}

    def test_forwards_notification_without_tracking(self, supervisor):
        proc = _FakeProc()
        supervisor.proc = proc
        line = _line({"method": "notifications/initialized"})

        supervisor.send_to_worker(line, sup._parse(line))

        assert proc.stdin.getvalue() == line
        assert supervisor.pending == {}

    @pytest.mark.parametrize("proc", [None, _FakeProc(rc=1)])
    def test_request_fails_fast_when_worker_down(self, supervisor, proc):
        supervisor.proc = proc
        line = _line({"id": 5, "method": "tools/call"})

        supervisor.send_to_worker(line, sup._parse(line))

        assert _host_lines(supervisor)[0]["id"] == 5
        assert "not running" in _host_lines(supervisor)[0]["error"]["message"]

    def test_notification_dropped_when_worker_down(self, supervisor):
        supervisor.proc = None
        supervisor.send_to_worker(b"garbage\n", None)
        assert supervisor.host_out.getvalue() == b""

    def test_write_failure_fails_request(self, supervisor):
        proc = _FakeProc(stdin=_FakeStdin(fail_with=BrokenPipeError("gone")))
        supervisor.proc = proc
        line = _line({"id": 9, "method": "tools/call"})

        supervisor.send_to_worker(line, sup._parse(line))

        assert supervisor.pending == {}
        msg = _host_lines(supervisor)[0]
        assert msg["id"] == 9
        assert "request not delivered" in msg["error"]["message"]

    def test_write_failure_for_notification_is_silent(self, supervisor):
        supervisor.proc = _FakeProc(stdin=_FakeStdin(fail_with=OSError("gone")))
        supervisor.send_to_worker(b"{}\n", {})
        assert supervisor.host_out.getvalue() == b""

    def test_waits_while_replaying(self, supervisor):
        proc = _FakeProc()
        supervisor.proc = proc
        supervisor.replaying = True
        line = _line({"id": 1, "method": "ping"})

        def finish_replay():
            with supervisor.state:
                supervisor.replaying = False
                supervisor.state.notify_all()

        timer = threading.Timer(0.05, finish_replay)
        timer.start()
        supervisor.send_to_worker(line, sup._parse(line))
        timer.join()

        assert proc.stdin.getvalue() == line


class TestPumpWorkerStdout:
    def test_forwards_lines_and_clears_pending(self, supervisor):
        supervisor.generation = 1
        supervisor.pending = {1: True, 2: True}
        lines = [_line({"id": 1, "result": {}}), b"not json\n",
                 _line({"method": "notifications/progress"})]
        proc = _FakeProc(stdout_lines=lines)

        supervisor.pump_worker_stdout(proc, 1)

        assert supervisor.host_out.getvalue() == b"".join(lines)
        assert supervisor.pending == {2: True}

    def test_swallows_duplicate_initialize_response_during_replay(self, supervisor):
        supervisor.generation = 2
        supervisor.replaying = True
        supervisor.init_id = 0
        lines = [_line({"id": 0, "result": {"protocolVersion": "x"}}),
                 _line({"id": 4, "result": {}})]
        proc = _FakeProc(stdout_lines=lines)

        supervisor.pump_worker_stdout(proc, 2)

        assert supervisor.handshake_done.is_set()
        assert supervisor.host_out.getvalue() == lines[1]

    def test_initialize_id_from_stale_generation_is_forwarded(self, supervisor):
        supervisor.generation = 3
        supervisor.replaying = True
        supervisor.init_id = 0
        lines = [_line({"id": 0, "result": {}})]

        supervisor.pump_worker_stdout(_FakeProc(stdout_lines=lines), 2)

        assert not supervisor.handshake_done.is_set()
        assert supervisor.host_out.getvalue() == lines[0]


class TestWatchWorker:
    def test_stale_generation_is_ignored(self, supervisor):
        supervisor.generation = 2
        with patch.object(supervisor, "_respawn_and_replay") as respawn:
            supervisor.watch_worker(_FakeProc(rc=1), 1)
        respawn.assert_not_called()

    def test_exit_during_shutdown_is_logged_only(self, supervisor):
        supervisor.generation = 1
        supervisor.stopping = True
        with patch.object(supervisor, "_respawn_and_replay") as respawn:
            supervisor.watch_worker(_FakeProc(rc=0), 1)
        respawn.assert_not_called()

    @pytest.mark.parametrize("rc,why", [
        (-signal.SIGKILL, "killed by SIGKILL"),
        (-999, "killed by signal 999"),
        (3, "exit code 3"),
    ])
    def test_unexpected_exit_fails_pending_and_respawns(self, supervisor, rc, why):
        supervisor.generation = 1
        supervisor.pending = {8: True}
        with patch.object(supervisor, "_respawn_and_replay") as respawn, \
                patch.object(sup.time, "sleep") as sleep:
            supervisor.watch_worker(_FakeProc(rc=rc), 1)

        msg = _host_lines(supervisor)[0]
        assert msg["id"] == 8
        assert why in msg["error"]["message"]
        assert supervisor.pending == {}
        assert supervisor.replaying is True
        assert supervisor.proc is None
        sleep.assert_called_once_with(sup.RESPAWN_DELAY_SECONDS)
        respawn.assert_called_once()

    def test_rapid_exits_give_up(self, supervisor):
        supervisor.generation = 1
        supervisor.pending = {1: True}
        now = 1000.0
        supervisor.exit_times = [now - 1, now - 2]
        flushing = MagicMock()
        flushing.level = logging.NOTSET
        flushing.flush.side_effect = RuntimeError("cannot flush")
        supervisor.log.addHandler(flushing)

        with patch.object(sup.time, "monotonic", return_value=now), \
                patch.object(sup.os, "_exit", side_effect=SystemExit(1)) as hard_exit:
            with pytest.raises(SystemExit):
                supervisor.watch_worker(_FakeProc(rc=1), 1)

        hard_exit.assert_called_once_with(1)
        assert supervisor.stopping is True
        assert "keeps crashing" in _host_lines(supervisor)[0]["error"]["message"]

    def test_old_exits_outside_window_are_forgotten(self, supervisor):
        supervisor.generation = 1
        now = 1000.0
        supervisor.exit_times = [now - sup.RAPID_EXIT_WINDOW_SECONDS - 1] * 5
        with patch.object(sup.time, "monotonic", return_value=now), \
                patch.object(sup.time, "sleep"), \
                patch.object(supervisor, "_respawn_and_replay") as respawn:
            supervisor.watch_worker(_FakeProc(rc=1), 1)
        assert supervisor.exit_times == [now]
        respawn.assert_called_once()


class TestRespawnAndReplay:
    def test_does_nothing_when_stopping(self, supervisor):
        supervisor.stopping = True
        supervisor.replaying = True
        with patch.object(supervisor, "spawn") as spawn:
            supervisor._respawn_and_replay()
        spawn.assert_not_called()
        assert supervisor.replaying is False

    def test_spawn_failure_exits(self, supervisor):
        with patch.object(supervisor, "spawn", side_effect=OSError("no python")), \
                patch.object(sup.os, "_exit", side_effect=SystemExit(1)) as hard_exit:
            with pytest.raises(SystemExit):
                supervisor._respawn_and_replay()
        hard_exit.assert_called_once_with(1)
        assert supervisor.stopping is True

    def test_no_handshake_to_replay(self, supervisor):
        proc = _FakeProc()
        supervisor.replaying = True
        with patch.object(supervisor, "spawn", return_value=proc):
            supervisor._respawn_and_replay()
        assert supervisor.proc is proc
        assert supervisor.replaying is False
        assert proc.stdin.getvalue() == b""

    def test_replays_initialize_and_initialized(self, supervisor):
        proc = _FakeProc()
        supervisor.init_request = _line({"id": 0, "method": "initialize"})
        supervisor.initialized_notice = _line({"method": "notifications/initialized"})
        supervisor.handshake_done.set()
        supervisor.replaying = True

        with patch.object(supervisor, "spawn", return_value=proc):
            supervisor._respawn_and_replay()

        assert proc.stdin.getvalue() == supervisor.init_request + supervisor.initialized_notice
        assert supervisor.replaying is False

    def test_handshake_timeout_still_resumes(self, supervisor):
        proc = _FakeProc()
        supervisor.init_request = _line({"id": 0, "method": "initialize"})
        supervisor.initialized_notice = _line({"method": "notifications/initialized"})
        supervisor.replaying = True

        with patch.object(supervisor, "spawn", return_value=proc), \
                patch.object(supervisor.handshake_done, "wait", return_value=False):
            supervisor._respawn_and_replay()

        assert proc.stdin.getvalue() == supervisor.init_request
        assert supervisor.replaying is False

    def test_replay_write_failure_still_resumes(self, supervisor):
        proc = _FakeProc(stdin=_FakeStdin(fail_with=BrokenPipeError("dead")))
        supervisor.init_request = _line({"id": 0, "method": "initialize"})
        supervisor.replaying = True
        with patch.object(supervisor, "spawn", return_value=proc):
            supervisor._respawn_and_replay()
        assert supervisor.replaying is False


# ── lifecycle ────────────────────────────────────────────────────────


class TestShutdownWorker:
    def test_no_worker(self, supervisor):
        supervisor.shutdown_worker("test")
        assert supervisor.stopping is True

    def test_already_exited(self, supervisor):
        proc = _FakeProc(rc=0)
        supervisor.proc = proc
        supervisor.shutdown_worker("test")
        assert proc.stdin.closed_by_test is False

    def test_clean_exit_after_stdin_close(self, supervisor):
        proc = _FakeProc()
        supervisor.proc = proc
        supervisor.shutdown_worker("test")
        assert proc.stdin.closed_by_test is True
        assert proc.wait_calls == [sup.SHUTDOWN_GRACE_SECONDS]
        assert proc.terminated is False

    def test_terminates_when_grace_expires(self, supervisor):
        proc = _FakeProc()
        proc.stdin = MagicMock()
        proc.stdin.close.side_effect = OSError("already closed")
        calls = []

        def wait(timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("w", timeout)
            return 0

        proc.wait = wait
        supervisor.proc = proc
        supervisor.shutdown_worker("test")
        assert proc.terminated is True
        assert calls == [sup.SHUTDOWN_GRACE_SECONDS, 2.0]
        assert proc.killed is False

    def test_kills_when_terminate_fails(self, supervisor):
        proc = _FakeProc()
        proc.wait = MagicMock(side_effect=subprocess.TimeoutExpired("w", 1))
        proc.terminate = MagicMock(side_effect=OSError("no such process"))
        proc.kill = MagicMock(side_effect=OSError("still no"))
        supervisor.proc = proc
        supervisor.shutdown_worker("test")
        proc.kill.assert_called_once()


class TestRun:
    def test_forwards_host_lines_records_handshake_and_shuts_down(self, supervisor):
        init = _line({"jsonrpc": "2.0", "id": 0, "method": "initialize"})
        notice = _line({"jsonrpc": "2.0", "method": "notifications/initialized"})
        call = _line({"jsonrpc": "2.0", "id": 1, "method": "tools/call"})
        supervisor.host_in = io.BytesIO(init + b"\n" + notice + b"junk\n" + call)
        proc = _FakeProc()

        with patch.object(sup, "install_signal_logging") as install, \
                patch.object(supervisor, "spawn", return_value=proc), \
                patch.object(supervisor, "shutdown_worker") as shutdown:
            rc = supervisor.run()

        assert rc == 0
        install.assert_called_once()
        assert supervisor.init_request == init
        assert supervisor.init_id == 0
        assert supervisor.initialized_notice == notice
        assert proc.stdin.getvalue() == init + notice + b"junk\n" + call
        shutdown.assert_called_once_with("host closed stdin")

        before_exit = install.call_args.kwargs["before_exit"]
        with patch.object(supervisor, "shutdown_worker") as shutdown2:
            before_exit(15)
        shutdown2.assert_called_once_with("signal 15")

    def test_host_stdin_error_still_shuts_down(self, supervisor):
        supervisor.host_in = MagicMock()
        supervisor.host_in.readline.side_effect = OSError("bad fd")
        with patch.object(sup, "install_signal_logging"), \
                patch.object(supervisor, "spawn", return_value=_FakeProc()), \
                patch.object(supervisor, "shutdown_worker") as shutdown:
            assert supervisor.run() == 0
        shutdown.assert_called_once()


def test_run_supervisor_builds_supervisor(logger):
    with patch.object(sup, "setup_file_logging", return_value=logger) as setup, \
            patch.object(sup.Supervisor, "run", return_value=0) as run:
        assert sup.run_supervisor(["w"], cwd="/x") == 0
    setup.assert_called_once_with("AbletonMCPSupervisor")
    run.assert_called_once()
