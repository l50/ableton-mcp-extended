"""Track tools: info, creation, naming, mixer volume/panning and deletion."""

import json
import math

from mcp.server.fastmcp import Context

from MCP_Server import connection
from MCP_Server.conversions import to_zero_based
from MCP_Server.tools.base import ableton_tool

# Live's fader taper, measured from Live 12's own readout: (normalized volume, dB).
# Above 0.4 the taper is linear at 40 dB per unit; below it curves down to -inf.
FADER_TAPER = (
    (0.05, -57.2), (0.1, -48.6), (0.15, -41.0), (0.2, -34.4), (0.25, -28.8),
    (0.3, -24.2), (0.35, -20.6), (0.4, -18.0), (1.0, 6.0),
)
# Pan values closer to zero than this are reported as "center".
PAN_CENTER_TOLERANCE = 0.01


def _format_db(volume: float, display: str | None = None) -> str:
    """Return the fader level in dB as Live shows it.

    *display* is Live's own readout for the fader (from the Remote Script); it
    is used verbatim when present. Otherwise fall back to Live's measured fader
    taper: 0.85 is 0 dB and 1.0 is +6 dB. Below the first measured point only an
    upper bound is reported.
    """
    if display:
        return display.strip()
    if volume <= 0:
        return "-inf dB"
    if volume < FADER_TAPER[0][0]:
        return f"< {FADER_TAPER[0][1]:+.1f} dB"
    volume = min(volume, 1.0)
    for (v0, db0), (v1, db1) in zip(FADER_TAPER, FADER_TAPER[1:]):
        if volume <= v1:
            return f"{db0 + (volume - v0) / (v1 - v0) * (db1 - db0):+.1f} dB"
    return f"{FADER_TAPER[-1][1]:+.1f} dB"


def _format_pan(pan: float) -> str:
    if abs(pan) < PAN_CENTER_TOLERANCE:
        return "center"
    return f"{abs(pan):.2f} {'L' if pan < 0 else 'R'}"


def _find_track_by_name(
    ableton: connection.AbletonConnection, name: str, track_count: int
) -> int | None:
    """Return the 0-based index of the first track named *name* (any case)."""
    for index in range(track_count):
        track = ableton.send_command("get_track_info", {"track_index": index})
        if track.get("name", "").lower() == name.lower():
            return index
    return None


@ableton_tool("getting track info")
def get_track_info(ctx: Context, track_index: int, include_clip_slots: bool = True) -> str:
    """
    Get detailed information about a specific track in Ableton.

    Besides name, mixer state, clip slots and devices, the reply includes
    input/output routing, monitoring, can_be_armed, implicit_arm, group and
    fold state (is_foldable, fold_state, is_grouped, group_track with its
    1-based track_index), is_visible, muted_via_solo, is_frozen, color,
    arrangement_clip_count and take_lane_count (null where Live lacks it).

    Parameters:
    - track_index: Track number (1-based).
    - include_clip_slots: False leaves out the session clip slots.
    """
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    result = ableton.send_command(
        "get_track_info",
        {"track_index": ti, "include_clip_slots": bool(include_clip_slots)},
    )
    group = result.get("group_track") if isinstance(result, dict) else None
    if isinstance(group, dict) and isinstance(group.get("index"), int):
        result["group_track"] = {"track_index": group["index"] + 1, "name": group.get("name")}
    return json.dumps(result, indent=2)


@ableton_tool("creating MIDI track")
def create_midi_track(ctx: Context, index: int = -1) -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    """
    result = connection.get_ableton_connection().send_command("create_midi_track", {"index": index})
    return f"Created new MIDI track: {result.get('name', 'unknown')}"


@ableton_tool("setting track name")
def set_track_name(ctx: Context, track_index: int, name: str) -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: Track number (1-based).
    - name: The new name for the track.
    """
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    result = ableton.send_command("set_track_name", {"track_index": ti, "name": name})
    return f"Renamed track to: {result.get('name', name)}"


@ableton_tool("getting track volume")
def get_track_volume(ctx: Context, track_index: int) -> str:
    """Get the current fader volume and panning for a track.

    Returns the raw normalized value, its min/max range, and the panning.
    Volume 0.85 = 0 dB unity gain, 1.0 = +6 dB. The dB shown is Live's own
    fader readout. Use this before set_track_volume to
    understand the current state.

    Parameters:
    - track_index: Track number (1-based). Return tracks come after session tracks.
    """
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    result = ableton.send_command("get_track_volume", {"track_index": ti})
    vol = result.get("volume", 0)
    pan = result.get("panning", 0)
    name = result.get("track_name", "?")
    vol_min = result.get("volume_min", 0)
    vol_max = result.get("volume_max", 1)
    return (
        f"Track '{name}':\n"
        f"  Volume: {vol:.4f} (range {vol_min:.2f}–{vol_max:.2f}) ≈ {_format_db(vol, result.get('volume_display'))}\n"
        f"  Panning: {pan:.4f} ({_format_pan(pan)})\n"
        f"  Unity gain (0 dB) = 0.85, top of fader (+6 dB) = 1.0"
    )


@ableton_tool("setting track volume")
def set_track_volume(ctx: Context, track_index: int, volume: float) -> str:
    """Set the mixer fader volume for a track directly.

    This controls the actual track fader, not any device parameter.

    Volume scale (normalized):
      0.0   = silence
      0.85  = 0 dB (unity gain, Ableton's default fader position)
      1.0   = maximum (~+6 dB)

    Parameters:
    - track_index: Track number (1-based). Return tracks come after session tracks.
    - volume: Normalized volume 0.0–1.0. Use 0.85 for unity (0 dB).
    """
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    result = ableton.send_command("set_track_volume", {"track_index": ti, "volume": volume})
    name = result.get("track_name", "?")
    vol = result.get("volume", volume)
    return f"Set '{name}' fader to {vol:.4f} (≈ {_format_db(vol, result.get('volume_display'))})"


@ableton_tool("setting track panning")
def set_track_panning(ctx: Context, track_index: int, panning: float) -> str:
    """Set the mixer panning for a track.

    Parameters:
    - track_index: Track number (1-based).
    - panning: -1.0 = full left, 0.0 = center, +1.0 = full right.
    """
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    result = ableton.send_command("set_track_panning", {"track_index": ti, "panning": panning})
    name = result.get("track_name", "?")
    pan = result.get("panning", panning)
    return f"Set '{name}' panning to {pan:.4f} ({_format_pan(pan)})"


@ableton_tool("checking track deletion status")
def get_track_deletion_status(ctx: Context) -> str:
    """Check whether session tracks can be deleted right now.

    Returns a quick safety summary so agents can avoid attempting deletes
    when Ableton's minimum-track constraint would block them.
    """
    info = connection.get_ableton_connection().send_command("get_session_info")
    track_count = info.get("track_count", 0)
    if track_count <= 1:
        return (
            "Track deletion blocked: 1 session track remaining. "
            "Ableton requires at least one session track. "
            "Create a new track before deleting."
        )
    return (
        f"Track deletion available: {track_count} session tracks currently exist. "
        f"You can delete up to {track_count - 1} more track(s) before hitting Ableton's "
        "minimum-track limit."
    )


@ableton_tool("deleting track")
def delete_track(
    ctx: Context,
    track_index: int = 0,
    track_name: str = "",
) -> str:
    """Delete a track from the Ableton session.

    Parameters:
    - track_index: Track number (1-based). Use 0 to resolve by name instead.
    - track_name: Track name (alternative to track_index). If both are given, track_index takes priority.
    """
    ableton = connection.get_ableton_connection()
    track_count = ableton.send_command("get_session_info").get("track_count", 0)

    # Ableton requires at least one session track.
    if track_count <= 1:
        return (
            "Error: Cannot delete the last remaining session track. "
            "Ableton must always have at least one track. "
            "Create a new track before deleting."
        )

    if track_index > 0:
        ti = to_zero_based(track_index, "track_index")
    elif not track_name:
        return "Error: provide either track_index (1-based) or track_name."
    else:
        ti = _find_track_by_name(ableton, track_name, track_count)
        if ti is None:
            return f"Error: No track named '{track_name}' found."

    result = ableton.send_command("delete_track", {"track_index": ti})
    return "Deleted track '{}'. {} tracks remaining.".format(
        result.get("deleted_track", "unknown"),
        result.get("remaining_tracks", "?"),
    )
