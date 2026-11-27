"""UI-state tools: what Live's UI is doing, and a way to clear a time selection.

Key Map and MIDI Map mode, dialogs, keyboard focus and the Arrangement time
selection are not in the Live Object Model, so these read them through macOS
accessibility (see live_ui_state.py). Like the export tools they always reply
with a JSON object, failures included.
"""

import json
import logging
from typing import Any

from mcp.server.fastmcp import Context

from MCP_Server import connection, live_ui_state
from MCP_Server.app import mcp
from MCP_Server.tools.recording import one_based_track_indices

logger = logging.getLogger(__name__)


def armed_tracks_reader() -> list[dict[str, Any]]:
    """Live's armed tracks from the Remote Script, with 1-based track indices."""
    status = connection.get_ableton_connection().send_command("get_recording_status")
    return one_based_track_indices(status.get("armed_tracks") or [])


def _failure(message: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": message, **extra})


@mcp.tool()
def get_ui_state(ctx: Context) -> str:
    """Read what Live's UI is doing right now, without changing anything.

    macOS only. Reads Live through accessibility (the app running this server
    needs Accessibility permission), plus the armed tracks from Live itself.
    Use it before driving Live's UI: the UI tools (export_audio, export_video,
    save_set) call the same check and refuse when it says the UI is unsafe.

    Returns JSON: {ok, running, frontmost, set: {window_title, path},
    key_map_mode, midi_map_mode, computer_midi_keyboard, dialogs: [{name,
    subrole, modal, sheets, text, buttons}], other_windows, focus: {view
    ("arrangement", "session", "detail", "mixer", "browser", "control",
    "learn", "status", "dialog" or "unknown"), element, reason?}, transport: {playing,
    recording, arrangement_record, session_record, overdub, loop, punch_in,
    punch_out}, time_selection: {present, reason?}, items_selected,
    armed_tracks (null with armed_tracks_error when Live's Remote Script
    cannot be reached), unreadable, safe_to_drive, unsafe_reasons}.

    A value is null, never a guess, when accessibility cannot tell: for
    example time_selection.present is null while a view other than the
    Arrangement has focus. safe_to_drive is false in Key Map or MIDI Map
    mode, with any dialog open, while playing or recording, with Computer
    MIDI Keyboard on, or when something could not be read. It does not
    refuse to run: it reports.
    """
    try:
        state = live_ui_state.read_ui_state(armed_reader=armed_tracks_reader)
    except Exception as e:
        logger.error("Error reading Live's UI state: %s", e)
        return _failure(f"Error reading Live's UI state: {e}")
    return json.dumps(state)


@mcp.tool()
def clear_selection(ctx: Context) -> str:
    """Remove the Arrangement time selection, then check the transport is as it was.

    macOS only. A time selection left by a UI-driven tool (Edit > Select Loop)
    makes stray keys nudge or delete clips and tracks. This replaces it with
    the insert marker using Playback > Move Insert Marker To Playhead: no
    click in a lane and no key is sent. When another view has focus and
    accessibility cannot tell whether a selection exists, Navigate >
    Arrangement View moves focus to the Arrangement first. The selection is
    read back afterwards, and if playback started it is stopped.

    Refuses, clicking nothing, in Key Map or MIDI Map mode, with a dialog
    open, or while Live is playing or recording. Clip or track selections are reported (items_selected_after) but
    not changed.

    Returns JSON: {ok, time_selection_before, time_selection_after,
    transport_before, transport_after, transport_changed, stopped_playback?,
    items_selected_after, steps, warnings, error?}. ok is true only when no
    time selection remains and the transport is unchanged.
    """
    try:
        result = live_ui_state.clear_selection(armed_reader=armed_tracks_reader)
    except live_ui_state.UnsafeUIState as e:
        return _failure(
            "Live's UI is not safe to drive, so nothing was clicked: " + "; ".join(e.reasons),
            unsafe_reasons=e.reasons,
        )
    except Exception as e:
        logger.error("Error clearing the selection: %s", e)
        return _failure(f"Error clearing the selection: {e}")
    return json.dumps(result)
