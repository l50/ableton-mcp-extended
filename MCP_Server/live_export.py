"""Export audio (and video) from Ableton Live by driving its UI on macOS.

Live's File > Export Audio/Video is not part of the Live Object Model, so the
Remote Script cannot trigger it. This module drives the dialog from the host
side instead, through macOS UI scripting (System Events, via JavaScript for
Automation run by ``osascript``). ``live_ui_state`` and ``live_save`` share its
``JXADriver``; the LOM tools never import it.

Flow:

1. Check Live is running, is not already showing a dialog, has an audio
   output device (from its Log.txt) and is in a UI state safe to drive
   (``live_ui_state.require_safe_to_drive``), then bring it to the front.
2. Open File > Export Audio/Video (falling back to Cmd+Shift+R). Nothing
   else is clicked first: the Arrangement's selection and the loop brace are
   not touched, so the dialog's Render Start and Render Length are typed in
   rather than pre-filled from a selection.
3. Set the dialog's controls (Rendered Track: Main, a named track or All
   Individual Tracks; Encode PCM / MP3, File Type, Bit Depth, Sample Rate,
   Normalize, Create Video), re-reading each one and failing if it did not
   take. Then type Render Start and Render Length into their bar, beat and
   sixteenth fields and read both back; a field that did not take its value
   fails the export before Export is pressed.
4. Press Export, then fill in the macOS Save panel (folder and file name).
5. Poll until the render window is gone and every output file has stopped
   growing, then check each file with ``afinfo``: sample rate, bit depth and,
   when the caller says how long the range should be, length.
6. When the export is no longer pending, check the transport is unchanged and
   that no time selection exists (``live_ui_state.finish_ui_tool`` clears one
   a click left behind).

If any step from 2 on fails, the Save panel and Export dialog are
cancelled and the run waits until Live shows no window the export opened.
When that cannot be confirmed, the result says ``export_pending``: Live may
still be exporting, so the caller must not change the Set until it is clear.
Nothing is restored or changed while a render may be pending.

A run that cannot prove the output file was written is a failure. Nothing
here reports success from the UI alone. Render Start and Render Length are
read back from the dialog before Export, and the file's length is checked
afterwards against the range that was typed.

The control labels come from Live 12's accessibility tree (the descriptions
VoiceOver reads), e.g. the Sample Rate chooser is an ``AXPopUpButton``
described as "Sample Rate" and the Normalize toggle is an ``AXButton``
(subrole ``AXSwitch``) described as "Normalize" with value "On"/"Off".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from MCP_Server import live_ui_state

LIVE_BUNDLE_ID = "com.ableton.live"

# Windows Live showed before the last export this process left pending, or
# None. While set, no export may start until Live is shown clear of it.
_pending_baseline: Optional[List[str]] = None

EXPORT_DIALOG_TITLE = "Export Audio/Video"
SAVE_PANEL_TITLE = "Save"
FILE_MENU = "File"
EXPORT_MENU_ITEM = "Export Audio/Video"
# Live's documented shortcut for File > Export Audio/Video (manual 5.1.3).
EXPORT_SHORTCUT = {"key": "r", "modifiers": ["command down", "shift down"]}

# Accessibility descriptions of the Export dialog controls (Live 12).
RENDERED_TRACK = "Rendered Track Chooser"
RENDER_START = "Render Start"
RENDER_LENGTH = "Render Length"
ENCODE_PCM = "Encode PCM"
FILE_TYPE = "File Type"
BIT_DEPTH = "Recording Bit Depth"
ENCODE_MP3 = "Encode MP3"
SAMPLE_RATE = "Sample Rate"
NORMALIZE = "Normalize"
CREATE_VIDEO = "Create Video with Rendered Audio"
EXPORT_BUTTON = "Export"
CANCEL_BUTTON = "Cancel"

# Live calls the master bus "Main" from Live 12 on and "Master" before that.
MAIN_TRACK_VALUES = ["Main", "Master"]
# The Rendered Track chooser's entry that renders one file per track.
ALL_TRACKS_ENTRY = "All Individual Tracks"
RENDERED_TRACK_ALL = "all"
RENDERED_TRACK_MAIN = "main"

PCM_FORMATS = {"wav": "WAV", "aiff": "AIFF", "flac": "FLAC"}
FORMATS = ("wav", "aiff", "flac", "mp3")
# Extensions Live writes for each format; the first is the one we expect.
FORMAT_EXTENSIONS = {
    "wav": (".wav",),
    "aiff": (".aif", ".aiff"),
    "flac": (".flac",),
    "mp3": (".mp3",),
}
SAMPLE_RATES = (22050, 32000, 44100, 48000, 88200, 96000, 176400, 192000)
BIT_DEPTHS = (16, 24, 32)
RENDER_RANGES = ("arrangement", "loop", "selection")
VIDEO_EXTENSIONS = (".mov", ".mp4", ".m4v", ".avi")

# Words that mark a Live window's text as an error message, not progress.
_ERROR_WORDS = re.compile(
    r"\b(error|failed|fail|could not|couldn't|cannot|can't|unable|not enough|invalid|denied)\b",
    re.IGNORECASE,
)

# Live has no LOM property for its audio device, but it logs it to Log.txt in
# its preferences folder: each device change writes the current one, e.g.
# "info: Audio In Out: Output Device: No Device" once the interface is
# unplugged, and a launch without the interface writes "info: Message Box:
# Audio is disabled. Please choose an audio output device ...". Only lines
# after the latest "Started: Live" line describe the Live running now.
LIVE_PREFS_ROOT = os.path.expanduser("~/Library/Preferences/Ableton")
_LIVE_STARTED = re.compile(r"info: Started: Live ")
_DEVICE_EVENT = re.compile(
    r"info: (?:Audio In Out: Output Device: (?P<device>.*?)|Message Box: (?P<disabled>Audio is disabled)\b.*?)\s*$",
    re.MULTILINE,
)
NO_DEVICE = "No Device"

DEFAULT_DIALOG_TIMEOUT = 15.0
DEFAULT_RENDER_TIMEOUT = 900.0
DEFAULT_POLL_INTERVAL = 1.0
STABLE_POLLS = 3
# Slack between the rendered and the expected length: covers MP3 encoder
# padding and the dialog's sixteenth-note resolution at any normal tempo.
LENGTH_TOLERANCE_SECONDS = 0.5
# A beat in Live's song time is a quarter note; the dialog counts in sixteenths.
SIXTEENTHS_PER_BEAT = 4
SIXTEENTHS_PER_WHOLE_NOTE = 16


# ── bar, beat and sixteenth fields ───────────────────────────────


def _sixteenths_per_beat(signature: Tuple[int, int]) -> int:
    """Sixteenth notes in one beat of the signature (a beat is 1/denominator of a whole note)."""
    return SIXTEENTHS_PER_WHOLE_NOTE // int(signature[1])


def beats_to_fields(
    beats: float, signature: Tuple[int, int], one_based: bool
) -> Tuple[int, int, int]:
    """Quarter-note beats as the (bar, beat, sixteenth) Live's dialog fields show.

    Render Start counts from 1.1.1 (``one_based``); Render Length counts from
    0.0.0, so 14 bars in 4/4 is 14.0.0. ``beats`` must be a whole number of
    sixteenths; the caller has checked.
    """
    per_beat = _sixteenths_per_beat(signature)
    per_bar = int(signature[0]) * per_beat
    total = int(round(beats * SIXTEENTHS_PER_BEAT))
    bars, rest = divmod(total, per_bar)
    beat, sixteenth = divmod(rest, per_beat)
    offset = 1 if one_based else 0
    return bars + offset, beat + offset, sixteenth + offset


def fields_to_beats(
    fields: Sequence[int], signature: Tuple[int, int], one_based: bool
) -> float:
    """The inverse of ``beats_to_fields``: dialog fields back to quarter-note beats."""
    per_beat = _sixteenths_per_beat(signature)
    per_bar = int(signature[0]) * per_beat
    offset = 1 if one_based else 0
    bars, beat, sixteenth = (int(v) - offset for v in fields)
    return (bars * per_bar + beat * per_beat + sixteenth) / SIXTEENTHS_PER_BEAT


def format_fields(fields: Sequence[int]) -> str:
    return ".".join(str(int(v)) for v in fields)


class ExportError(Exception):
    """A step of the export failed; the message is reported verbatim."""


class UIScriptingError(ExportError):
    """osascript itself failed (permissions, timeout, bad output)."""


# ── request and plan ─────────────────────────────────────────────


@dataclass
class ExportRequest:
    """Validated parameters for one export."""

    output_path: str
    format: str = "wav"
    sample_rate: int = 44100
    bit_depth: int = 24
    normalize: bool = False
    create_video: bool = False
    overwrite: bool = False
    render_timeout: float = DEFAULT_RENDER_TIMEOUT
    dialog_timeout: float = DEFAULT_DIALOG_TIMEOUT
    # How long each rendered file should be, when the caller knows the range.
    expected_seconds: Optional[float] = None
    # What the Rendered Track chooser is set to: "Main" (Live's own bus), the
    # name of one track, or "all" (All Individual Tracks: one file per track).
    rendered_track: str = "Main"
    # The range typed into Render Start / Render Length, in beats from the
    # start of the song; both or neither. Neither leaves the dialog's own
    # pre-filled range alone (the current time selection).
    start_beats: Optional[float] = None
    length_beats: Optional[float] = None
    # The Set's time signature, which sets what a bar and a beat are in the
    # dialog's bar.beat.sixteenth fields.
    signature: Tuple[int, int] = (4, 4)
    # For rendered_track "all": how many files Live should write, when known.
    expected_files: Optional[int] = None
    # True when the typed range was widened to the sixteenth grid.
    range_rounded: bool = False

    # Filled in by validate().
    directory: str = field(default="", init=False)
    stem: str = field(default="", init=False)
    # "main", "all" or "track": which kind of Rendered Track choice this is.
    track_mode: str = field(default=RENDERED_TRACK_MAIN, init=False)
    track_name: str = field(default="", init=False)

    @property
    def typed_range(self) -> bool:
        return self.start_beats is not None

    def validate(self) -> "ExportRequest":
        self._validate_rendered_track()
        self._validate_range()
        return self._validate_file_options()

    def _validate_rendered_track(self) -> None:
        choice = str(self.rendered_track if self.rendered_track is not None else "").strip()
        if not choice:
            raise ValueError("rendered_track must be 'Main', 'all' or a track name (got '')")
        lowered = choice.lower()
        if lowered in tuple(n.lower() for n in MAIN_TRACK_VALUES):
            self.track_mode, self.track_name = RENDERED_TRACK_MAIN, ""
        elif lowered in (RENDERED_TRACK_ALL, ALL_TRACKS_ENTRY.lower()):
            self.track_mode, self.track_name = RENDERED_TRACK_ALL, ""
        else:
            self.track_mode, self.track_name = "track", choice

    def _validate_range(self) -> None:
        if (self.start_beats is None) != (self.length_beats is None):
            raise ValueError("start_beats and length_beats must be given together")
        if self.start_beats is None:
            return
        numerator, denominator = self.signature
        if int(numerator) < 1 or int(denominator) not in (1, 2, 4, 8, 16):
            raise ValueError(f"unsupported time signature {numerator}/{denominator}")
        try:
            start, length = float(self.start_beats), float(self.length_beats)
        except (TypeError, ValueError):
            raise ValueError("start_beats and length_beats must be numbers")
        if start < 0:
            raise ValueError(f"start_beats must be >= 0 (got {start:g})")
        if length <= 0:
            raise ValueError(f"length_beats must be > 0 (got {length:g})")
        for name, value in (("start_beats", start), ("length_beats", length)):
            if abs(value * SIXTEENTHS_PER_BEAT - round(value * SIXTEENTHS_PER_BEAT)) > 1e-6:
                raise ValueError(
                    f"{name} {value:g} is not a whole number of sixteenth notes; Render Start "
                    "and Render Length can only be typed to a sixteenth (a multiple of 0.25 beats)"
                )
        self.start_beats, self.length_beats = start, length

    def _validate_file_options(self) -> "ExportRequest":
        fmt = str(self.format or "").strip().lower().lstrip(".")
        if fmt == "aif":
            fmt = "aiff"
        if fmt not in FORMATS:
            raise ValueError(
                f"format must be one of {', '.join(FORMATS)} (got '{self.format}')"
            )
        self.format = fmt

        try:
            self.sample_rate = int(self.sample_rate)
        except (TypeError, ValueError):
            raise ValueError(f"sample_rate must be a number (got '{self.sample_rate}')")
        if self.sample_rate not in SAMPLE_RATES:
            raise ValueError(
                "sample_rate must be one Live offers: "
                + ", ".join(str(r) for r in SAMPLE_RATES)
                + f" (got {self.sample_rate})"
            )

        if fmt != "mp3":
            try:
                self.bit_depth = int(self.bit_depth)
            except (TypeError, ValueError):
                raise ValueError(f"bit_depth must be 16, 24 or 32 (got '{self.bit_depth}')")
            if self.bit_depth not in BIT_DEPTHS:
                raise ValueError(f"bit_depth must be 16, 24 or 32 (got {self.bit_depth})")

        for name in ("render_timeout", "dialog_timeout"):
            value = float(getattr(self, name))
            if value <= 0:
                raise ValueError(f"{name} must be greater than 0 (got {value})")
            setattr(self, name, value)

        raw = str(self.output_path or "").strip()
        if not raw:
            raise ValueError("output_path is required (an absolute file path)")
        path = os.path.expanduser(raw)
        if not os.path.isabs(path):
            raise ValueError(f"output_path must be absolute (got '{raw}')")
        if raw.endswith(os.sep):
            raise ValueError(f"output_path must name a file, not a folder (got '{raw}')")
        directory, filename = os.path.split(path)
        stem, ext = os.path.splitext(filename)
        known = {e for exts in FORMAT_EXTENSIONS.values() for e in exts}
        if ext.lower() in known:
            if ext.lower() not in FORMAT_EXTENSIONS[fmt]:
                raise ValueError(
                    f"output_path extension '{ext}' does not match format '{fmt}'"
                )
        elif ext:
            # Not an audio extension (e.g. "Song v1.2"): keep it in the name.
            stem = filename
        self.directory = directory
        self.stem = stem
        return self

    @property
    def expected_paths(self) -> List[str]:
        # Live adds the track's name to the name given in the Save panel when
        # one track is rendered ("<name> <track>.wav"); the Main mix keeps it.
        stem = f"{self.stem} {self.track_name}" if self.track_mode == "track" else self.stem
        return [os.path.join(self.directory, stem + ext) for ext in FORMAT_EXTENSIONS[self.format]]

    @property
    def output_file(self) -> str:
        return self.expected_paths[0]


def check_preconditions(request: ExportRequest, platform: Optional[str] = None) -> None:
    """Reject a validated request that cannot work, before Live is touched."""
    platform = platform or sys.platform
    if platform != "darwin":
        raise ExportError(
            f"Live export drives the macOS UI and is not supported on '{platform}'"
        )
    if not os.path.isdir(request.directory):
        raise ExportError(f"output folder does not exist: {request.directory}")
    if not os.access(request.directory, os.W_OK | os.X_OK):
        raise ExportError(f"output folder is not writable: {request.directory}")
    existing = [p for p in request.expected_paths if os.path.exists(p)]
    if existing and not request.overwrite:
        raise ExportError(
            f"output file already exists: {existing[0]} (pass overwrite=True to replace it)"
        )


def _rendered_track_step(request: ExportRequest) -> Dict[str, Any]:
    """The Rendered Track chooser step for this request.

    The chooser's list is positioned so the current choice sits over the
    control, so its rows move with the current choice and are never picked by
    position except Live's own bus, which always leads the list. Everything
    else is picked by name from the menu's items.
    """
    if request.track_mode == RENDERED_TRACK_ALL:
        return {"op": "set_choice", "label": RENDERED_TRACK, "values": [ALL_TRACKS_ENTRY]}
    if request.track_mode == "track":
        return {"op": "set_choice", "label": RENDERED_TRACK, "values": [request.track_name]}
    # Live's own bus leads this list and the tracks after it can have its
    # name, so it is taken by position: the first entry, whatever is set.
    return {"op": "set_choice", "label": RENDERED_TRACK, "values": list(MAIN_TRACK_VALUES),
            "first": True}


def build_dialog_plan(request: ExportRequest) -> List[Dict[str, Any]]:
    """Map export parameters to the Export dialog controls to set, in order.

    Order matters: File Type and Bit Depth are only live while Encode PCM is on.
    The Render Start / Render Length fields are typed after these steps (see
    ``LiveExporter._type_range``), once nothing else can change them.
    """
    plan: List[Dict[str, Any]] = [_rendered_track_step(request)]
    if request.format in PCM_FORMATS:
        plan += [
            {"op": "set_toggle", "label": ENCODE_PCM, "on": True},
            {"op": "set_choice", "label": FILE_TYPE, "values": [PCM_FORMATS[request.format]]},
            {"op": "set_choice", "label": BIT_DEPTH, "values": [str(request.bit_depth)]},
            {"op": "set_toggle", "label": ENCODE_MP3, "on": False},
        ]
    else:
        plan += [
            {"op": "set_toggle", "label": ENCODE_MP3, "on": True},
            {"op": "set_toggle", "label": ENCODE_PCM, "on": False},
        ]
    plan += [
        {"op": "set_choice", "label": SAMPLE_RATE, "values": [str(request.sample_rate)]},
        {"op": "set_toggle", "label": NORMALIZE, "on": bool(request.normalize)},
        # Live disables this toggle when the Arrangement has no video clips;
        # turning it off is then a no-op, turning it on is an error.
        {"op": "set_toggle", "label": CREATE_VIDEO, "on": bool(request.create_video),
         "allow_disabled_off": True},
    ]
    return plan


# ── osascript driver ─────────────────────────────────────────────

# JavaScript for Automation. Takes one JSON argument {op, ...}, prints JSON.
_JXA_BASE = r"""
function run(argv) {
  var cmd = JSON.parse(argv[0]);
  try {
    var out = dispatch(cmd);
    out.ok = out.ok === undefined ? true : out.ok;
    return JSON.stringify(out);
  } catch (e) {
    return JSON.stringify({ok: false, error: String((e && e.message) || e),
                           code: (e && e.errorNumber) || null});
  }
}

var SE = Application('System Events');

function safe(f, dflt) {
  try { var v = f(); return (v === undefined || v === null) ? dflt : v; }
  catch (e) { return dflt; }
}
function norm(s) {
  return String(s === undefined || s === null ? '' : s).toLowerCase()
    .replace(/…/g, '...').replace(/[\s,]+/g, '');
}
function proc(cmd) {
  var ps = SE.processes.whose({bundleIdentifier: cmd.bundle});
  if (ps.length === 0) throw new Error('Ableton Live is not running');
  return ps[0];
}
function windowInfo(w) {
  return {
    name: safe(function () { return w.name(); }, ''),
    subrole: safe(function () { return w.subrole(); }, ''),
    modal: safe(function () { return w.attributes.byName('AXModal').value(); }, false),
    sheets: safe(function () { return w.sheets.length; }, 0)
  };
}
function windowsNamed(p, name) {
  return p.windows().filter(function (w) {
    return safe(function () { return w.name(); }, '') === name;
  });
}
function findWindow(p, name) {
  var ws = windowsNamed(p, name);
  if (ws.length === 0) throw new Error("Live window '" + name + "' is not open");
  return ws[0];
}
function labelsOf(el) {
  return [safe(function () { return el.description(); }, ''),
          safe(function () { return el.title(); }, ''),
          safe(function () { return el.name(); }, '')];
}
function findControl(w, label, roles) {
  var all = w.entireContents();
  var want = norm(label);
  for (var i = 0; i < all.length; i++) {
    var role = safe(function () { return all[i].role(); }, '');
    if (roles.indexOf(role) < 0) continue;
    var ls = labelsOf(all[i]);
    for (var j = 0; j < ls.length; j++) if (norm(ls[j]) === want) return all[i];
  }
  throw new Error("control '" + label + "' not found in '" + safe(function () { return w.name(); }, '') + "'");
}
// The Save panel holds the folder's file list: thousands of elements, each
// read with its own Apple Event, so walking entireContents() there takes
// longer than the op timeout (observed: 2263 elements, over 45 s). This
// search walks the window breadth first and does not go into lists, tables
// or scroll areas, which is where the file browser lives; the panel's own
// fields and buttons are found in under a second.
var HEAVY_ROLES = ['AXOutline', 'AXTable', 'AXList', 'AXBrowser', 'AXScrollArea',
                   'AXGrid', 'AXRow', 'AXColumn', 'AXCell'];
function findControlShallow(w, label, roles) {
  var want = norm(label), queue = [w];
  while (queue.length) {
    var kids = safe(function () { return queue.shift().uiElements(); }, []);
    for (var i = 0; i < kids.length; i++) {
      var role = safe(function () { return kids[i].role(); }, '');
      if (roles.indexOf(role) >= 0) {
        var ls = labelsOf(kids[i]);
        for (var j = 0; j < ls.length; j++) if (norm(ls[j]) === want) return kids[i];
      }
      if (HEAVY_ROLES.indexOf(role) < 0) queue.push(kids[i]);
    }
  }
  throw new Error("control '" + label + "' not found in '" + safe(function () { return w.name(); }, '') + "'");
}
// Render Start and Render Length are each three AXSliders (bars, beats,
// sixteenths) described "Start of Rendered Audio File (Bar)", "(Beat)" and
// "(Sixteenths)", or "Length of Rendered Audio File (...)". Live ignores
// AXValue writes and clicks on them, and Return in the dialog presses Export,
// so a value is reached with the sliders' own AXIncrement / AXDecrement
// actions (one step each) and read back after every step.
var TIME_FIELDS = {
  'renderstart': 'Start of Rendered Audio File',
  'renderlength': 'Length of Rendered Audio File'
};
var TIME_UNITS = ['Bar', 'Beat', 'Sixteenths'];
var MAX_NUDGES = 4000;
function firstInt(v) {
  var m = /-?\d+/.exec(String(v === undefined || v === null ? '' : v));
  return m ? parseInt(m[0], 10) : null;
}
function timeFieldParts(w, label) {
  var prefix = TIME_FIELDS[norm(label)];
  if (!prefix) throw new Error("'" + label + "' is not a time field (Render Start or Render Length)");
  var all = w.entireContents(), parts = [null, null, null];
  for (var i = 0; i < all.length; i++) {
    if (safe(function () { return all[i].role(); }, '') !== 'AXSlider') continue;
    var d = safe(function () { return all[i].description(); }, '');
    for (var u = 0; u < 3; u++)
      if (d === prefix + ' (' + TIME_UNITS[u] + ')') parts[u] = all[i];
  }
  for (var u = 0; u < 3; u++)
    if (!parts[u]) throw new Error("the three parts of '" + label + "' were not found in '" +
      safe(function () { return w.name(); }, '') + "'");
  return parts;
}
function readParts(parts) {
  return parts.map(function (el) { return firstInt(safe(function () { return el.value(); }, null)); });
}
// Step one slider to a value. Beats and sixteenths wrap round, bars stop at
// their ends, so a step that changes nothing means the value cannot be reached.
function nudgeTo(el, target, what) {
  var current = firstInt(safe(function () { return el.value(); }, null));
  for (var n = 0; current !== target && n < MAX_NUDGES; n++) {
    var action = (current === null || target > current) ? 'AXIncrement' : 'AXDecrement';
    el.actions.byName(action).perform();
    var next = firstInt(safe(function () { return el.value(); }, null));
    if (next === current)
      throw new Error(what + ' stays at ' + current + ' and cannot be set to ' + target);
    current = next;
  }
  if (current !== target) throw new Error(what + ' did not reach ' + target + ' in ' + MAX_NUDGES + ' steps');
}
function press(el) {
  try { el.actions.byName('AXPress').perform(); } catch (e) { SE.click(el); }
}
function toggleState(el) {
  var v = norm(safe(function () { return el.value(); }, ''));
  if (v === 'on') return true;
  if (v === 'off') return false;
  return null;
}
function matches(value, values) {
  var v = norm(value);
  for (var i = 0; i < values.length; i++) if (norm(values[i]) === v) return true;
  return false;
}
function menuItemsOf(p) {
  // Live 12 opens a chooser's list as a new untitled AXUnknown window of
  // AXMenuItems (the current choice is prefixed with a check mark). Untitled
  // AXUnknown windows can already be open, so scan them all for menu items
  // instead of looking for a window that is new by name.
  var items = [];
  var ws = safe(function () { return p.windows(); }, []);
  for (var i = 0; i < ws.length; i++) {
    if (safe(function () { return ws[i].subrole(); }, '') !== 'AXUnknown') continue;
    var cs = safe(function () { return ws[i].entireContents(); }, []);
    for (var j = 0; j < cs.length; j++) {
      if (safe(function () { return cs[j].role(); }, '') === 'AXMenuItem') items.push(cs[j]);
    }
  }
  return items;
}
// Live marks the current menu choice with a check mark whose exact
// characters vary (U+2714 or U+2713, maybe a variation selector, maybe a
// space), so drop everything before the first letter or digit.
function stripMark(name) {
  return name.replace(/^[^\p{L}\p{N}]+/u, '');
}
function pickFromMenu(p, el, values, firstOnly) {
  press(el);
  delay(0.6);
  var items = menuItemsOf(p);
  var seen = [];
  for (var i = 0; i < items.length; i++) {
    var name = String(safe(function () { return items[i].name(); }, ''));
    if (!name) continue;
    seen.push(name);
    var bare = stripMark(name);
    if (matches(bare, values)) {
      press(items[i]);
      delay(0.4);
      return {seen: seen, picked: bare};
    }
    if (firstOnly) break;
  }
  SE.keyCode(53);  // Esc closes the open menu, not the dialog
  delay(0.3);
  return {seen: seen, picked: null};
}
function controlList(w) {
  var all = w.entireContents(), out = [];
  for (var i = 0; i < all.length; i++) {
    var role = safe(function () { return all[i].role(); }, '');
    if (['AXButton', 'AXPopUpButton', 'AXSlider', 'AXTextField'].indexOf(role) < 0) continue;
    out.push({role: role, label: safe(function () { return all[i].description(); }, ''),
              value: String(safe(function () { return all[i].value(); }, '')),
              enabled: safe(function () { return all[i].enabled(); }, true)});
  }
  return out;
}
function textsOf(w) {
  var all = w.entireContents(), out = [];
  for (var i = 0; i < all.length && out.length < 40; i++) {
    if (safe(function () { return all[i].role(); }, '') !== 'AXStaticText') continue;
    var v = safe(function () { return all[i].value(); }, '');
    if (v) out.push(String(v));
  }
  return out;
}
// Names the Save panel's Where pop-up may show for cmd.directory: the
// on-disk name and the Finder display name, which differ for localized
// folders and iCloud Drive.
function folderNames(cmd) {
  var names = [cmd.folder_name];
  names.push(safe(function () {
    ObjC.import('Foundation');
    return $.NSFileManager.defaultManager.displayNameAtPath(cmd.directory).js;
  }, ''));
  names.push(safe(function () {
    ObjC.import('Foundation');
    var ref = Ref();
    $.NSURL.fileURLWithPath(cmd.directory).getResourceValueForKeyError(ref, $.NSURLLocalizedNameKey, null);
    return ObjC.unwrap(ref[0]);
  }, ''));
  return names.filter(function (n, i) { return n && names.indexOf(n) === i; });
}
// A synced Desktop or Documents folder shows as "Desktop — iCloud".
function folderMatches(where, names) {
  var bare = String(where).replace(/\s+[\u2014\u2013-]\s+iCloud$/, '');
  return matches(where, names) || matches(bare, names);
}
// Keystrokes go to whichever app is frontmost, and Live can lose the front
// while the Save panel is up (a Go to Folder that never shows with Live in
// the background), so bring Live back first and send nothing while it is
// not frontmost.
function requireFront(p, cmd, keys) {
  var front = safe(function () { return p.frontmost(); }, false);
  for (var t = 0; t < 20 && !front; t++) {
    if (t % 5 === 0) {
      try { p.frontmost = true; }
      catch (e) {
        if (safe(function () {
          return SE.processes.whose({bundleIdentifier: cmd.bundle}).length;
        }, null) === 0)
          throw new Error('Ableton Live is not running, so ' + keys + ' was not sent');
        throw e;
      }
    }
    delay(0.1);
    front = safe(function () { return p.frontmost(); }, false);
  }
  if (!front) throw new Error('Ableton Live was not frontmost, so ' + keys + ' was not sent');
}
function requireKeystrokeTarget(target, targetName, keys) {
  if (!safe(target, false))
    throw new Error(targetName + ' was no longer open, so ' + keys + ' was not sent');
}
function requireWindowFocus(w, target, targetName, keys) {
  for (var t = 0; t < 20; t++) {
    requireKeystrokeTarget(target, targetName, keys);
    if (safe(function () { return w.focused(); }, false)) return;
    delay(0.1);
  }
  requireKeystrokeTarget(target, targetName, keys);
  if (safe(function () { return w.focused(); }, false)) return;
  throw new Error(targetName + ' did not get keyboard focus, so ' + keys + ' was not sent');
}
function clickMenu(p, path) {
  var bar = p.menuBars[0];
  var top = bar.menuBarItems.byName(path[0]);
  var items = top.menus[0].menuItems();
  for (var i = 0; i < items.length; i++) {
    var n = safe(function () { return items[i].name(); }, '');
    if (n && norm(n).indexOf(norm(path[1])) === 0) {
      if (!safe(function () { return items[i].enabled(); }, false))
        return {ok: false, error: "menu item '" + path[0] + " > " + n + "' is disabled", disabled: true};
      items[i].click();
      return {clicked: n};
    }
  }
  return {ok: false, error: "menu item '" + path[0] + " > " + path[1] + "' not found", missing: true};
}

function dispatch(cmd) {
  if (cmd.op === 'status') {
    var ps = SE.processes.whose({bundleIdentifier: cmd.bundle});
    if (ps.length === 0) return {running: false, frontmost: false, windows: []};
    var p = ps[0];
    return {running: true, frontmost: safe(function () { return p.frontmost(); }, false),
            windows: p.windows().map(windowInfo)};
  }
  if (cmd.op === 'ui_state') {
    var live = SE.processes.whose({bundleIdentifier: cmd.bundle});
    if (live.length === 0) return {running: false};
    return uiState(live[0], cmd);
  }
  var p = proc(cmd);
  switch (cmd.op) {
    case 'activate':
      Application(cmd.bundle).activate();
      delay(0.5);
      return {frontmost: safe(function () { return p.frontmost(); }, false)};
    case 'menu':
      return clickMenu(p, cmd.path);
    case 'keystroke':
      SE.keystroke(cmd.key, {using: cmd.modifiers});
      return {};
    case 'controls':
      return {controls: controlList(findWindow(p, cmd.window))};
    case 'get_time_field': {
      var w = findWindow(p, cmd.window);
      return {fields: readParts(timeFieldParts(w, cmd.label))};
    }
    case 'set_time_field': {
      var w = findWindow(p, cmd.window);
      var parts = timeFieldParts(w, cmd.label);
      if (cmd.fields.length !== 3) return {ok: false, error: 'a time field needs three parts'};
      var before = readParts(parts);
      for (var pass = 0; pass < 2; pass++)
        for (var i = 0; i < 3; i++)
          if ((cmd.fields[i] > 0) === (pass === 0))
            nudgeTo(parts[i], cmd.fields[i], cmd.label + ' ' + TIME_UNITS[i].toLowerCase());
      return {before: before, after: readParts(parts)};
    }
    case 'texts': {
      // Every window with this name: a name does not single one out, and
      // Live's untitled windows all share the name ''.
      var texts = [];
      windowsNamed(p, cmd.window).forEach(function (x) { texts = texts.concat(textsOf(x)); });
      return {texts: texts};
    }
    case 'set_toggle': {
      var w = findWindow(p, cmd.window);
      var el = findControl(w, cmd.label, ['AXButton']);
      var before = toggleState(el);
      var enabled = safe(function () { return el.enabled(); }, true);
      if (before === cmd.on) return {before: before, after: before, enabled: enabled};
      if (!enabled) return {ok: false, before: before, after: before, enabled: false,
                            error: "'" + cmd.label + "' is disabled in the Export dialog"};
      press(el);
      delay(0.3);
      return {before: before, after: toggleState(el), enabled: enabled};
    }
    case 'set_choice': {
      var w = findWindow(p, cmd.window);
      var el = findControl(w, cmd.label, ['AXPopUpButton']);
      var before = String(safe(function () { return el.value(); }, ''));
      // cmd.first: only the list's first entry will do, and a later entry can
      // have its name, so the current value says nothing.
      if (!cmd.first && matches(before, cmd.values)) return {before: before, after: before};
      if (!safe(function () { return el.enabled(); }, true))
        return {ok: false, before: before, after: before,
                error: "'" + cmd.label + "' is disabled in the Export dialog"};
      // Live's choosers do not accept AXValue writes; pick from the menu.
      var pick = pickFromMenu(p, el, cmd.values, cmd.first);
      var out = {before: before, after: String(safe(function () { return el.value(); }, '')),
                 seen: pick.seen.slice(0, 40)};
      if (pick.picked !== null) {
        out.picked = pick.picked;
      } else if (cmd.first) {
        out.ok = false;
        out.error = "the first entry in the '" + cmd.label + "' list is " +
          (pick.seen.length ? "'" + pick.seen[0] + "'" : 'missing') +
          ', expected ' + cmd.values.join(' or ');
      }
      return out;
    }
    case 'cancel': {
      // Close an export window with its Cancel button. A sheet left on it
      // (Go to Folder, Replace) blocks the window's own buttons, so it is
      // dismissed first: its Cancel button, else Esc with Live frontmost.
      // With no Cancel button found, Esc cancels the window itself.
      if (windowsNamed(p, cmd.window).length === 0) return {was_open: false, closed: true};
      var w = windowsNamed(p, cmd.window)[0];
      for (var t = 0; t < 3 && safe(function () { return w.sheets.length; }, 0); t++) {
        var s = w.sheets[0];
        var sc = safe(function () { return findControlShallow(s, 'Cancel', ['AXButton']); }, null);
        if (sc) press(sc);
        else { requireFront(p, cmd, 'Esc'); SE.keyCode(53); }
        delay(0.5);
      }
      if (windowsNamed(p, cmd.window).length === 0) return {was_open: true, closed: true};
      if (safe(function () { return w.sheets.length; }, 0))
        return {ok: false, was_open: true, closed: false,
                error: "a sheet on '" + cmd.window + "' did not close"};
      var cancel = safe(function () { return findControlShallow(w, 'Cancel', ['AXButton']); }, null);
      if (cancel) {
        press(cancel);
      } else {
        safe(function () { w.actions.byName('AXRaise').perform(); }, null);
        requireFront(p, cmd, 'Esc');
        requireKeystrokeTarget(function () { return w.name() === cmd.window; }, cmd.window, 'Esc');
        SE.keyCode(53);
      }
      for (var t = 0; t < 30; t++) {
        delay(0.1);
        if (windowsNamed(p, cmd.window).length === 0) return {was_open: true, closed: true};
      }
      return {ok: false, was_open: true, closed: false,
              error: "'" + cmd.window + "' was still open after pressing Cancel"};
    }
    case 'press': {
      var w = findWindow(p, cmd.window);
      var el = findControl(w, cmd.label, ['AXButton']);
      if (!safe(function () { return el.enabled(); }, true))
        return {ok: false, error: "'" + cmd.label + "' button is disabled"};
      press(el);
      return {pressed: cmd.label};
    }
    case 'save_panel': {
      var w = findWindow(p, cmd.window);
      var nameField = findControlShallow(w, 'Save As:', ['AXTextField']);
      nameField.value = cmd.filename;
      // Go to the folder: Cmd+Shift+G opens a path sheet on the panel. The
      // sheet can be slow, so wait up to 5 s, then re-send the shortcut once
      // and wait up to 5 s more before giving up.
      var sheet = null;
      for (var attempt = 0; attempt < 2 && !sheet; attempt++) {
        if (safe(function () { return w.name(); }, null) !== cmd.window) break;
        requireFront(p, cmd, 'Cmd+Shift+G');
        safe(function () { w.actions.byName('AXRaise').perform(); }, null);
        requireKeystrokeTarget(function () { return w.name() === cmd.window; },
                               'the Save panel', 'Cmd+Shift+G');
        SE.keystroke('g', {using: ['command down', 'shift down']});
        for (var t = 0; t < 50 && !sheet; t++) {
          delay(0.1);
          sheet = safe(function () { return w.sheets.length ? w.sheets[0] : null; }, null);
        }
      }
      if (!sheet) return {ok: false, error: 'the Go to Folder sheet did not open in the Save panel'};
      var typed = false;
      var parts = sheet.entireContents();
      for (var i = 0; i < parts.length && !typed; i++) {
        var role = safe(function () { return parts[i].role(); }, '');
        if (role === 'AXTextField' || role === 'AXComboBox') {
          try { parts[i].value = cmd.directory; typed = true; } catch (e) {}
        }
      }
      if (!typed) {
        requireFront(p, cmd, 'the folder path');
        var pathTarget = function () {
          return w.name() === cmd.window && w.sheets.length;
        };
        requireWindowFocus(w, pathTarget, 'the Go to Folder sheet', 'the folder path');
        SE.keystroke(cmd.directory);
      }
      delay(0.3);
      requireFront(p, cmd, 'Return');
      requireKeystrokeTarget(function () {
        return w.name() === cmd.window && w.sheets.length;
      }, 'the Go to Folder sheet', 'Return');
      SE.keyCode(36);  // Return: go
      for (var t = 0; t < 30; t++) {
        delay(0.1);
        if (!safe(function () { return w.sheets.length; }, 0)) break;
      }
      if (safe(function () { return w.sheets.length; }, 0))
        return {ok: false, error: "the Save panel could not open folder '" + cmd.directory + "'",
                texts: textsOf(w.sheets[0])};
      delay(0.3);
      nameField.value = cmd.filename;
      var where = safe(function () { return findControlShallow(w, 'Where:', ['AXPopUpButton']).value(); }, '');
      if (cmd.folder_name && where && !folderMatches(where, folderNames(cmd)))
        return {ok: false, where: where,
                error: "the Save panel is in folder '" + where + "', expected '" +
                  folderNames(cmd).join("' or '") + "'"};
      var name = String(safe(function () { return nameField.value(); }, ''));
      if (name !== cmd.filename)
        return {ok: false, error: "the Save panel file name is '" + name + "', expected '" + cmd.filename + "'"};
      press(findControlShallow(w, 'Save', ['AXButton']));
      // An existing file brings up a Replace confirmation sheet.
      for (var t = 0; t < 20; t++) {
        delay(0.1);
        var s = safe(function () { return w.sheets.length ? w.sheets[0] : null; }, null);
        if (!s) continue;
        var replace = safe(function () { return findControl(s, 'Replace', ['AXButton']); }, null);
        if (!replace) continue;
        if (cmd.replace) { press(replace); return {where: where, replaced: true}; }
        press(findControl(s, 'Cancel', ['AXButton']));
        return {ok: false, where: where, error: 'the file already exists and overwrite is off'};
      }
      return {where: where, replaced: false};
    }
  }
  var extra = dispatchUi(cmd, p);
  if (extra !== undefined) return extra;
  throw new Error('unknown op ' + cmd.op);
}
"""

# The UI-state ops (Key/MIDI Map mode, dialogs, focus, selection) live with
# their Python side in live_ui_state.py and share this script's helpers.
JXA_DRIVER = _JXA_BASE + live_ui_state.UI_STATE_JXA


_PERMISSION_HINT = (
    "macOS blocked UI scripting. Grant the app that runs the MCP server "
    "(e.g. Claude, Terminal) Accessibility access in System Settings > "
    "Privacy & Security > Accessibility, and allow it to control "
    "System Events under Automation."
)


class JXADriver:
    """Runs JXA_DRIVER through osascript, one op per call."""

    def __init__(
        self,
        bundle_id: str = LIVE_BUNDLE_ID,
        runner: Optional[Callable[..., Any]] = None,
        timeout: float = 45.0,
    ):
        self.bundle_id = bundle_id
        self.runner = runner or subprocess.run
        self.timeout = timeout

    def call(self, op: str, **params: Any) -> Dict[str, Any]:
        payload = dict(params, op=op, bundle=self.bundle_id)
        try:
            proc = self.runner(
                ["osascript", "-l", "JavaScript", "-e", JXA_DRIVER, json.dumps(payload)],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError:
            raise UIScriptingError("osascript not found: Live export needs macOS")
        except subprocess.TimeoutExpired:
            raise UIScriptingError(f"UI scripting op '{op}' timed out after {self.timeout:.0f}s")
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            raise UIScriptingError(_explain_osascript_error(stderr) or f"osascript exited {proc.returncode}")
        try:
            result = json.loads((proc.stdout or "").strip())
        except json.JSONDecodeError:
            raise UIScriptingError(f"unreadable reply from UI scripting op '{op}': {proc.stdout!r}")
        if not result.get("ok", False):
            error = str(result.get("error") or f"UI scripting op '{op}' failed")
            explained = _explain_osascript_error(error, result.get("code"))
            if explained != error:
                raise UIScriptingError(explained)
        return result


def _explain_osascript_error(text: str, code: Any = None) -> str:
    codes = {str(code)} if code is not None else set()
    if (codes & {"-1719", "-25211", "-1743"}
            or re.search(r"-1719|-25211|-1743|assistive access|not authori[sz]ed|not allowed", text or "")):
        return f"{_PERMISSION_HINT} (osascript: {text})"
    return text


# ── orchestration ────────────────────────────────────────────────


@dataclass
class _Clock:
    now: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def _busy_windows(windows: Sequence[Dict[str, Any]]) -> List[str]:
    busy = []
    for w in windows:
        if w.get("modal") or w.get("sheets") or w.get("subrole") in ("AXDialog", "AXSystemDialog"):
            busy.append(w.get("name") or "(untitled)")
    return busy


def _window_names(status: Dict[str, Any]) -> List[str]:
    return [w.get("name", "") for w in status.get("windows", [])]


def _describe_windows(still_open: Optional[Sequence[str]]) -> str:
    if still_open:
        return ", ".join(n or "(untitled)" for n in still_open)
    return "windows that could not be read"


def _extra_windows(status: Dict[str, Any], baseline: Sequence[str]) -> List[str]:
    """Windows open now that were not open at baseline, counted by name.

    Counting (not set membership) matters: Live's render progress window can
    share a name, even an empty one, with a window that was already open.
    """
    remaining = Counter(baseline)
    extra = []
    for name in _window_names(status):
        if remaining[name] > 0:
            remaining[name] -= 1
        else:
            extra.append(name)
    return extra


def read_live_audio_output_device(prefs_root: Optional[str] = None) -> Optional[str]:
    """The output device the running Live last logged, or None if unknown.

    Reads the most recently written ``Live */Log.txt`` under ``prefs_root``
    and returns the last "Output Device" Live logged since it was started,
    or "No Device" when that is what it logged or it said audio is disabled.
    None means the log could not be read or says nothing about the device
    for this launch, so the device is unknown.
    """
    root = prefs_root or LIVE_PREFS_ROOT
    try:
        logs = [os.path.join(root, d, "Log.txt") for d in os.listdir(root) if d.startswith("Live ")]
    except OSError:
        return None
    logs = [p for p in logs if os.path.isfile(p)]
    if not logs:
        return None
    try:
        with open(max(logs, key=os.path.getmtime), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    starts = list(_LIVE_STARTED.finditer(text))
    if not starts:
        return None
    device = None
    for event in _DEVICE_EVENT.finditer(text, starts[-1].end()):
        device = NO_DEVICE if event.group("disabled") else event.group("device")
    return device


def _parse_afinfo(text: str) -> Dict[str, Any]:
    info: Dict[str, Any] = {}
    m = re.search(r"File type ID:\s*(\S+)", text)
    if m:
        info["file_type"] = m.group(1)
    m = re.search(r"Data format:\s*(\d+)\s*ch,\s*(\d+)\s*Hz,\s*(.+)", text)
    if m:
        info["channels"] = int(m.group(1))
        info["sample_rate"] = int(m.group(2))
        fmt = m.group(3).strip()
        info["data_format"] = fmt
        depth = re.search(r"(?:Int|Float)(\d+)", fmt) or re.search(r"(\d+)-bit", fmt)
        if depth:
            info["bit_depth"] = int(depth.group(1))
    m = re.search(r"estimated duration:\s*([\d.]+)\s*sec", text)
    if m:
        info["duration_seconds"] = float(m.group(1))
    return info


class LiveExporter:
    """Runs one export end to end. Every external effect is injectable."""

    def __init__(
        self,
        driver: Any = None,
        clock: Optional[_Clock] = None,
        afinfo: Optional[Callable[[str], Optional[str]]] = None,
        platform: Optional[str] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        audio_device: Optional[Callable[[], Optional[str]]] = None,
        armed_reader: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    ):
        self.driver = driver or JXADriver()
        # Asks Live's Remote Script for the armed tracks (get_ui_state shows
        # them); without it the UI state reports them as unknown.
        self.armed_reader = armed_reader
        # The transport as the UI guard found it, set by preflight; run()
        # checks the transport against it when the export is over.
        self.transport_before: Optional[Dict[str, Optional[bool]]] = None
        # Whether the Arrangement had a time selection when preflight looked
        # (True, False, or None when accessibility could not tell).
        self.selection_before: Optional[bool] = None
        self.clock = clock or _Clock()
        self.afinfo = afinfo or _run_afinfo
        self.audio_device = audio_device or read_live_audio_output_device
        self.platform = platform or sys.platform
        self.poll_interval = poll_interval
        self.steps: List[Dict[str, Any]] = []

    # each driver call is recorded so the reply shows what was driven
    def _call(self, op: str, **params: Any) -> Dict[str, Any]:
        result = self.driver.call(op, **params)
        entry = dict(params, op=op)
        for key in ("before", "after", "clicked", "pressed", "where", "replaced", "fields"):
            if key in result:
                entry[key] = result[key]
        if op not in ("status", "texts", "controls", "get_time_field"):
            self.steps.append(entry)
        return result

    def _require(self, result: Dict[str, Any], what: str) -> Dict[str, Any]:
        if not result.get("ok", False):
            raise ExportError(f"{what}: {result.get('error') or 'failed'}")
        return result

    def _status(self) -> Dict[str, Any]:
        status = self._call("status")
        if not status.get("ok", False):
            raise UIScriptingError(
                f"could not read Live's windows: {status.get('error') or 'status failed'}"
            )
        return status

    def _wait_for_window(self, title: str, timeout: float) -> bool:
        deadline = self.clock.monotonic() + timeout
        while True:
            if title in _window_names(self._status()):
                return True
            if self.clock.monotonic() >= deadline:
                return False
            self.clock.sleep(min(0.25, self.poll_interval))

    def _cancel(self, window: str) -> None:
        try:
            self.driver.call("cancel", window=window)
        except Exception:  # best effort while already failing
            pass

    def preflight(self) -> List[str]:
        """Check Live can take an export now, before anything in it is moved.

        Fails when an earlier export left pending is not yet shown finished
        or cancelled, Live is not running, is showing a dialog (an Export
        dialog or Save panel left from an earlier run included), or logs no
        audio output device. Returns warnings for what could not be checked.
        """
        global _pending_baseline
        if _pending_baseline is not None:
            still_open = self._settle(_pending_baseline, 0.0, earlier=True)
            if still_open != []:
                raise ExportError(
                    "an earlier export did not finish cleanly and may still be running in "
                    f"Live (Live still shows: {_describe_windows(still_open)}); let its "
                    "render finish or cancel it in Live, then export again"
                )
            _pending_baseline = None
        status = self._status()
        if not status.get("running"):
            raise ExportError("Ableton Live is not running")
        windows = status.get("windows", [])
        busy = _busy_windows(windows)
        busy += [n for n in (EXPORT_DIALOG_TITLE, SAVE_PANEL_TITLE)
                 if n in _window_names(status) and n not in busy]
        if busy:
            raise ExportError(
                "Live is showing a dialog or modal window ("
                + ", ".join(busy)
                + "); close it before exporting"
            )
        device = self.audio_device()
        if device is None:
            return self._guard_ui() + [
                "could not read Live's audio output device from its Log.txt; "
                "the audio interface was not checked"
            ]
        if device.strip().lower() == NO_DEVICE.lower():
            raise ExportError(
                "Live has no audio output device (its Log.txt says audio is disabled or "
                "the output device is 'No Device'), so the audio interface is missing or "
                "unplugged; "
                "reconnect it, or choose an output device in Live's Settings > Audio, "
                "then export again"
            )
        return self._guard_ui()

    def _guard_ui(self) -> List[str]:
        """Refuse unless Live's UI is safe to click in; remember the transport.

        Key Map or MIDI Map mode, an open dialog and running playback all
        change what a click or key does, so nothing is sent to Live then.
        """
        try:
            state = live_ui_state.require_safe_to_drive(self.driver, self.armed_reader)
        except live_ui_state.UnsafeUIState as e:
            raise ExportError(
                "Live's UI is not safe to drive: " + "; ".join(e.reasons) + ". Nothing was clicked"
            ) from e
        self.transport_before = live_ui_state.transport_snapshot(state)
        self.selection_before = (state.get("time_selection") or {}).get("present")
        armed = state.get("armed_tracks") or []
        if armed:
            names = ", ".join(str(t.get("name")) for t in armed)
            return [f"tracks are armed in Live: {names}"]
        return []

    def finalize(self, result: Dict[str, Any]) -> None:
        """Leave Live as the export found it: same transport, no selection made.

        Nothing is clicked unless something needs undoing: the transport and
        the time selection are read first (the export itself makes no
        selection). A selection that is there, whoever made it, is cleared
        (``live_ui_state.finish_ui_tool``), and playback a click started is
        stopped. The outcome goes in
        the result (selection_cleared, transport_unchanged, and
        time_selection_before / time_selection_after) and a failure to clean
        up is a warning: the render itself is judged by its file.

        While the export is pending nothing is touched at all.
        """
        if self.transport_before is None:
            return
        if result.get("export_pending"):
            result["selection_cleared"] = False
            result["transport_unchanged"] = None
            result.setdefault("warnings", []).append(
                "Live was not left clean: Live's UI is not safe to touch while the export is pending"
            )
            return
        result["time_selection_before"] = self.selection_before
        try:
            after = live_ui_state.read_ui_state(self.driver, self.armed_reader)
            selection = (after.get("time_selection") or {}).get("present")
            changes = live_ui_state.compare_transport(
                self.transport_before, live_ui_state.transport_snapshot(after)
            )
        except Exception as e:
            result["selection_cleared"] = False
            result["transport_unchanged"] = None
            result.setdefault("warnings", []).append(
                f"Live was not left clean: could not read Live's UI after the export: {e}"
            )
            return
        if selection is False and not changes:
            result["time_selection_after"] = False
            result["selection_cleared"] = True
            result["transport_unchanged"] = True
            return
        outcome = live_ui_state.finish_ui_tool(self.driver, self.transport_before, self.armed_reader)
        result["selection_cleared"] = outcome.get("selection_cleared")
        result["time_selection_after"] = False if outcome.get("selection_cleared") else None
        result["transport_unchanged"] = outcome.get("transport_unchanged")
        if outcome.get("stopped_playback"):
            result.setdefault("warnings", []).append(
                "playback started during the export and was stopped"
            )
        if outcome.get("error"):
            result.setdefault("warnings", []).append(
                "Live was not left clean: " + str(outcome["error"])
            )

    def _settle(
        self, baseline: Sequence[str], timeout: float, earlier: bool = False
    ) -> Optional[List[str]]:
        """Cancel the export windows and wait until Live shows none it opened.

        With earlier True (an export an earlier call left pending) nothing is
        cancelled, and only windows an export leaves behind count: the Export
        dialog, the Save panel and untitled (render progress) windows beyond
        the baseline's, so a renamed Set or a plugin window does not. It also
        does not wait: the first poll that is not clear is returned, so it
        takes at most STABLE_POLLS polls and the timeout does not apply.

        Returns [] once STABLE_POLLS polls in a row show no window that was
        not open at baseline (no Export dialog, Save panel or render window).
        Otherwise returns what is still open (or was last seen open when the
        timeout ends before Live has shown clear long enough), or None when
        that cannot be told.
        """
        if not earlier:
            self._cancel(SAVE_PANEL_TITLE)
            self._cancel(EXPORT_DIALOG_TITLE)
        deadline = self.clock.monotonic() + timeout
        clear = 0
        last_open: Optional[List[str]] = None
        while True:
            try:
                status = self._status()
            except ExportError:
                extra: Optional[List[str]] = None
            else:
                if not status.get("running"):
                    return []
                extra = _extra_windows(status, baseline)
                if earlier:
                    extra = [n for n in extra if n in (EXPORT_DIALOG_TITLE, SAVE_PANEL_TITLE, "")]
            if earlier and extra != []:
                return extra
            clear = clear + 1 if extra == [] else 0
            if clear >= STABLE_POLLS:
                return []
            if extra:
                last_open = extra
            if not earlier and self.clock.monotonic() >= deadline:
                return last_open if extra == [] else extra
            for name in (SAVE_PANEL_TITLE, EXPORT_DIALOG_TITLE):
                if not earlier and extra and name in extra:
                    self._cancel(name)
            self.clock.sleep(min(0.25, self.poll_interval))

    def _texts(self, name: str) -> List[str]:
        """Static text of every window with this name ('' for the untitled ones)."""
        return self._call("texts", window=name).get("texts", [])

    def _new_window_texts(
        self, extra: Sequence[str], untitled_shown: Sequence[str]
    ) -> Dict[str, List[str]]:
        """What the windows opened since baseline say, by window name.

        Untitled windows cannot be told apart, so all of them are read and the
        text the ones open at baseline already showed is left out.
        """
        texts: Dict[str, List[str]] = {}
        for name in dict.fromkeys(extra):
            try:
                found = self._texts(name)
            except ExportError:
                continue
            if not name:
                found = [t for t in found if t not in untitled_shown]
            texts[name or "(untitled)"] = found
        return texts

    def run(self, request: ExportRequest) -> Dict[str, Any]:
        self.steps = []
        self.transport_before = None
        result: Dict[str, Any] = {
            "ok": False,
            "status": "failed",
            "output_path": None,
            "output_files": [],
            "warnings": [],
            # True when the export failed and Live may still be in it (an
            # export window or render could not be shown closed): nothing in
            # the Set may be changed until it is.
            "export_pending": False,
        }
        try:
            request.validate()
            result.update({
                "format": request.format,
                "sample_rate": request.sample_rate,
                "bit_depth": None if request.format == "mp3" else request.bit_depth,
                "normalize": bool(request.normalize),
                "create_video": bool(request.create_video),
                "expected_path": request.output_file,
                "expected_seconds": request.expected_seconds,
                "rendered_seconds": None,
                "length_verified": False,
                "rendered_track": _rendered_track_label(request),
                "files": [],
            })
            self._export(request, result)
        except (ExportError, ValueError) as e:
            result["error"] = str(e)
        except Exception as e:  # never let an unexpected error read as success
            result["error"] = f"unexpected export error: {e}"
        self.finalize(result)
        result["steps"] = self.steps
        return result

    def _export(self, request: ExportRequest, result: Dict[str, Any]) -> None:
        global _pending_baseline
        check_preconditions(request, self.platform)
        result["warnings"] += self.preflight()

        activated = self._require(self._call("activate"), "could not bring Live to the front")
        if not activated.get("frontmost"):
            raise ExportError("Ableton Live did not come to the front; export needs Live frontmost")

        baseline = _window_names(self._status())
        untitled_shown = self._texts("") if "" in baseline else []
        try:
            # From here on Live may be in the export, so any failure first
            # cancels it and checks Live came out of it (export_pending).
            opened = self._call("menu", path=[FILE_MENU, EXPORT_MENU_ITEM])
            if not opened.get("ok") and opened.get("missing"):
                # Menu item renamed or localized: fall back to the shortcut.
                self._call("keystroke", **EXPORT_SHORTCUT)
            elif not opened.get("ok"):
                raise ExportError(f"could not open the Export dialog: {opened.get('error')}")
            files, start_wall = self._drive_export(request, baseline, untitled_shown, result)
        except Exception as e:
            try:
                still_open = self._settle(baseline, request.dialog_timeout)
            except Exception:
                still_open = None
            if still_open == []:
                raise
            result["export_pending"] = True
            _pending_baseline = list(baseline)
            reason = str(e) if isinstance(e, ExportError) else f"unexpected export error: {e}"
            raise ExportError(
                f"{reason}. The export may still be pending: Live's export windows could "
                f"not be confirmed closed (Live still shows: {_describe_windows(still_open)}). Check Live for an "
                f"open {EXPORT_DIALOG_TITLE} dialog, Save panel or render progress window, "
                "and cancel it or let the render finish before changing the Set or "
                "exporting again"
            ) from e
        self._check_output(request, files, start_wall, result)

    def _drive_export(
        self,
        request: ExportRequest,
        baseline: Sequence[str],
        untitled_shown: Sequence[str],
        result: Dict[str, Any],
    ) -> Tuple[List[str], float]:
        """Drive the Export dialog and Save panel, then wait out the render.

        Returns the rendered files and when the render started. Returns only
        once Live shows no window the export opened and the files have
        stopped growing.
        """
        if not self._wait_for_window(EXPORT_DIALOG_TITLE, request.dialog_timeout):
            raise ExportError(
                f"the '{EXPORT_DIALOG_TITLE}' dialog did not appear within "
                f"{request.dialog_timeout:.0f}s"
            )
        for step in build_dialog_plan(request):
            self._apply(step)
        if request.typed_range:
            self._type_range(request, result)
        else:
            self._report_prefilled_range(request, result)
        settings = self._call("controls", window=EXPORT_DIALOG_TITLE).get("controls", [])
        result["dialog_settings"] = {
            c["label"]: c["value"] for c in settings
            if c.get("label") and c.get("role") in ("AXButton", "AXPopUpButton") and c.get("value")
        }
        self._require(
            self._call("press", window=EXPORT_DIALOG_TITLE, label=EXPORT_BUTTON),
            "could not press Export",
        )
        if not self._wait_for_window(SAVE_PANEL_TITLE, request.dialog_timeout):
            texts = self._new_window_texts(
                _extra_windows(self._status(), baseline), untitled_shown
            )
            if texts:
                result["dialog_text"] = texts
            raise ExportError(
                f"the Save panel did not appear within {request.dialog_timeout:.0f}s after Export"
            )
        start_wall = self.clock.now()
        self._require(
            self._call(
                "save_panel",
                window=SAVE_PANEL_TITLE,
                directory=request.directory,
                filename=request.stem,
                folder_name=os.path.basename(request.directory.rstrip(os.sep)),
                replace=bool(request.overwrite),
            ),
            "could not fill in the Save panel",
        )
        files = self._wait_for_render(request, baseline, untitled_shown, start_wall, result)
        return files, start_wall

    def _read_field(self, label: str) -> Tuple[int, int, int]:
        """The bar, beat and sixteenth a Render Start / Render Length field shows."""
        res = self._require(
            self._call("get_time_field", window=EXPORT_DIALOG_TITLE, label=label),
            f"could not read '{label}'",
        )
        fields = res.get("fields")
        if not isinstance(fields, (list, tuple)) or len(fields) != 3:
            raise ExportError(f"could not read '{label}': Live's dialog gave {fields!r}")
        try:
            return tuple(int(v) for v in fields)  # type: ignore[return-value]
        except (TypeError, ValueError):
            raise ExportError(f"could not read '{label}': Live's dialog gave {fields!r}")

    def _dialog_range(self, request: ExportRequest, start: Sequence[int], length: Sequence[int]) -> Dict[str, Any]:
        return {
            "start": format_fields(start),
            "length": format_fields(length),
            "start_beats": fields_to_beats(start, request.signature, True),
            "length_beats": fields_to_beats(length, request.signature, False),
        }

    def _type_range(self, request: ExportRequest, result: Dict[str, Any]) -> None:
        """Type Render Start and Render Length, then read both back.

        Both are read only after both were typed, because Live may adjust
        one when the other changes. A field that does not read what was typed
        fails the export here, before Export is pressed.
        """
        wanted = {
            RENDER_START: beats_to_fields(request.start_beats, request.signature, True),
            RENDER_LENGTH: beats_to_fields(request.length_beats, request.signature, False),
        }
        for label, fields in wanted.items():
            self._require(
                self._call("set_time_field", window=EXPORT_DIALOG_TITLE, label=label,
                           fields=list(fields)),
                f"could not type '{label}'",
            )
        got = {label: self._read_field(label) for label in wanted}
        for label, fields in wanted.items():
            if got[label] != fields:
                raise ExportError(
                    f"'{label}' reads {format_fields(got[label])} after typing "
                    f"{format_fields(fields)}; the range in the dialog is not the one asked for"
                )
        result["dialog_range"] = self._dialog_range(request, got[RENDER_START], got[RENDER_LENGTH])

    def _report_prefilled_range(self, request: ExportRequest, result: Dict[str, Any]) -> None:
        """With no range asked for, report what the dialog pre-filled (best effort)."""
        try:
            start, length = self._read_field(RENDER_START), self._read_field(RENDER_LENGTH)
        except ExportError as e:
            result["warnings"].append(f"the dialog's own render range was not read: {e}")
            return
        result["dialog_range"] = self._dialog_range(request, start, length)

    def _check_output(
        self, request: ExportRequest, files: Sequence[str], start_wall: float, result: Dict[str, Any]
    ) -> None:
        """Check the rendered files (and video); Live has finished with them by now."""
        files = list(files)
        result["output_path"] = files[0]
        result["output_files"] = list(files)
        entries = [self._verify_audio(request, path, result, qualify=len(files) > 1)
                   for path in files]
        result["files"] = [e["entry"] for e in entries]
        first = entries[0]["entry"]
        result["file_info"] = entries[0]["info"]
        result["rendered_seconds"] = first.get("duration_seconds")
        result["length_verified"] = all(e["entry"].get("length_verified") for e in entries)
        problems = [e["problem"] for e in entries if e["problem"]]
        if problems:
            raise ExportError("; ".join(problems))
        if request.track_mode == RENDERED_TRACK_ALL and request.expected_files is not None \
                and len(files) != request.expected_files:
            raise ExportError(
                f"All Individual Tracks wrote {len(files)} file(s) in {request.directory} "
                f"but {request.expected_files} were expected: " + ", ".join(
                    os.path.basename(f) for f in files)
            )
        if request.create_video:
            videos = self._find_videos(request, start_wall)
            if not videos:
                raise ExportError(
                    f"audio was rendered to {files[0]} but no new video file appeared in {request.directory}"
                )
            result["video_files"] = videos
            result["output_files"] += videos
        result["ok"] = True
        result["status"] = "success"

    def _apply(self, step: Dict[str, Any]) -> None:
        op = step["op"]
        label = step["label"]
        params = {"window": EXPORT_DIALOG_TITLE, "label": label}
        if op == "set_toggle":
            res = self._call("set_toggle", on=step["on"], **params)
            if not res.get("ok"):
                if (step.get("allow_disabled_off") and not step["on"]
                        and res.get("enabled") is False and res.get("before") is not True):
                    return
                if label == CREATE_VIDEO and res.get("enabled") is False:
                    raise ExportError(
                        "Create Video is disabled: Live only enables it when the "
                        "Arrangement contains video clips"
                    )
                raise ExportError(f"could not set '{label}': {res.get('error')}")
            if res.get("after") is not step["on"]:
                raise ExportError(
                    f"'{label}' is {res.get('after')} after toggling, expected {step['on']}"
                )
        else:
            if step.get("first"):
                params["first"] = True
            res = self._call("set_choice", values=step["values"], **params)
            if not res.get("ok"):
                raise ExportError(f"could not set '{label}': {res.get('error')}")
            after = str(res.get("after", ""))
            wanted = [res.get("picked")] if step.get("first") else step["values"]
            if not any(_same(after, v) for v in wanted):
                seen = res.get("seen") or []
                hint = f" (choices seen: {', '.join(seen)})" if seen else ""
                raise ExportError(
                    f"could not set '{label}' to {wanted[0]}: it reads '{after}'{hint}"
                )

    def _output_files(self, request: ExportRequest, start_wall: float) -> List[str]:
        """The files this export has written so far (new since the Save panel)."""
        if request.track_mode != RENDERED_TRACK_ALL:
            for candidate in request.expected_paths:
                try:
                    st = os.stat(candidate)
                except OSError:
                    continue
                if st.st_mtime >= start_wall - 1:
                    return [candidate]
            return []
        # All Individual Tracks writes one file per track beside the name
        # given in the Save panel: every new audio file whose name starts with it.
        wanted = FORMAT_EXTENSIONS[request.format]
        found = []
        for name in sorted(os.listdir(request.directory)):
            stem, ext = os.path.splitext(name)
            if (stem != request.stem and not stem.startswith(request.stem + " ")) or ext.lower() not in wanted:
                continue
            full = os.path.join(request.directory, name)
            try:
                if os.stat(full).st_mtime >= start_wall - 1:
                    found.append(full)
            except OSError:
                continue
        return found

    def _wait_for_render(
        self,
        request: ExportRequest,
        baseline: Sequence[str],
        untitled_shown: Sequence[str],
        start_wall: float,
        result: Dict[str, Any],
    ) -> List[str]:
        """Wait until the render is over: no export window open and no file growing.

        Returns only when Live shows no window beyond the baseline (the render
        progress window has closed) and the set of output files and every
        file's size have stayed the same for STABLE_POLLS - 1 polls in a row.
        Nothing is touched while waiting.
        """
        deadline = self.clock.monotonic() + request.render_timeout
        last_signature: Optional[Tuple[Tuple[str, int], ...]] = None
        stable = 0
        found: List[str] = []
        while True:
            status = self._status()
            if not status.get("running"):
                raise ExportError("Ableton Live quit during the export")
            extra = _extra_windows(status, baseline)
            alerts = [n for n in extra if n not in (EXPORT_DIALOG_TITLE, SAVE_PANEL_TITLE)]
            for name, texts in self._new_window_texts(alerts, untitled_shown).items():
                if any(_ERROR_WORDS.search(t) for t in texts):
                    result["dialog_text"] = {name: texts}
                    raise ExportError("Live reported an error during export: " + " | ".join(texts))

            found = self._output_files(request, start_wall)
            signature = tuple((f, _size(f)) for f in found)
            if found:
                grown = signature != last_signature or any(size == 0 for _, size in signature)
                stable = 0 if grown else stable + 1
                last_signature = signature
                if not extra and stable >= STABLE_POLLS - 1:
                    return found
            if self.clock.monotonic() >= deadline:
                texts = self._new_window_texts(extra, untitled_shown)
                if texts:
                    result["dialog_text"] = texts
                if found:
                    sizes = ", ".join(f"{os.path.basename(f)} is {n} bytes" for f, n in signature)
                    raise ExportError(
                        f"render did not finish within {request.render_timeout:.0f}s "
                        f"({sizes} and Live still shows: {', '.join(extra) or 'nothing'})"
                    )
                raise ExportError(
                    f"no output file was produced within {request.render_timeout:.0f}s "
                    f"(expected {request.output_file})"
                )
            self.clock.sleep(self.poll_interval)

    def _verify_audio(
        self, request: ExportRequest, path: str, result: Dict[str, Any], qualify: bool = False
    ) -> Dict[str, Any]:
        """Check one rendered file; returns {entry, info, problem}.

        ``entry`` is the file's row in the reply (path, size, duration, sample
        rate, ...). ``problem`` is a sentence when the file does not match the
        request, or None. Warnings about what afinfo did not report go to
        ``result``, naming the file when there are several (``qualify``).
        """
        prefix = f"{os.path.basename(path)}: " if qualify else ""
        size = os.path.getsize(path)
        entry: Dict[str, Any] = {"path": path, "size_bytes": size, "duration_seconds": None,
                                 "sample_rate": None, "length_verified": False}
        if size == 0:
            raise ExportError(f"output file is empty: {path}")
        text = self.afinfo(path)
        if not text:
            result["warnings"].append(
                f"{prefix}afinfo unavailable; the output file was not verified")
            return {"entry": entry, "info": None, "problem": None}
        info = _parse_afinfo(text)
        if not info:
            result["warnings"].append(
                f"{prefix}could not read afinfo output; the output file was not verified")
            return {"entry": entry, "info": info, "problem": None}
        problems = []
        unverified = []
        rate = info.get("sample_rate")
        entry["sample_rate"] = rate
        entry["bit_depth"] = info.get("bit_depth")
        entry["channels"] = info.get("channels")
        if rate is None:
            unverified.append("sample rate")
        elif rate != request.sample_rate:
            problems.append(f"sample rate is {rate} Hz, expected {request.sample_rate}")
        if request.format != "mp3":
            depth = info.get("bit_depth")
            if depth is None:
                unverified.append("bit depth")
            elif depth != request.bit_depth:
                problems.append(f"bit depth is {depth}, expected {request.bit_depth}")
        duration = info.get("duration_seconds")
        entry["duration_seconds"] = duration
        if request.expected_seconds is not None:
            if duration is None:
                unverified.append("length")
            elif abs(duration - request.expected_seconds) > LENGTH_TOLERANCE_SECONDS:
                problems.append(
                    f"it is {duration:.2f} s long, expected {request.expected_seconds:.2f} s "
                    "for the requested range (Live rendered a different range, or the "
                    "tempo changes inside it)"
                )
            else:
                entry["length_verified"] = True
        if unverified:
            result["warnings"].append(
                f"{prefix}afinfo did not report the " + ", ".join(unverified) + "; not verified"
            )
        problem = f"{path} was written but does not match the request: " + "; ".join(problems) \
            if problems else None
        return {"entry": entry, "info": info, "problem": problem}

    def _find_videos(self, request: ExportRequest, start_wall: float) -> List[str]:
        videos = []
        for name in sorted(os.listdir(request.directory)):
            if os.path.splitext(name)[1].lower() not in VIDEO_EXTENSIONS:
                continue
            full = os.path.join(request.directory, name)
            if os.path.getmtime(full) >= start_wall - 1 and os.path.getsize(full) > 0:
                videos.append(full)
        return videos


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _rendered_track_label(request: ExportRequest) -> str:
    if request.track_mode == RENDERED_TRACK_ALL:
        return RENDERED_TRACK_ALL
    if request.track_mode == "track":
        return request.track_name
    return "Main"


def _same(a: Any, b: Any) -> bool:
    def n(s: Any) -> str:
        return re.sub(r"[\s,]+", "", str(s)).lower()
    return n(a) == n(b)


def _run_afinfo(path: str) -> Optional[str]:
    try:
        proc = subprocess.run(["afinfo", path], capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def export_from_live(request: ExportRequest, exporter: Optional[LiveExporter] = None) -> Dict[str, Any]:
    """Run one export and return the JSON-ready result dict."""
    return (exporter or LiveExporter()).run(request)
