"""The MCP tools, grouped by domain.

Importing this package registers every tool on ``MCP_Server.app.mcp``.
"""

from MCP_Server.tools import (
    arrangement,
    browser,
    clips,
    devices,
    export,
    recording,
    save,
    session,
    take_lanes,
    tracks,
    ui_state,
)

TOOL_MODULES = (
    session,
    tracks,
    recording,
    clips,
    browser,
    arrangement,
    devices,
    export,
    take_lanes,
    ui_state,
    save,
)
