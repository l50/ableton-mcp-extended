"""Unit-test guards. (The osascript and screencapture block is in tests/conftest.py.)"""
import pytest


@pytest.fixture(autouse=True)
def _no_pending_export_between_tests(monkeypatch):
    """Each test starts with no export left pending by an earlier one."""
    from MCP_Server import live_export

    monkeypatch.setattr(live_export, "_pending_baseline", None)
