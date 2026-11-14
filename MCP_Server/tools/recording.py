"""Recording-safety tools: mute/solo/arm, inputs, monitoring and batches.

These tools take a track by 1-based track_index, by track_name
(case-insensitive exact match) or, where returns make sense, by
return_track ("A", "B", ... or 1-based). Writes accept expect_name: if
the addressed track's name differs, the call fails and nothing changes.
Success is a compact JSON object with "ok": true and a "warnings" list;
failure is a string starting "Error <action>: ".
"""

import json
from typing import Any

from mcp.server.fastmcp import Context

from MCP_Server import connection
from MCP_Server.conversions import optional_to_zero_based
from MCP_Server.tools.base import ableton_tool

MONITORING_STATES = ("in", "auto", "off")
TRACK_REF_FIELDS = ("track_index", "track_name", "return_track", "expect_name")


def parse_return_track(value: Any) -> int | None:
    """Convert a return-track reference ("A", "b", "1", 1) to 0-based.

    Returns None when *value* is empty.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        if number >= 1:
            return number - 1
    elif len(text) == 1 and text.isalpha():
        return ord(text.upper()) - ord("A")
    raise ValueError(
        f"return_track must be a letter (A, B, ...) or a 1-based number, got '{value}'"
    )


def track_ref_params(
    track_index: Any = 0,
    track_name: str = "",
    return_track: Any = "",
    expect_name: str = "",
) -> dict[str, Any]:
    """Build the Remote Script track reference (0-based) from tool args."""
    params: dict[str, Any] = {}
    index = optional_to_zero_based(int(track_index or 0), "track_index")
    if index is not None:
        params["track_index"] = index
    if track_name:
        params["track_name"] = str(track_name)
    return_index = parse_return_track(return_track)
    if return_index is not None:
        params["return_track_index"] = return_index
    if len(params) != 1:
        raise ValueError("give exactly one of track_index (1-based), track_name or return_track")
    if expect_name:
        params["expect_name"] = str(expect_name)
    return params


def one_based_track_indices(value: Any) -> Any:
    """Return a copy of a Remote Script reply with every "index" made 1-based.

    The recording-safety replies use the "index" key only for track indices.
    """
    if isinstance(value, dict):
        return {
            key: (
                item + 1
                if key == "index" and isinstance(item, int) and not isinstance(item, bool)
                else one_based_track_indices(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [one_based_track_indices(item) for item in value]
    return value


def ok_json(result: Any) -> str:
    """Compact JSON success reply: {"ok": true, ..., "warnings": [...]}."""
    payload: dict[str, Any] = {"ok": True}
    payload.update(one_based_track_indices(result or {}))
    payload.setdefault("warnings", [])
    return json.dumps(payload, separators=(",", ":"))


def normalize_monitoring(state: Any) -> str:
    """Validate a monitoring state ("in", "auto" or "off", any case)."""
    value = str(state or "").strip().lower()
    if value not in MONITORING_STATES:
        raise ValueError(f"monitoring state must be one of in, auto, off (got '{state}')")
    return value


def convert_track_change(change: Any) -> dict[str, Any]:
    """Convert one set_tracks_state entry to Remote Script params."""
    if not isinstance(change, dict):
        raise ValueError("each change must be an object")
    params = {key: value for key, value in change.items() if key not in TRACK_REF_FIELDS}
    params.update(
        track_ref_params(
            change.get("track_index") or 0,
            change.get("track_name") or "",
            change.get("return_track") or "",
            change.get("expect_name") or "",
        )
    )
    return params


def _send(command_type: str, params: dict[str, Any] | None = None) -> str:
    """Send a recording-safety command and shape its reply with ok_json."""
    ableton = connection.get_ableton_connection()
    if params is None:
        return ok_json(ableton.send_command(command_type))
    return ok_json(ableton.send_command(command_type, params))


@ableton_tool("setting track mute")
def set_track_mute(
    ctx: Context,
    track_index: int = 0,
    mute: bool = True,
    track_name: str = "",
    return_track: str = "",
    expect_name: str = "",
) -> str:
    """Mute or unmute a track or return track.

    Parameters:
    - track_index: Track number (1-based, groups included). 0 = use track_name or return_track.
    - mute: True to mute, False to unmute.
    - track_name: Track name (case-insensitive exact match) instead of track_index.
    - return_track: Return track letter ("A", "B", ...) or 1-based number.
    - expect_name: If given, fail without changing anything unless the addressed track has this name.

    Returns JSON: {ok, track, mute, solo, muted_via_solo, warnings}.
    """
    params = track_ref_params(track_index, track_name, return_track, expect_name)
    params["mute"] = bool(mute)
    return _send("set_track_mute", params)


@ableton_tool("setting track solo")
def set_track_solo(
    ctx: Context,
    track_index: int = 0,
    solo: bool = True,
    exclusive: bool = False,
    track_name: str = "",
    return_track: str = "",
    expect_name: str = "",
) -> str:
    """Solo or unsolo a track or return track.

    Live's API bypasses exclusive solo, so soloing through it does not unsolo
    other tracks. Pass exclusive=True to unsolo every other track first.

    Parameters:
    - track_index: Track number (1-based, groups included). 0 = use track_name or return_track.
    - solo: True to solo, False to unsolo.
    - exclusive: With solo=True, unsolo all other tracks and return tracks.
    - track_name: Track name (case-insensitive exact match) instead of track_index.
    - return_track: Return track letter ("A", "B", ...) or 1-based number.
    - expect_name: If given, fail without changing anything unless the addressed track has this name.

    Returns JSON: {ok, track, solo, unsoloed, warnings}.
    """
    params = track_ref_params(track_index, track_name, return_track, expect_name)
    params["solo"] = bool(solo)
    params["exclusive"] = bool(exclusive)
    return _send("set_track_solo", params)


@ableton_tool("setting track arm")
def set_track_arm(
    ctx: Context,
    track_index: int = 0,
    arm: bool = True,
    exclusive: bool = False,
    track_name: str = "",
    expect_name: str = "",
) -> str:
    """Arm or disarm a track for recording.

    Group, return and master tracks have no Arm state and are refused. A
    frozen track cannot be armed (disarming it works). Check
    get_recording_status before recording.

    Parameters:
    - track_index: Track number (1-based, groups included). 0 = use track_name.
    - arm: True to arm, False to disarm.
    - exclusive: With arm=True, disarm every other armed track in the same step.
    - track_name: Track name (case-insensitive exact match) instead of track_index.
    - expect_name: If given, fail without changing anything unless the addressed track has this name.

    Returns JSON: {ok, track, arm, implicit_arm, input, monitoring, disarmed, warnings}.
    Warnings flag an armed track with input "No Input", monitoring "In",
    other tracks still armed, and implicit_arm (Live's auto-arm).
    """
    params = track_ref_params(track_index, track_name, "", expect_name)
    params["arm"] = bool(arm)
    params["exclusive"] = bool(exclusive)
    return _send("set_track_arm", params)


@ableton_tool("getting track routing options")
def get_track_routing_options(
    ctx: Context,
    track_index: int = 0,
    track_name: str = "",
    return_track: str = "",
) -> str:
    """List a track's current input/output routing and the names Live offers.

    Use the names returned here with set_track_input or set_tracks_state.
    Group and return tracks have no input, so their "input" is null.

    Parameters:
    - track_index: Track number (1-based, groups included). 0 = use track_name or return_track.
    - track_name: Track name (case-insensitive exact match) instead of track_index.
    - return_track: Return track letter ("A", "B", ...) or 1-based number.

    Returns JSON: {ok, track, input: {current: {type, channel}, types, channels}, output: {...}}.
    """
    return _send(
        "get_track_routing_options", track_ref_params(track_index, track_name, return_track)
    )


@ableton_tool("setting track input")
def set_track_input(
    ctx: Context,
    input_type: str,
    input_channel: str = "",
    track_index: int = 0,
    track_name: str = "",
    expect_name: str = "",
) -> str:
    """Set a track's input type and channel by the names Live shows.

    Names are matched case-insensitively against the options Live offers
    (see get_track_routing_options), e.g. input_type="Ext. In" with
    input_channel="4", or input_type="No Input" so a guide track cannot record.
    If the channel is not offered, the input is left unchanged.

    Parameters:
    - input_type: Input type name, e.g. "Ext. In", "No Input", or another track's name.
    - input_channel: Input channel name, e.g. "1", "1/2". Empty = Live's default for the type.
    - track_index: Track number (1-based, groups included). 0 = use track_name.
    - track_name: Track name (case-insensitive exact match) instead of track_index.
    - expect_name: If given, fail without changing anything unless the addressed track has this name.

    Returns JSON: {ok, track, input: {type, channel}, available_channels, arm, warnings}.
    """
    if not str(input_type or "").strip():
        raise ValueError("input_type is required")
    params = track_ref_params(track_index, track_name, "", expect_name)
    params["input_type"] = str(input_type)
    params["input_channel"] = str(input_channel or "")
    return _send("set_track_input", params)


@ableton_tool("setting track monitoring")
def set_track_monitoring(
    ctx: Context,
    state: str,
    track_index: int = 0,
    track_name: str = "",
    expect_name: str = "",
) -> str:
    """Set a track's monitoring to In, Auto or Off.

    Group, return, master and frozen tracks are refused.

    Parameters:
    - state: "in", "auto" or "off".
    - track_index: Track number (1-based, groups included). 0 = use track_name.
    - track_name: Track name (case-insensitive exact match) instead of track_index.
    - expect_name: If given, fail without changing anything unless the addressed track has this name.

    Returns JSON: {ok, track, monitoring, warnings}.
    """
    value = normalize_monitoring(state)
    params = track_ref_params(track_index, track_name, "", expect_name)
    params["state"] = value
    return _send("set_track_monitoring", params)


@ableton_tool("setting tracks state")
def set_tracks_state(
    ctx: Context,
    changes: list[dict[str, Any]],
    dry_run: bool = False,
) -> str:
    """Change several tracks' mute/solo/arm/input/monitoring in one validated batch.

    Every entry is checked before anything is written. Any problem rejects
    the whole batch and lists every problem, so a batch is never half
    applied; if Live rejects a write part-way, the earlier changes are
    restored. Use dry_run=True to show which tracks would change first.

    Parameters:
    - changes: List of objects. Each names one track with track_index
      (1-based), track_name or return_track, may add expect_name, and sets
      any of: mute (bool), solo (bool), arm (bool), input_type (str),
      input_channel (str), monitoring ("in"/"auto"/"off").
      Example: [{"track_name": "Guide", "input_type": "No Input", "arm": false},
                {"track_index": 3, "expect_name": "Gtr DI", "mute": true}]
    - dry_run: True = validate and show the resolved targets without changing anything.

    Returns JSON: {ok, dry_run, changes: [{track, before, after or requested}], warnings}.
    """
    if not isinstance(changes, list) or not changes:
        raise ValueError("changes must be a non-empty list")
    converted, problems = [], []
    for number, change in enumerate(changes, 1):
        try:
            converted.append(convert_track_change(change))
        except (ValueError, TypeError) as e:
            problems.append(f"change {number}: {e}")
    if problems:
        raise ValueError("rejected the whole batch; nothing was changed. " + "; ".join(problems))
    return _send("set_tracks_state", {"changes": converted, "dry_run": bool(dry_run)})


@ableton_tool("getting recording status")
def get_recording_status(ctx: Context) -> str:
    """Check what would record before pressing record.

    Reports the transport flags (playing, record mode, session record,
    overdub, punch in/out, metronome, count-in, loop, exclusive arm/solo),
    every armed track with its input and monitoring, and warnings: armed
    tracks with input "No Input", armed tracks sharing an input, implicit_arm
    (Live's auto-arm), monitoring "In" on unarmed tracks, and record mode on
    while stopped.

    Returns JSON: {ok, transport, armed_tracks: [{index, name, arm, implicit_arm, input, monitoring}], warnings}.
    """
    return _send("get_recording_status")


@ableton_tool("listing tracks")
def list_tracks(
    ctx: Context,
    include_returns: bool = True,
    include_master: bool = True,
) -> str:
    """List every track in one compact pass.

    Each row: {index (1-based; null for returns/master), name, kind
    (midi/audio/group/return/master), group, folded, visible, mute, solo,
    muted_via_solo, arm, implicit_arm, can_be_armed, input, monitoring,
    output, device_count, arrangement_clip_count}. Return rows also carry
    return_track ("A", "B", ...).

    Parameters:
    - include_returns: Include return tracks.
    - include_master: Include the master track.
    """
    return _send(
        "list_tracks",
        {"include_returns": bool(include_returns), "include_master": bool(include_master)},
    )
