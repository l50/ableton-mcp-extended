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
    session,
    take_lanes,
    tracks,
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
)
