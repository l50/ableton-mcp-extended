"""Save tools: save the open Live Set, or save it under a new name.

Saving is not in the Live Object Model, so these drive File > Save Live Set
(As...) through macOS accessibility (see live_save.py). They run only when
called and return the file's path and modification time read afterwards, so
the caller can check the save. No other tool calls them. They always reply
with a JSON object, failures included.
"""

import json
import logging

from mcp.server.fastmcp import Context

from MCP_Server import live_save
from MCP_Server.app import mcp
from MCP_Server.tools.ui_state import armed_tracks_reader

logger = logging.getLogger(__name__)


@mcp.tool()
def save_set(ctx: Context) -> str:
    """Save the open Live Set in place (File > Save Live Set) and prove it was written.

    macOS only. Drives Live's menu through accessibility. Runs only when you
    call it: no other tool saves. Refuses, clicking nothing, while a dialog is
    open, Key Map or MIDI Map mode is on, or Live is playing or recording (see get_ui_state), and refuses an
    untitled Set that has no file yet (use save_set_as). Live disables Save
    while the Set has no unsaved changes: that is a failure (unmodified: true),
    since the file's modification time cannot move.

    Returns JSON: {ok, saved, path, mtime_before, mtime, modified (ISO time),
    size_bytes, unmodified?, error?}. path is the open Set's file as Live reports it. ok is
    true only when that file's modification time is later than before the
    save, read after Live stopped writing.
    """
    try:
        return json.dumps(live_save.save_set(armed_reader=armed_tracks_reader))
    except Exception as e:
        logger.error("Error saving the Set: %s", e)
        return json.dumps({"ok": False, "saved": False, "error": f"Error saving the Set: {e}"})


@mcp.tool()
def save_set_as(ctx: Context, path: str, overwrite: bool = False) -> str:
    """Save the open Live Set to a new .als file (File > Save Live Set As...).

    macOS only. The Set stays open under the new name. Runs only when you call
    it. Refuses, clicking nothing, while a dialog is open, Key Map or MIDI
    Map mode is on, or Live is playing or recording.

    Parameters:
    - path: Absolute path ending in .als. The folder must exist and be writable.
    - overwrite: Replace the file if it exists (Live's Replace prompt is only
      answered when this is true).

    Live puts a Set it has never saved in a "<name> Project" folder it creates
    next to the requested file name, so the file may not be exactly where you
    asked: path is where it is, requested_path what you asked for.

    Returns JSON: {ok, saved, path, requested_path, in_project_folder, mtime,
    modified (ISO time), size_bytes, open_set_path, set_title, error?}. ok is
    true only when the file exists, is not empty, and Live reports it as the
    open Set.
    """
    try:
        return json.dumps(live_save.save_set_as(path, overwrite, armed_reader=armed_tracks_reader))
    except Exception as e:
        logger.error("Error saving the Set as %s: %s", path, e)
        return json.dumps({"ok": False, "saved": False, "error": f"Error saving the Set: {e}"})
