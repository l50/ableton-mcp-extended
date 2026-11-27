"""Export tools: render audio/video through Live's Export Audio/Video dialog.

Live's Export Audio/Video is not in the Live Object Model, so these tools
drive the dialog through macOS UI scripting (see live_export.py) instead of
sending a Remote Script command. Only the render range uses the LOM: the
range is read there (and, for "arrangement", selected with the loop brace),
and its length in seconds goes to the exporter to check the file against.

Unlike the other tools, these always reply with a JSON object, failures
included, so they are registered without the "Error ..." text wrapper.
"""

import json
import logging
from typing import Any

from mcp.server.fastmcp import Context

from MCP_Server import connection, live_export
from MCP_Server.app import mcp
from MCP_Server.conversions import ALL_TRACKS
from MCP_Server.tools.ui_state import armed_tracks_reader

logger = logging.getLogger(__name__)

SECONDS_PER_MINUTE = 60.0


def _export_failure(message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "status": "failed",
        "output_path": None,
        "output_files": [],
        "error": message,
        "warnings": [],
        "export_pending": False,
    }
    payload.update(extra)
    return payload


def _arrangement_end_beats(info: dict[str, Any]) -> float:
    """End of the last arrangement clip, in beats (0 when there are none)."""
    end = 0.0
    for track in info.get("tracks", []):
        for clip in track.get("arrangement_clips", []):
            end = max(end, float(clip.get("end_time") or 0))
    return end


def _run_live_export(
    output_path: str,
    file_format: str,
    sample_rate: int,
    bit_depth: int,
    render_range: str,
    normalize: bool,
    overwrite: bool,
    render_timeout: float,
    create_video: bool,
) -> str:
    """Validate the request, select the render range in Live, then export."""
    range_name = str(render_range or "").strip().lower()
    if range_name not in live_export.RENDER_RANGES:
        return json.dumps(
            _export_failure(
                "render_range must be one of "
                + ", ".join(live_export.RENDER_RANGES)
                + f" (got '{render_range}')"
            )
        )

    request = live_export.ExportRequest(
        output_path=output_path,
        format=file_format,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        normalize=normalize,
        select_loop=range_name != "selection",
        create_video=create_video,
        overwrite=overwrite,
        render_timeout=render_timeout,
    )
    restore_loop = None
    ableton = None
    try:
        request.validate()
        live_export.check_preconditions(request)
        exporter = live_export.LiveExporter(armed_reader=armed_tracks_reader)
        # Before the loop brace is touched: Live must not be in an earlier
        # export, and must have an audio device to render through.
        exporter.preflight()
        if range_name != "selection":
            ableton = connection.get_ableton_connection()
            info = ableton.send_command("get_arrangement_info", {"track_index": ALL_TRACKS})
            transport = info.get("transport", {})
            if transport.get("back_to_arranger"):
                return json.dumps(
                    _export_failure(
                        "Session clips are overriding the Arrangement (Back to Arrangement "
                        "is lit in Live), so the render would not be the Arrangement; click "
                        "Back to Arrangement, or use render_range='selection' to render "
                        "what is playing",
                        render_range=range_name,
                    )
                )
            if range_name == "arrangement":
                beats = _arrangement_end_beats(info)
                if beats <= 0:
                    return json.dumps(
                        _export_failure(
                            "the Arrangement has no clips to render; use render_range="
                            "'loop' or 'selection' instead",
                            render_range=range_name,
                        )
                    )
            else:
                beats = float(transport["loop_length"])
            request.expected_seconds = beats * SECONDS_PER_MINUTE / float(transport["tempo"])
            if range_name == "arrangement":
                restore_loop = {
                    "enabled": bool(transport.get("loop_enabled")),
                    "start": float(transport.get("loop_start") or 0),
                    "length": float(transport.get("loop_length") or 0),
                }
                # Point the loop brace at the whole Arrangement so Select Loop
                # pre-fills Render Start/Length; the brace is restored below.
                ableton.send_command(
                    "set_arrangement_loop",
                    {"enabled": restore_loop["enabled"], "start": 0.0, "length": beats},
                )
            ableton.send_command("set_view", {"view_name": "Arranger"})
        result = live_export.export_from_live(request, exporter)
    except Exception as e:
        logger.error("Error exporting from Live: %s", e)
        result = _export_failure(f"Error exporting from Live: {e}")
    finally:
        if restore_loop is not None and ableton is not None and result.get("export_pending"):
            # Live may still have the export dialog, Save panel or a render
            # going: moving the loop now can kill the render, so leave it.
            result["loop_to_restore"] = restore_loop
            result["error"] = (
                f"{result.get('error') or 'export failed'}. The loop brace was not restored "
                "because Live may still be exporting; once Live shows no export window, set "
                f"it back with set_arrangement_loop(enabled={restore_loop['enabled']}, "
                f"start={restore_loop['start']:g}, length={restore_loop['length']:g})"
            )
        elif restore_loop is not None and ableton is not None:
            try:
                ableton.send_command("set_arrangement_loop", restore_loop)
            except Exception as e:
                restore_warning = f"could not restore the loop brace: {e}"
                logger.error(restore_warning)
                result.setdefault("warnings", []).append(restore_warning)
    result["render_range"] = range_name
    return json.dumps(result)


@mcp.tool()
def export_audio(
    ctx: Context,
    output_path: str,
    format: str = "wav",
    sample_rate: int = 44100,
    bit_depth: int = 24,
    render_range: str = "arrangement",
    normalize: bool = False,
    overwrite: bool = False,
    render_timeout: float = 900.0,
) -> str:
    """Render Live's Main output to an audio file via File > Export Audio/Video.

    macOS only. This drives Live's UI (it is not a Remote Script command): Live
    is brought to the front and must not be showing another dialog. The app
    running this server needs Accessibility permission. Do not use the mouse
    or keyboard until it returns. Live keeps the dialog settings it was given.

    Parameters:
    - output_path: Absolute path of the file to write, e.g. "/Users/me/Music/song.wav".
      The folder must exist. The extension is optional; if given it must match format.
    - format: "wav", "aiff", "flac" (lossless PCM) or "mp3" (CBR 320).
    - sample_rate: 22050, 32000, 44100, 48000, 88200, 96000, 176400 or 192000.
    - bit_depth: 16, 24 or 32 (PCM formats only).
    - render_range: "arrangement" = bar 1 to the end of the last arrangement clip
      (the loop brace is moved to select it, then restored once Live is out of the
      export; if a failed export cannot be shown closed, export_pending is true
      and the brace is left, with loop_to_restore saying where it was); "loop" = the current
      loop brace; "selection" = whatever Live pre-fills (the current time selection).
      "arrangement" and "loop" refuse to run while Session clips override the
      Arrangement (Back to Arrangement lit).
    - normalize: Normalize the rendered file.
    - overwrite: Replace output_path if it already exists.
    - render_timeout: Seconds to wait for the render to finish.

    Fails before touching Live when Live logs no audio output device (the
    audio interface is unplugged), is already showing a dialog, or an earlier
    call returned export_pending and Live is not yet shown clear of that export.

    Returns JSON: {ok, status, output_path, output_files, expected_path,
    format, sample_rate, bit_depth, normalize, create_video, render_range,
    expected_seconds, rendered_seconds, length_verified, dialog_settings,
    file_info, steps, warnings, export_pending, error?, dialog_text?,
    loop_to_restore?}. On a failure after the Export dialog was asked for, the
    dialogs are cancelled and export_pending is false only once Live shows no
    export window; when true, the export may still be running in Live and
    nothing in the Set was moved back. ok is true only when
    the file exists and has stopped growing, and it is false when afinfo
    reports a sample rate, bit depth or, for "arrangement" and "loop", length
    other than the one requested. If afinfo cannot be run or does not report
    one of those, ok stays true and warnings says what was not verified.

    render_range is the range that was asked for. Render Start/Length are not
    read back from the dialog, so the only check on the range is the file's
    length: rendered_seconds (from afinfo) against expected_seconds (the
    range's beats at the Set's current tempo), with length_verified true when
    they match. A Set whose tempo changes inside the range fails that check
    even when the file is right. "selection" has no expected length and is
    never length-verified.
    """
    return _run_live_export(
        output_path,
        format,
        sample_rate,
        bit_depth,
        render_range,
        normalize,
        overwrite,
        render_timeout,
        create_video=False,
    )


@mcp.tool()
def export_video(
    ctx: Context,
    output_path: str,
    format: str = "wav",
    sample_rate: int = 44100,
    bit_depth: int = 24,
    render_range: str = "arrangement",
    normalize: bool = False,
    overwrite: bool = False,
    render_timeout: float = 1800.0,
) -> str:
    """Render the Arrangement's video, with the rendered audio, via Export Audio/Video.

    Same UI flow, requirements and parameters as export_audio, with Live's
    Create Video switch turned on. Live only enables that switch when the
    Arrangement contains video clips, and always writes the audio file too.
    The video goes in the same folder using the encoder currently chosen in
    the dialog. output_path names the audio file.

    Returns JSON like export_audio plus video_files. ok is true only when both
    the audio file and a new video file exist in the folder.
    """
    return _run_live_export(
        output_path,
        format,
        sample_rate,
        bit_depth,
        render_range,
        normalize,
        overwrite,
        render_timeout,
        create_video=True,
    )
