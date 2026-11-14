"""Session-view clip tools: create, fill with notes, rename, fire and stop."""

from typing import Any

from mcp.server.fastmcp import Context

from MCP_Server import connection
from MCP_Server.conversions import to_zero_based
from MCP_Server.tools.base import ableton_tool


def _send_clip_command(command_type: str, track_index: int, clip_index: int, **params: Any) -> Any:
    """Send a command addressed to one session clip slot (1-based in)."""
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    ci = to_zero_based(clip_index, "clip_index")
    return ableton.send_command(command_type, {"track_index": ti, "clip_index": ci, **params})


@ableton_tool("creating clip")
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0) -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip slot number (1-based).
    - length: The length of the clip in beats (default: 4.0).
    """
    _send_clip_command("create_clip", track_index, clip_index, length=length)
    return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"


@ableton_tool("adding notes to clip")
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: list[dict[str, int | float | bool]],
) -> str:
    """
    Add MIDI notes to a clip.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip slot number (1-based).
    - notes: List of note dictionaries, each with pitch, start_time, duration, velocity, and mute.
    """
    _send_clip_command("add_notes_to_clip", track_index, clip_index, notes=notes)
    return f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"


@ableton_tool("setting clip name")
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str) -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip slot number (1-based).
    - name: The new name for the clip.
    """
    _send_clip_command("set_clip_name", track_index, clip_index, name=name)
    return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"


@ableton_tool("firing clip")
def fire_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip slot number (1-based).
    """
    _send_clip_command("fire_clip", track_index, clip_index)
    return f"Started playing clip at track {track_index}, slot {clip_index}"


@ableton_tool("stopping clip")
def stop_clip(ctx: Context, track_index: int, clip_index: int) -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip slot number (1-based).
    """
    _send_clip_command("stop_clip", track_index, clip_index)
    return f"Stopped clip at track {track_index}, slot {clip_index}"
