"""Session-wide tools: session info, tempo, transport and the main view."""

import json

from mcp.server.fastmcp import Context

from MCP_Server import connection
from MCP_Server.tools.base import ableton_tool


@ableton_tool("getting session info")
def get_session_info(ctx: Context) -> str:
    """Get detailed information about the current Ableton session"""
    result = connection.get_ableton_connection().send_command("get_session_info")
    return json.dumps(result, indent=2)


@ableton_tool("setting tempo")
def set_tempo(ctx: Context, tempo: float) -> str:
    """
    Set the tempo of the Ableton session.

    Parameters:
    - tempo: The new tempo in BPM
    """
    connection.get_ableton_connection().send_command("set_tempo", {"tempo": tempo})
    return f"Set tempo to {tempo} BPM"


@ableton_tool("starting playback")
def start_playback(ctx: Context) -> str:
    """Start playing the Ableton session."""
    connection.get_ableton_connection().send_command("start_playback")
    return "Started playback"


@ableton_tool("stopping playback")
def stop_playback(ctx: Context) -> str:
    """Stop playing the Ableton session."""
    connection.get_ableton_connection().send_command("stop_playback")
    return "Stopped playback"


@ableton_tool("setting view")
def set_ableton_view(ctx: Context, view: str = "Arranger") -> str:
    """Switch Ableton's main view.

    Parameters:
    - view: View name. Options: Arranger, Session, Detail, Detail/Clip,
            Detail/DeviceChain, Browser.
    """
    connection.get_ableton_connection().send_command("set_view", {"view_name": view})
    return f"Switched to {view} view"
