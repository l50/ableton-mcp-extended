"""Export tools: render audio/video through Live's Export Audio/Video dialog.

Live's Export Audio/Video is not in the Live Object Model, so these tools
drive the dialog through macOS UI scripting (see live_export.py) instead of
sending a Remote Script command. Only the render range uses the LOM, and only
to read it: the range (the whole Arrangement, the loop brace, or one the
caller names) is worked out from the transport and the clips, then typed into
the dialog's Render Start and Render Length fields. The loop brace and the
Arrangement selection are never touched, and its length in seconds goes to the
exporter to check the file against.

Unlike the other tools, these always reply with a JSON object, failures
included, so they are registered without the "Error ..." text wrapper.
"""

import json
import logging
import math
from typing import Any

from mcp.server.fastmcp import Context

from MCP_Server import connection, live_export
from MCP_Server.app import mcp
from MCP_Server.conversions import ALL_TRACKS, DEFAULT_TIME_SIGNATURE, bar_to_beat
from MCP_Server.tools.ui_state import armed_tracks_reader

logger = logging.getLogger(__name__)

SECONDS_PER_MINUTE = 60.0
CUSTOM_RANGE = "custom"
# Slack when a beat count is checked against the sixteenth grid.
GRID_EPSILON = 1e-6


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


def _round_out(start: float, length: float) -> tuple[float, float]:
    """The sixteenth-note range that contains start .. start + length.

    The dialog's fields count sixteenths, so a range read from the Set (a
    clip that ends off the grid) is widened to the grid, never narrowed: the
    render then contains every clip in full.
    """
    per_beat = live_export.SIXTEENTHS_PER_BEAT
    first = math.floor(start * per_beat + GRID_EPSILON) / per_beat
    last = math.ceil((start + length) * per_beat - GRID_EPSILON) / per_beat
    return first, last - first


def _explicit_range(
    start_bar: int,
    start_beat: float | None,
    length_bars: int,
    length_beats: float | None,
    signature: tuple[int, int],
) -> tuple[float | None, float | None]:
    """The start and length (beats) the caller named; None for a part left out.

    start_bar and length_bars (counts of bars, 1-based for the start) take
    precedence over the beat arguments, as in the other tools that take both.
    """
    numerator, denominator = signature
    beats_per_bar = numerator * 4 / denominator
    start = length = None
    if start_bar > 0:
        start = bar_to_beat(start_bar, numerator, denominator)
    elif start_bar < 0:
        raise ValueError(f"start_bar must be >= 1 (got {start_bar}); 0 leaves it out")
    elif start_beat is not None:
        start = float(start_beat)
    if length_bars > 0:
        length = length_bars * beats_per_bar
    elif length_bars < 0:
        raise ValueError(f"length_bars must be >= 1 (got {length_bars}); 0 leaves it out")
    elif length_beats is not None:
        length = float(length_beats)
    return start, length


def _check_track_name(ableton: Any, name: str) -> str | None:
    """None when the Rendered Track chooser can tell this track from the rest, else why not."""
    rows = ableton.send_command(
        "list_tracks", {"include_returns": True, "include_master": False}
    ).get("tracks", [])
    matches = [row for row in rows if row.get("name") == name]
    if not matches:
        return f"there is no track named '{name}' (see list_tracks)"
    if len(matches) > 1:
        return (
            f"{len(matches)} tracks are named '{name}'; the Rendered Track chooser would pick "
            "the first, so rename them to tell them apart"
        )
    return None


def _expected_individual_files(ableton: Any) -> int | None:
    """How many files All Individual Tracks should write, or None when that is not known.

    Live writes one file per track (return tracks included) and one more for
    the Main mix under the name given in the Save panel. Group tracks were not
    observed, so with one in the Set the count is not checked.
    """
    rows = ableton.send_command(
        "list_tracks", {"include_returns": True, "include_master": False}
    ).get("tracks", [])
    if not rows or any(row.get("kind") == "group" for row in rows):
        return None
    return len(rows) + 1


def _plan_range(
    request: live_export.ExportRequest,
    range_name: str,
    info: dict[str, Any],
    explicit: tuple[float | None, float | None],
) -> tuple[str | None, str]:
    """Fill the request's typed range and expected length; (error, range label).

    ``range_name`` is one of the render_range names; ``explicit`` is what the
    caller named. A named range wins over render_range.
    """
    transport = info.get("transport", {})
    named_start, named_length = explicit
    label = range_name
    rounded = False
    if named_start is not None or named_length is not None:
        label = CUSTOM_RANGE
        start = named_start or 0.0
        if named_length is not None:
            length = named_length
        else:
            end = _arrangement_end_beats(info)
            if end <= start:
                return (
                    f"the Arrangement ends at beat {end:g}, so there is nothing to render from "
                    f"beat {start:g}; give length_bars or length_beats",
                    label,
                )
            # The end is read from the Set, so it is widened to the grid; the
            # start was named, so it is typed as given (or refused off the grid).
            length = (
                math.ceil(end * live_export.SIXTEENTHS_PER_BEAT - GRID_EPSILON)
                / live_export.SIXTEENTHS_PER_BEAT
                - start
            )
    elif range_name == "arrangement":
        end = _arrangement_end_beats(info)
        if end <= 0:
            return (
                "the Arrangement has no clips to render; use render_range='loop' or "
                "'selection' instead",
                label,
            )
        start, length = _round_out(0.0, end)
        rounded = abs(length - end) > GRID_EPSILON
    else:
        length = float(transport["loop_length"])
        if length <= 0:
            return ("the loop brace has no length to render", label)
        loop_start = float(transport.get("loop_start") or 0)
        start, length = _round_out(loop_start, length)
        rounded = (
            abs(start - loop_start) > GRID_EPSILON
            or abs(length - float(transport["loop_length"])) > GRID_EPSILON
        )
    request.start_beats = start
    request.length_beats = length
    request.expected_seconds = length * SECONDS_PER_MINUTE / float(transport["tempo"])
    request.range_rounded = rounded
    return None, label


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
    start_bar: int = 0,
    start_beat: float | None = None,
    length_bars: int = 0,
    length_beats: float | None = None,
    rendered_track: str = "Main",
) -> str:
    """Validate the request, work out the render range, then export."""
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
        create_video=create_video,
        overwrite=overwrite,
        render_timeout=render_timeout,
        rendered_track=rendered_track,
    )
    label = range_name
    try:
        request.validate()
        live_export.check_preconditions(request)
        ableton = None
        named = (
            start_bar != 0 or length_bars != 0 or start_beat is not None or length_beats is not None
        )
        needs_range = range_name != "selection" or named
        if needs_range or request.track_mode in ("track", "all"):
            ableton = connection.get_ableton_connection()
        if request.track_mode == "track":
            problem = _check_track_name(ableton, request.track_name)
            if problem:
                return json.dumps(_export_failure(problem, render_range=label))
        elif request.track_mode == "all":
            request.expected_files = _expected_individual_files(ableton)
        if needs_range:
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
            request.signature = (
                int(transport.get("signature_numerator") or DEFAULT_TIME_SIGNATURE[0]),
                int(transport.get("signature_denominator") or DEFAULT_TIME_SIGNATURE[1]),
            )
            explicit = _explicit_range(
                start_bar, start_beat, length_bars, length_beats, request.signature
            )
            problem, label = _plan_range(request, range_name, info, explicit)
            if problem:
                return json.dumps(_export_failure(problem, render_range=label))
            # A range the caller named must sit on the dialog's sixteenth grid.
            request.validate()
        exporter = live_export.LiveExporter(armed_reader=armed_tracks_reader)
        result = live_export.export_from_live(request, exporter)
    except Exception as e:
        logger.error("Error exporting from Live: %s", e)
        result = _export_failure(f"Error exporting from Live: {e}")
    result["render_range"] = label
    if request.typed_range:
        result["range_rounded"] = request.range_rounded
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
    start_bar: int = 0,
    start_beat: float = None,
    length_bars: int = 0,
    length_beats: float = None,
    rendered_track: str = "Main",
) -> str:
    """Render Live's Main output (or a track, or every track) to audio files via File > Export Audio/Video.

    macOS only. This drives Live's UI (it is not a Remote Script command): Live
    is brought to the front and must not be showing another dialog. The app
    running this server needs Accessibility permission. Do not use the mouse
    or keyboard until it returns. Live keeps the dialog settings it was given.

    The render range is typed into the dialog's Render Start and Render Length
    fields and read back before Export is pressed. The loop brace and the
    Arrangement selection are not used and not changed, so no time selection or
    loop change is left behind. While a render is running nothing is restored
    or changed; the tool returns only after the render window has closed and
    every output file has stopped growing.

    Parameters:
    - output_path: Absolute path of the file to write, e.g. "/Users/me/Music/song.wav".
      The folder must exist. The extension is optional; if given it must match format.
      With a track name Live writes "<name> <track>.wav". With rendered_track="all" it is the name the files start with: Live writes one
      file per track beside it ("<name> <track>.wav"), plus the Main mix as "<name>.wav".
    - format: "wav", "aiff", "flac" (lossless PCM) or "mp3" (CBR 320).
    - sample_rate: 22050, 32000, 44100, 48000, 88200, 96000, 176400 or 192000.
    - bit_depth: 16, 24 or 32 (PCM formats only).
    - render_range: what to render when no start or length is given. "arrangement" = bar 1
      to the end of the last arrangement clip; "loop" = the current loop brace (read, never
      moved); "selection" = whatever Live pre-fills in the dialog (the current time
      selection). "arrangement" and "loop" refuse to run while Session clips override the
      Arrangement (Back to Arrangement lit). A range read from the Set is widened to the
      sixteenth-note grid the dialog counts in (range_rounded says so).
    - start_bar: Render start as a 1-based bar (1 = the start of the song). Takes
      precedence over start_beat.
    - start_beat: Render start in beats (0 = the start of the song). With a start or a
      length given, render_range is ignored (the reply says render_range "custom"); a
      length left out runs to the end of the Arrangement, and a start left out is 0.
    - length_bars: Render length in bars (not 1-based). Takes precedence over length_beats.
    - length_beats: Render length in beats. Start and length named by the caller must be
      a whole number of sixteenth notes (multiples of 0.25 beats); they are refused, not
      rounded.
    - rendered_track: "Main" (Live's own mix), "all" (All Individual Tracks: one file per
      track) or the name of one track (it must be the only track with that name; "Main",
      "Master" and "All Individual Tracks" always mean Live's own entries, so a track called
      that cannot be chosen by name). The choice is read back from the dialog.
    - normalize: Normalize the rendered file.
    - overwrite: Replace output_path if it already exists.
    - render_timeout: Seconds to wait for the render to finish.

    Fails before touching Live when Live logs no audio output device (the
    audio interface is unplugged), Live's UI is not safe to drive (Key Map or
    MIDI Map mode, a dialog open, Computer MIDI Keyboard on, or playing or
    recording; see get_ui_state), or an earlier call returned export_pending
    and Live is not yet shown clear of that export.

    Returns JSON: {ok, status, output_path, output_files, files, expected_path,
    format, sample_rate, bit_depth, normalize, create_video, render_range,
    rendered_track, dialog_range, range_rounded?, expected_seconds, rendered_seconds,
    length_verified, dialog_settings, file_info, steps, warnings, export_pending,
    selection_cleared?, transport_unchanged?, time_selection_after?, error?,
    dialog_text?}. files has one row per rendered file: {path, size_bytes,
    duration_seconds, sample_rate, bit_depth, channels, length_verified}; output_path is
    the first file (the only one unless rendered_track is "all"). dialog_range is the
    Render Start and Render Length Live showed when Export was pressed ({start, length,
    start_beats, length_beats}). On a failure after the Export dialog was asked for, the
    dialogs are cancelled and export_pending is false only once Live shows no export window;
    when true, the export may still be running in Live and nothing was changed. ok is true
    only when every file exists and has stopped growing, and it is false when afinfo reports
    a sample rate, bit depth or length other than the one requested. If afinfo cannot be run
    or does not report one of those, ok stays true and warnings says what was not verified.

    The file's length (afinfo) is checked against the typed range at the Set's current
    tempo: expected_seconds against rendered_seconds, with length_verified true when they
    match. A Set whose tempo changes inside the range fails that check even when the file
    is right. "selection" has no expected length and is never length-verified.
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
        start_bar=start_bar,
        start_beat=start_beat,
        length_bars=length_bars,
        length_beats=length_beats,
        rendered_track=rendered_track,
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
    start_bar: int = 0,
    start_beat: float = None,
    length_bars: int = 0,
    length_beats: float = None,
    rendered_track: str = "Main",
) -> str:
    """Render the Arrangement's video, with the rendered audio, via Export Audio/Video.

    Same UI flow, requirements and parameters as export_audio (typed range,
    rendered track, nothing changed while a render is pending), with Live's
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
        start_bar=start_bar,
        start_beat=start_beat,
        length_bars=length_bars,
        length_beats=length_beats,
        rendered_track=rendered_track,
    )
