"""No test can reach the screen: osascript and screencapture launches fail loudly.

The UI tools drive Live through osascript. A unit run once reached them from a
module-scoped fixture, before the per-test guard of the time was active, on the
owner's machine. tests/conftest.py now blocks every launch of osascript or
screencapture for the whole session; these tests prove it, including from a
module-scoped fixture, and that a UI tool called without a fake driver raises.

The launches below are harmless if the block ever failed: a trivial AppleScript
expression, and screencapture's usage text, which captures nothing.
"""

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from MCP_Server import connection, live_export, live_save, live_ui_state, tools  # noqa: E402
from tests.conftest import ScreenAccessBlocked, launches_screen_tool  # noqa: E402

CTX = MagicMock()


@pytest.fixture(scope="module")
def launch_from_a_module_fixture():
    """A module-scoped fixture, the kind that ran before the old per-test guard."""
    try:
        subprocess.run(["osascript", "-e", "1"], capture_output=True, timeout=5)
    except ScreenAccessBlocked as e:
        return e
    return None


def test_a_module_scoped_fixture_could_not_launch_osascript(launch_from_a_module_fixture):
    assert isinstance(launch_from_a_module_fixture, ScreenAccessBlocked)


def test_the_block_is_not_an_exception_so_a_tool_cannot_swallow_it():
    assert not issubclass(ScreenAccessBlocked, Exception)


LAUNCHES = {
    "run": lambda: subprocess.run(["osascript", "-e", "1"]),
    "run_absolute_path": lambda: subprocess.run(["/usr/bin/osascript", "-e", "1"]),
    "run_jxa": lambda: subprocess.run(["osascript", "-l", "JavaScript", "-e", "1"]),
    "Popen": lambda: subprocess.Popen(["osascript", "-e", "1"]),
    "call": lambda: subprocess.call(["osascript", "-e", "1"]),
    "check_output": lambda: subprocess.check_output(["osascript", "-e", "1"]),
    "shell_string": lambda: subprocess.run("osascript -e 1", shell=True),
    "shell_wrapper": lambda: subprocess.run(["sh", "-c", "echo hi; osascript -e 1"]),
    "env_wrapper": lambda: subprocess.run(["env", "osascript", "-e", "1"]),
    "screencapture": lambda: subprocess.run(["screencapture", "-h"]),
    "screencapture_path": lambda: subprocess.run(["/usr/sbin/screencapture", "-h"]),
    "screencapture_shell": lambda: subprocess.run("screencapture -h", shell=True),
    "os_system": lambda: os.system("osascript -e 1"),
    "os_popen": lambda: os.popen("osascript -e 1"),
    "posix_spawn": lambda: os.posix_spawn("/usr/bin/osascript", ["osascript", "-e", "1"], {}),
    "execv": lambda: os.execv("/usr/bin/osascript", ["osascript", "-e", "1"]),
    "spawnv": lambda: os.spawnv(os.P_WAIT, "/usr/bin/osascript", ["osascript", "-e", "1"]),
}


@pytest.mark.parametrize("launch", list(LAUNCHES.values()), ids=list(LAUNCHES))
def test_every_way_to_launch_a_screen_tool_raises(launch):
    with pytest.raises(ScreenAccessBlocked):
        launch()


@pytest.mark.parametrize("argv, shell", [
    (["echo", "osascript"], False),            # the word as an argument is not a launch
    (["node", "-e", "var x = 'osascript';"], False),
    (["git", "status"], False),
    (["python3", "-c", "print('screencapture')"], False),
    (["sh", "-c", "echo hello"], False),
    ("echo hello", True),
    ([], False),
], ids=["echo_arg", "node_script", "git", "python_c", "sh_echo", "shell_echo", "empty"])
def test_ordinary_launches_are_not_blocked(argv, shell):
    assert launches_screen_tool(argv, shell=shell) is False


def test_an_ordinary_subprocess_still_runs():
    done = subprocess.run(["echo", "hello"], capture_output=True, text=True)
    assert done.stdout.strip() == "hello"


# ── UI tools without a fake driver raise instead of running ──────


def test_read_ui_state_without_a_fake_driver_raises_instead_of_running():
    with pytest.raises(ScreenAccessBlocked):
        live_ui_state.read_ui_state()


def test_the_real_driver_raises_instead_of_running():
    with pytest.raises(ScreenAccessBlocked):
        live_export.JXADriver().call("ui_state")


def test_clear_selection_without_a_fake_driver_raises_instead_of_running():
    with pytest.raises(ScreenAccessBlocked):
        live_ui_state.clear_selection()


def test_save_set_without_a_fake_driver_raises_instead_of_running():
    with pytest.raises(ScreenAccessBlocked):
        live_save.save_set()


def test_save_set_as_without_a_fake_driver_raises_instead_of_running(tmp_path):
    with pytest.raises(ScreenAccessBlocked):
        live_save.save_set_as(str(tmp_path / "A.als"))


@pytest.mark.parametrize("tool, kwargs", [
    (tools.ui_state.get_ui_state, {}),
    (tools.ui_state.clear_selection, {}),
    (tools.save.save_set, {}),
    (tools.save.save_set_as, {"path": "SET_PATH"}),
    (tools.export.export_audio, {"output_path": "WAV_PATH", "render_range": "selection"}),
    (tools.export.export_video, {"output_path": "WAV_PATH", "render_range": "selection"}),
], ids=["get_ui_state", "clear_selection", "save_set", "save_set_as", "export_audio", "export_video"])
def test_every_ui_tool_without_a_fake_driver_raises_instead_of_running(
        tool, kwargs, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    kwargs = {k: str(tmp_path / ("A.als" if v == "SET_PATH" else "mix.wav"))
              if v in ("SET_PATH", "WAV_PATH") else v for k, v in kwargs.items()}
    conn = MagicMock()
    conn.send_command.return_value = {}
    with patch.object(connection, "get_ableton_connection", return_value=conn):
        with pytest.raises(ScreenAccessBlocked):
            tool(CTX, **kwargs)
