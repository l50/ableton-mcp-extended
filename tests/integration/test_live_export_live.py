"""Manual smoke test: a real Export Audio/Video through Live's UI.

This takes over the screen: it brings Live to the front, opens the Export
dialog, changes its settings (Live keeps them), and renders the current
Set's Arrangement to a temporary folder. Use a scratch Set, keep your hands
off the mouse and keyboard while it runs, and expect it to take as long as
the render does. It needs macOS, a running Live with the ableton-mcp Remote
Script, and Accessibility permission for the terminal running pytest:

    ABLETON_MCP_LIVE_TESTS=1 ABLETON_MCP_EXPORT_TESTS=1 \\
        uv run --extra test python -m pytest -m integration tests/integration/test_live_export_live.py -s

It is excluded by default (the "integration" marker) and also skips unless
both variables are set. It cannot run in CI.
"""
import json
import os
import sys

import pytest

pytestmark = pytest.mark.integration

if os.environ.get("ABLETON_MCP_LIVE_TESTS") != "1" or os.environ.get("ABLETON_MCP_EXPORT_TESTS") != "1":
    pytest.skip("set ABLETON_MCP_LIVE_TESTS=1 and ABLETON_MCP_EXPORT_TESTS=1 to run a real export",
                allow_module_level=True)
if sys.platform != "darwin":
    pytest.skip("Live export drives the macOS UI", allow_module_level=True)

from MCP_Server import server  # noqa: E402


@pytest.mark.parametrize("fmt,bit_depth", [("wav", 24), ("mp3", 24)])
def test_export_writes_verified_file(tmp_path, fmt, bit_depth):
    out = json.loads(server.export_audio(
        None,
        output_path=str(tmp_path / f"smoke.{fmt}"),
        format=fmt,
        sample_rate=44100,
        bit_depth=bit_depth,
        render_range="arrangement",
        render_timeout=600,
    ))
    print(json.dumps(out, indent=2))
    assert out["ok"] is True, out.get("error")
    assert os.path.getsize(out["output_path"]) > 0
    assert out["file_info"]["sample_rate"] == 44100
    assert out["length_verified"] is True, (out["expected_seconds"], out["rendered_seconds"])
