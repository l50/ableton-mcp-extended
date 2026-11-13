"""Unit-test guards."""
import subprocess

import pytest

_real_run = subprocess.run


def _no_osascript(cmd, *args, **kwargs):
    if cmd and cmd[0] == "osascript":
        raise AssertionError("unit tests must not run osascript; it would drive the real Live UI")
    return _real_run(cmd, *args, **kwargs)


@pytest.fixture(autouse=True)
def _block_ui_scripting(monkeypatch):
    """Fail any unit test that would reach real macOS UI scripting."""
    monkeypatch.setattr(subprocess, "run", _no_osascript)
