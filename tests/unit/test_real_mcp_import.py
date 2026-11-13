"""Smoke test: the server must load against the real, installed mcp package.

Every other server test replaces ``mcp`` with a MagicMock, so an incompatible
mcp release (mcp 2.x renamed FastMCP) would leave them all green while the
real ``ableton-mcp-extended`` entry point dies at import. This test imports
the real ``mcp.server.fastmcp`` and ``MCP_Server.server`` in a fresh
interpreter, because the stubs the other test modules put in ``sys.modules``
would otherwise shadow the real package. No Ableton Live instance is needed.
"""
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_PROBE = """
import asyncio, json
from mcp.server.fastmcp import FastMCP
from MCP_Server import server
assert isinstance(server.mcp, FastMCP), type(server.mcp)
tools = asyncio.run(server.mcp.list_tools())
print(json.dumps(sorted(t.name for t in tools)))
"""


def test_server_registers_tools_with_real_fastmcp():
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    tool_names = json.loads(proc.stdout.strip().splitlines()[-1])
    for name in ("get_session_info", "set_tempo", "manage_clip_automation",
                 "export_audio", "export_video"):
        assert name in tool_names
