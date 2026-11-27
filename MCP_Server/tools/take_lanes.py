"""Inspect take lanes without editing the main lane."""

import json
from typing import Any

from mcp.server.fastmcp import Context

from MCP_Server import connection
from MCP_Server.conversions import to_zero_based
from MCP_Server.tools.base import ableton_tool


def _index(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a 1-based integer")
    return to_zero_based(value, field)


def _reply_indices(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: item + 1
            if key in ("track_index", "lane_index", "clip_index")
            else _reply_indices(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_reply_indices(item) for item in value]
    return value


def _send(command: str, params: dict[str, int]) -> str:
    result = connection.get_ableton_connection().send_command(command, params)
    return json.dumps(_reply_indices(result))


@ableton_tool("listing take lanes")
def list_take_lanes(ctx: Context, track_index: int) -> str:
    """List a track's take lanes, including empty lanes and each lane's clips.

    Track, lane and clip indices are 1-based. Each clip has name, start and
    end (absolute quarter-note beats), and file (audio path, null for MIDI).
    The main arrangement lane is excluded. Re-list after deleting material:
    lane and clip indices shift when items are removed.
    """
    return _send("get_take_lanes", {"track_index": _index(track_index, "track_index")})
