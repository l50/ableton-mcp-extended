"""Arrangement tools: song position, loop, cue points, clips and automation.

Positions are given as a 1-based bar (converted with the Set's current time
signature) or, when the bar is 0, as a 0-based beat.
"""

from mcp.server.fastmcp import Context

from MCP_Server import connection
from MCP_Server.conversions import (
    ALL_TRACKS,
    DEFAULT_TIME_SIGNATURE,
    arrangement_clip_target,
    bar_to_beat,
    beat_to_bar,
    optional_to_zero_based,
    to_zero_based,
)
from MCP_Server.tools.base import ableton_tool

ARRANGEMENT_TIP = "\nTip: use set_ableton_view(view='Arranger') to see changes in arrangement view."
# control_arrangement_view's track_index when no track is given.
NO_TRACK = 0


def _get_time_signature() -> tuple[int, int]:
    """Get the current time signature (numerator, denominator) from Live."""
    info = connection.get_ableton_connection().send_command("get_session_info")
    numerator, denominator = DEFAULT_TIME_SIGNATURE
    return info.get("signature_numerator", numerator), info.get(
        "signature_denominator", denominator
    )


def _convert_bar_to_beat(bar: int, beat: float = 0.0) -> float:
    """Convert a 1-based bar to beats, or return *beat* when bar is 0."""
    if bar > 0:
        num, denom = _get_time_signature()
        return bar_to_beat(bar, num, denom)
    return beat


def _bar_range(start: float, end: float, num: int, denom: int) -> str:
    return f"{beat_to_bar(start, num, denom)}-{beat_to_bar(end, num, denom)}"


def _audio_source(clip: dict) -> str:
    """The audio file an arrangement clip plays and, if unwarped, which seconds of it."""
    if not clip.get("file_path"):
        return ""
    file_name = clip["file_path"].rsplit("/", 1)[-1]
    if clip.get("warping"):
        return f" [{file_name}, warped]"
    return (
        f" [{file_name}, unwarped, file {clip.get('loop_start', 0):.3f}-"
        f"{clip.get('loop_end', 0):.3f}s]"
    )


@ableton_tool("getting arrangement info")
def get_arrangement_info(ctx: Context, track_index: int = 0) -> str:
    """Get arrangement clips and transport state.

    Parameters:
    - track_index: Track number (1-based). 0 = all tracks.
    """
    ableton = connection.get_ableton_connection()
    idx = optional_to_zero_based(track_index, "track_index")
    result = ableton.send_command(
        "get_arrangement_info", {"track_index": idx if idx is not None else ALL_TRACKS}
    )

    transport = result.get("transport", {})
    num = transport.get("signature_numerator", DEFAULT_TIME_SIGNATURE[0])
    denom = transport.get("signature_denominator", DEFAULT_TIME_SIGNATURE[1])

    lines = [
        "=== Arrangement Info ===",
        f"Tempo: {transport.get('tempo')} BPM | "
        f"Time Sig: {num}/{denom} | "
        f"Playing: {transport.get('is_playing')} | "
        f"Position: bar {beat_to_bar(transport.get('current_time', 0), num, denom)}",
    ]
    if transport.get("loop_enabled"):
        loop_start = transport.get("loop_start", 0)
        loop_end = loop_start + transport.get("loop_length", 0)
        lines.append(f"Loop: bars {_bar_range(loop_start, loop_end, num, denom)}")

    for track in result.get("tracks", []):
        clips = track.get("arrangement_clips", [])
        kind = "MIDI" if track.get("is_midi") else "Audio"
        lines.append(
            f"\nTrack {track['index'] + 1}: {track['name']} ({kind}) — {len(clips)} clip(s)"
        )
        for clip in clips:
            bars = _bar_range(clip.get("start_time", 0), clip.get("end_time", 0), num, denom)
            muted = " [MUTED]" if clip.get("muted") else ""
            lines.append(
                f'  {clip.get("index", 0) + 1}. "{clip.get("name", "")}" bars {bars}{muted}'
                f"{_audio_source(clip)}"
            )

    return "\n".join(lines)


@ableton_tool("getting cue points")
def get_cue_points(ctx: Context) -> str:
    """List all cue points (locators) with bar positions."""
    result = connection.get_ableton_connection().send_command("get_cue_points")
    num, denom = _get_time_signature()

    cues = result.get("cue_points", [])
    if not cues:
        return "No cue points in this project."

    lines = ["=== Cue Points ==="]
    for cue in cues:
        bar = beat_to_bar(cue.get("time", 0), num, denom)
        lines.append(f'  "{cue.get("name", "")}" — bar {bar}')
    return "\n".join(lines)


@ableton_tool("setting song time")
def set_song_time(ctx: Context, bar: int = 0, beat: float = 0.0) -> str:
    """Jump playback to a position.

    Parameters:
    - bar: Bar number (1-based). Takes precedence over beat.
    - beat: Beat position (0-based).
    """
    ableton = connection.get_ableton_connection()
    time_val = _convert_bar_to_beat(bar, beat)
    ableton.send_command("set_song_time", {"time": time_val})
    num, denom = _get_time_signature()
    return f"Jumped to bar {beat_to_bar(time_val, num, denom)} (beat {time_val})"


@ableton_tool("setting arrangement loop")
def set_arrangement_loop(
    ctx: Context,
    enabled: bool = True,
    start_bar: int = 0,
    end_bar: int = 0,
    start_beat: float = 0.0,
    length_beats: float = 0.0,
) -> str:
    """Enable/disable arrangement loop and set region.

    Parameters:
    - enabled: Whether loop is on.
    - start_bar: Loop start (1-based). Takes precedence over start_beat.
    - end_bar: Loop end (1-based). Used with start_bar to compute length.
    - start_beat: Loop start in beats.
    - length_beats: Loop length in beats.
    """
    ableton = connection.get_ableton_connection()
    num, denom = _get_time_signature()

    params: dict[str, bool | float] = {"enabled": enabled}
    if start_bar > 0:
        params["start"] = bar_to_beat(start_bar, num, denom)
        if end_bar > start_bar:
            params["length"] = bar_to_beat(end_bar, num, denom) - params["start"]
    else:
        if start_beat > 0:
            params["start"] = start_beat
        if length_beats > 0:
            params["length"] = length_beats

    result = ableton.send_command("set_arrangement_loop", params)
    state = "enabled" if result.get("enabled") else "disabled"
    loop_start = result.get("start", 0)
    loop_end = loop_start + result.get("length", 0)
    return f"Loop {state}: bars {_bar_range(loop_start, loop_end, num, denom)}"


@ableton_tool("jumping to cue point")
def jump_to_cue_point(ctx: Context, direction: str = "", name: str = "") -> str:
    """Jump to a cue point.

    Parameters:
    - direction: "next" or "prev"
    - name: Cue point name to jump to.
    """
    ableton = connection.get_ableton_connection()
    params = {}
    if direction:
        params["direction"] = direction
    if name:
        params["name"] = name
    result = ableton.send_command("jump_to_cue", params)
    return f"Jumped to cue point: {result}"


@ableton_tool("creating cue point")
def create_cue_point(ctx: Context, bar: int = 0, beat: float = 0.0, name: str = "") -> str:
    """Create a cue point at a position.

    Parameters:
    - bar: Bar number (1-based).
    - beat: Beat position (0-based).
    - name: Name for the cue point.
    """
    ableton = connection.get_ableton_connection()
    time_val = _convert_bar_to_beat(bar, beat)
    ableton.send_command("create_cue_point", {"time": time_val, "name": name})
    return f"Created cue point '{name}' at bar {bar if bar > 0 else '?'}"


@ableton_tool("deleting cue point")
def delete_cue_point(ctx: Context, bar: int = 0, beat: float = 0.0) -> str:
    """Delete a cue point at a position.

    Parameters:
    - bar: Bar number (1-based).
    - beat: Beat position (0-based).
    """
    ableton = connection.get_ableton_connection()
    time_val = _convert_bar_to_beat(bar, beat)
    ableton.send_command("delete_cue_point", {"time": time_val})
    return f"Deleted cue point at bar {bar if bar > 0 else '?'}"


@ableton_tool("creating arrangement MIDI clip")
def create_arrangement_midi_clip(
    ctx: Context,
    track_index: int,
    start_bar: int = 0,
    end_bar: int = 0,
    start_beat: float = 0.0,
    length_beats: float = 4.0,
    name: str = "",
) -> str:
    """Create an empty MIDI clip in the arrangement.

    Parameters:
    - track_index: Track number (1-based).
    - start_bar: Start bar (1-based). Takes precedence over start_beat.
    - end_bar: End bar (1-based). Used with start_bar to compute length.
    - start_beat: Start position in beats.
    - length_beats: Clip length in beats.
    - name: Optional clip name.
    """
    ableton = connection.get_ableton_connection()
    num, denom = _get_time_signature()

    position, length = start_beat, length_beats
    if start_bar > 0:
        position = bar_to_beat(start_bar, num, denom)
        if end_bar > start_bar:
            length = bar_to_beat(end_bar, num, denom) - position

    ti = to_zero_based(track_index, "track_index")
    result = ableton.send_command(
        "create_arrangement_clip", {"track_index": ti, "position": position, "length": length}
    )

    msg = (
        f"Created MIDI clip on track {track_index} at "
        f"bar {beat_to_bar(position, num, denom)}, "
        f"length {length} beats"
    )
    overlapped = result.get("overlapped_clips", [])
    if overlapped:
        msg += f"\nWarning: overlapped existing clips: {', '.join(overlapped)}"
    return msg + ARRANGEMENT_TIP


@ableton_tool("creating arrangement audio clip")
def create_arrangement_audio_clip(
    ctx: Context,
    track_index: int,
    file_path: str,
    start_bar: int = 0,
    start_beat: float = 0.0,
) -> str:
    """Place an audio file as a clip in the arrangement.

    Parameters:
    - track_index: Track number (1-based).
    - file_path: Path to the audio file.
    - start_bar: Start bar (1-based).
    - start_beat: Start position in beats.
    """
    ableton = connection.get_ableton_connection()
    position = _convert_bar_to_beat(start_bar, start_beat)
    ti = to_zero_based(track_index, "track_index")
    ableton.send_command(
        "create_arrangement_audio_clip",
        {"track_index": ti, "position": position, "file_path": file_path},
    )
    return f"Created audio clip from '{file_path}' on track {track_index}" + ARRANGEMENT_TIP


@ableton_tool("duplicating clip to arrangement")
def duplicate_clip_to_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_bar: int = 0,
    destination_beat: float = 0.0,
) -> str:
    """Copy a session clip to the arrangement.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Session clip slot (1-based).
    - destination_bar: Destination bar (1-based).
    - destination_beat: Destination beat.
    """
    ableton = connection.get_ableton_connection()
    dest = _convert_bar_to_beat(destination_bar, destination_beat)
    ti = to_zero_based(track_index, "track_index")
    ci = to_zero_based(clip_index, "clip_index")
    ableton.send_command(
        "duplicate_to_arrangement",
        {"track_index": ti, "clip_index": ci, "destination_time": dest},
    )
    return f"Duplicated session clip to arrangement on track {track_index}" + ARRANGEMENT_TIP


@ableton_tool("deleting arrangement clip")
def delete_arrangement_clip(
    ctx: Context,
    track_index: int,
    clip_index: int = 0,
    clip_name: str = "",
) -> str:
    """Delete an arrangement clip.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip position in arrangement (1-based).
    - clip_name: Clip name (alternative to clip_index).
    """
    ableton = connection.get_ableton_connection()
    params: dict[str, int | str] = {"track_index": to_zero_based(track_index, "track_index")}
    if clip_name:
        params["clip_name"] = clip_name
    elif clip_index > 0:
        params["clip_index"] = to_zero_based(clip_index, "clip_index")
    else:
        return "Error: provide clip_index or clip_name"

    ableton.send_command("delete_arrangement_clip", params)
    ref = f"'{clip_name}'" if clip_name else f"#{clip_index}"
    return f"Deleted arrangement clip {ref} on track {track_index}" + ARRANGEMENT_TIP


def _overlap_warning(result: dict) -> str:
    overlapped = result.get("overlapped_clips", [])
    if not overlapped:
        return ""
    return (
        f"\nWarning: overlapped existing clips: {', '.join(overlapped)}"
        " (as with pasting, Live cuts out the part the new clip covers: a clip it lands"
        " inside is split around it)"
    )


def _audio_settings(result: dict) -> str:
    if not result.get("is_audio"):
        return ""
    warp = "warped" if result.get("warping") else "unwarped (plays at original speed)"
    return (
        f"\nAudio: {result.get('file_path')} | {warp} | gain {result.get('gain')} | "
        f"pitch {result.get('pitch_coarse')} st {result.get('pitch_fine')} ct"
    )


@ableton_tool("copying arrangement clip region")
def copy_arrangement_clip_region(
    ctx: Context,
    track_index: int,
    region_start_seconds: float,
    region_end_seconds: float,
    clip_index: int = 0,
    clip_name: str = "",
    destination_bar: int = 0,
    destination_beat: float = 0.0,
) -> str:
    """Copy part of an arrangement audio clip to a new position on the same track.

    The copy plays the source clip's own audio file (no new file is rendered)
    and keeps the source's name, warping, warp mode, gain and pitch, so an
    unwarped source still plays at its original speed.

    Parameters:
    - track_index: Track holding the source clip (1-based).
    - region_start_seconds: Region start, in seconds of the clip's audio file
      (0 = the file's first sample, not the clip's left edge).
    - region_end_seconds: Region end, in seconds of the clip's audio file.
    - clip_index: Source clip position in the arrangement (1-based).
    - clip_name: Source clip name (alternative to clip_index).
    - destination_bar: Destination bar (1-based). Takes precedence over destination_beat.
    - destination_beat: Destination position in beats (0-based).
    """
    ableton = connection.get_ableton_connection()
    params = {
        "track_index": to_zero_based(track_index, "track_index"),
        **arrangement_clip_target(clip_index, clip_name),
        "destination_time": _convert_bar_to_beat(destination_bar, destination_beat),
        "region_start": region_start_seconds,
        "region_end": region_end_seconds,
    }
    result = ableton.send_command("copy_arrangement_audio_region", params)
    num, denom = _get_time_signature()
    start = result.get("start_time", params["destination_time"])
    msg = (
        f"Copied {region_start_seconds}-{region_end_seconds}s of "
        f"'{result.get('source_clip', clip_name)}' on '{result.get('track')}' "
        f"to bar {beat_to_bar(start, num, denom)} (beat {start}), "
        f"length {result.get('length')} beats"
    )
    return msg + _audio_settings(result) + _overlap_warning(result) + ARRANGEMENT_TIP


@ableton_tool("moving arrangement clip")
def move_arrangement_clip(
    ctx: Context,
    track_index: int,
    clip_index: int = 0,
    clip_name: str = "",
    destination_bar: int = 0,
    destination_beat: float = 0.0,
) -> str:
    """Move an arrangement clip to a new position on the same track.

    Live has no move call, so the clip is duplicated to the new position, the
    copy is checked against the original (audio file, markers, warping, gain,
    pitch, and length unless it is unwarped audio), and only then is the
    original deleted.

    Parameters:
    - track_index: Track holding the clip (1-based).
    - clip_index: Clip position in the arrangement (1-based).
    - clip_name: Clip name (alternative to clip_index).
    - destination_bar: Destination bar (1-based). Takes precedence over destination_beat.
    - destination_beat: Destination position in beats (0-based).
    """
    ableton = connection.get_ableton_connection()
    params = {
        "track_index": to_zero_based(track_index, "track_index"),
        **arrangement_clip_target(clip_index, clip_name),
        "destination_time": _convert_bar_to_beat(destination_bar, destination_beat),
    }
    result = ableton.send_command("move_arrangement_clip", params)
    num, denom = _get_time_signature()
    start = result.get("start_time", params["destination_time"])
    old = result.get("moved_from", 0)
    msg = (
        f"Moved '{result.get('name') or '(unnamed)'}' on '{result.get('track')}' from bar "
        f"{beat_to_bar(old, num, denom)} (beat {old}) to "
        f"bar {beat_to_bar(start, num, denom)} (beat {start})"
    )
    return msg + _audio_settings(result) + _overlap_warning(result) + ARRANGEMENT_TIP


@ableton_tool("setting arrangement clip property")
def set_arrangement_clip_property(
    ctx: Context,
    track_index: int,
    clip_index: int = 1,
    clip_name: str = "",
    name: str = "",
    # These keep the bare type with a None default (not "X | None") because
    # that is the parameter schema MCP clients already see.
    muted: bool = None,
    color: int = None,
    looping: bool = None,
    loop_start: float = None,
    loop_end: float = None,
    gain: float = None,
    pitch_coarse: int = None,
    pitch_fine: float = None,
    warping: bool = None,
    warp_mode: int = None,
) -> str:
    """Set properties on an arrangement clip.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip position (1-based).
    - clip_name: Clip name (alternative to clip_index).
    - name: New clip name.
    - muted: Mute state.
    - color: Color (0x00RRGGBB).
    - looping: Loop on/off.
    - loop_start: Loop start in beats.
    - loop_end: Loop end in beats.
    - gain: Audio gain (0.0-1.0).
    - pitch_coarse: Semitone pitch shift (-48 to 48).
    - pitch_fine: Fine pitch shift (-50 to 49 cents).
    - warping: Warp on/off.
    - warp_mode: Warp mode (0-6).
    """
    ableton = connection.get_ableton_connection()
    ti = to_zero_based(track_index, "track_index")
    target = arrangement_clip_target(clip_index, clip_name)

    # "name" goes last: renaming first would break name-based lookup
    # for the remaining properties.
    props = {
        "muted": muted,
        "color": color,
        "looping": looping,
        "loop_start": loop_start,
        "loop_end": loop_end,
        "gain": gain,
        "pitch_coarse": pitch_coarse,
        "pitch_fine": pitch_fine,
        "warping": warping,
        "warp_mode": warp_mode,
        "name": name,
    }

    changes = []
    changed_clip = None
    for prop_name, value in props.items():
        if value is None or value == "":
            continue
        result = ableton.send_command(
            "set_arrangement_clip_property",
            {"track_index": ti, **target, "property": prop_name, "value": value},
        )
        if not changed_clip and isinstance(result, dict):
            changed_clip = result.get("clip_name")
        changes.append(f"{prop_name}={value}")

    if not changes:
        return "No properties specified to change."

    label = changed_clip or clip_name
    ref = f"'{label}'" if label else f"clip {clip_index}"
    return f"Updated {ref} on track {track_index}: {', '.join(changes)}"


@ableton_tool("controlling arrangement view")
def control_arrangement_view(ctx: Context, action: str, track_index: int = 0) -> str:
    """Control the arrangement view.

    Parameters:
    - action: One of: zoom_in, zoom_out, scroll_left, scroll_right,
              follow_on, follow_off, collapse_track, expand_track.
    - track_index: Track number (1-based, for collapse/expand).
    """
    ableton = connection.get_ableton_connection()
    ti = optional_to_zero_based(track_index, "track_index")
    ableton.send_command(
        "control_arrangement_view",
        {"action": action, "track_index": ti if ti is not None else NO_TRACK},
    )
    return f"Arrangement view: {action} done"


@ableton_tool("managing clip automation")
def manage_clip_automation(
    ctx: Context,
    track_index: int,
    clip_index: int = 1,
    clip_name: str = "",
    action: str = "create",
    parameter_name: str = "volume",
) -> str:
    """Create or clear automation envelopes on arrangement clips.

    Parameters:
    - track_index: Track number (1-based).
    - clip_index: Clip position (1-based).
    - clip_name: Clip name (alternative to clip_index).
    - action: "create", "clear", or "clear_all".
    - parameter_name: Parameter to automate (e.g., "volume", "panning").
    """
    ableton = connection.get_ableton_connection()
    params = {
        "track_index": to_zero_based(track_index, "track_index"),
        **arrangement_clip_target(clip_index, clip_name),
        "action": action,
        "parameter_name": parameter_name,
    }
    result = ableton.send_command("manage_clip_automation", params)

    changed_clip = result.get("clip_name") if isinstance(result, dict) else None
    label = changed_clip or clip_name
    ref = f"clip '{label}'" if label else f"clip {clip_index}"
    if action == "clear_all":
        return f"Cleared all automation on {ref}, track {track_index}"
    return f"Automation {action}: {parameter_name} on {ref}, track {track_index}"
