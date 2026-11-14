"""Export audio (and video) from Ableton Live by driving its UI on macOS.

Live's File > Export Audio/Video is not part of the Live Object Model, so the
Remote Script cannot trigger it. This module drives the dialog from the host
side instead, through macOS UI scripting (System Events, via JavaScript for
Automation run by ``osascript``). It is the only UI-automation path in the
server and is deliberately self-contained: the LOM tools never import it.

Flow:

1. Check Live is running and not already showing a dialog, then bring it
   to the front.
2. Optionally give the Arrangement keyboard focus (Navigate > Arrangement
   View) and run Edit > Select Loop, so the dialog pre-fills Render Start
   and Render Length from the loop brace.
3. Open File > Export Audio/Video (falling back to Cmd+Shift+R).
4. Set the dialog's controls (Rendered Track, Encode PCM / MP3, File Type,
   Bit Depth, Sample Rate, Normalize, Create Video), re-reading each one and
   failing if it did not take.
5. Press Export, then fill in the macOS Save panel (folder and file name).
6. Poll until the render window is gone and the output file has stopped
   growing, then check the file with ``afinfo``: sample rate, bit depth and,
   when the caller says how long the range should be, length.

A run that cannot prove the output file was written is a failure. Nothing
here reports success from the UI alone. Render Start and Render Length are
never read back from the dialog, so the file's length is the only evidence
of which range Live rendered.

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
from typing import Any, Callable, Dict, List, Optional, Sequence

LIVE_BUNDLE_ID = "com.ableton.live"

EXPORT_DIALOG_TITLE = "Export Audio/Video"
SAVE_PANEL_TITLE = "Save"
FILE_MENU = "File"
EXPORT_MENU_ITEM = "Export Audio/Video"
EDIT_MENU = "Edit"
SELECT_LOOP_MENU_ITEM = "Select Loop"
# Live's "Move Focus to the Arrangement View" command (Option+2).
NAVIGATE_MENU = "Navigate"
ARRANGEMENT_VIEW_MENU_ITEM = "Arrangement View"
# Live's documented shortcut for File > Export Audio/Video (manual 5.1.3).
EXPORT_SHORTCUT = {"key": "r", "modifiers": ["command down", "shift down"]}

# Accessibility descriptions of the Export dialog controls (Live 12).
RENDERED_TRACK = "Rendered Track Chooser"
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

DEFAULT_DIALOG_TIMEOUT = 15.0
DEFAULT_RENDER_TIMEOUT = 900.0
DEFAULT_POLL_INTERVAL = 1.0
STABLE_POLLS = 3
# Slack between the rendered and the expected length: covers MP3 encoder
# padding and the dialog's sixteenth-note resolution at any normal tempo.
LENGTH_TOLERANCE_SECONDS = 0.5


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
    select_loop: bool = False
    create_video: bool = False
    overwrite: bool = False
    render_timeout: float = DEFAULT_RENDER_TIMEOUT
    dialog_timeout: float = DEFAULT_DIALOG_TIMEOUT
    # How long the rendered file should be, when the caller knows the range.
    expected_seconds: Optional[float] = None

    # Filled in by validate().
    directory: str = field(default="", init=False)
    stem: str = field(default="", init=False)

    def validate(self) -> "ExportRequest":
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
        return [os.path.join(self.directory, self.stem + ext) for ext in FORMAT_EXTENSIONS[self.format]]

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


def build_dialog_plan(request: ExportRequest) -> List[Dict[str, Any]]:
    """Map export parameters to the Export dialog controls to set, in order.

    Order matters: File Type and Bit Depth are only live while Encode PCM is on.
    """
    plan: List[Dict[str, Any]] = [
        # Live's own bus leads this list and the tracks after it can have its
        # name, so it is taken by position: the first entry, whatever is set.
        {"op": "set_choice", "label": RENDERED_TRACK, "values": list(MAIN_TRACK_VALUES),
         "first": True},
    ]
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
JXA_DRIVER = r"""
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
    .replace(/…/g, '...').replace(/^[\u2714\u2713]/, '').replace(/[\s,]+/g, '');
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
function pickFromMenu(p, el, values, firstOnly) {
  press(el);
  delay(0.6);
  var items = menuItemsOf(p);
  var seen = [];
  for (var i = 0; i < items.length; i++) {
    var name = String(safe(function () { return items[i].name(); }, ''));
    if (!name) continue;
    seen.push(name);
    if (matches(name, values)) {
      press(items[i]);
      delay(0.4);
      return {seen: seen, picked: name.replace(/^[\u2714\u2713]\s*/, '')};
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
      var nameField = findControl(w, 'Save As:', ['AXTextField']);
      nameField.value = cmd.filename;
      // Go to the folder: Cmd+Shift+G opens a path sheet on the panel.
      SE.keystroke('g', {using: ['command down', 'shift down']});
      var sheet = null;
      for (var t = 0; t < 30 && !sheet; t++) {
        delay(0.1);
        sheet = safe(function () { return w.sheets.length ? w.sheets[0] : null; }, null);
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
      if (!typed) SE.keystroke(cmd.directory);
      delay(0.3);
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
      var where = safe(function () { return findControl(w, 'Where:', ['AXPopUpButton']).value(); }, '');
      if (cmd.folder_name && where && norm(where) !== norm(cmd.folder_name))
        return {ok: false, where: where,
                error: "the Save panel is in folder '" + where + "', expected '" + cmd.folder_name + "'"};
      var name = String(safe(function () { return nameField.value(); }, ''));
      if (name !== cmd.filename)
        return {ok: false, error: "the Save panel file name is '" + name + "', expected '" + cmd.filename + "'"};
      press(findControl(w, 'Save', ['AXButton']));
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
  throw new Error('unknown op ' + cmd.op);
}
"""


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
    ):
        self.driver = driver or JXADriver()
        self.clock = clock or _Clock()
        self.afinfo = afinfo or _run_afinfo
        self.platform = platform or sys.platform
        self.poll_interval = poll_interval
        self.steps: List[Dict[str, Any]] = []

    # each driver call is recorded so the reply shows what was driven
    def _call(self, op: str, **params: Any) -> Dict[str, Any]:
        result = self.driver.call(op, **params)
        entry = dict(params, op=op)
        for key in ("before", "after", "clicked", "pressed", "where", "replaced"):
            if key in result:
                entry[key] = result[key]
        if op not in ("status", "texts", "controls"):
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
            self.driver.call("press", window=window, label=CANCEL_BUTTON)
        except Exception:  # best effort while already failing
            pass

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
        result: Dict[str, Any] = {
            "ok": False,
            "status": "failed",
            "output_path": None,
            "output_files": [],
            "warnings": [],
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
            })
            self._export(request, result)
        except (ExportError, ValueError) as e:
            result["error"] = str(e)
        except Exception as e:  # never let an unexpected error read as success
            result["error"] = f"unexpected export error: {e}"
        result["steps"] = self.steps
        return result

    def _export(self, request: ExportRequest, result: Dict[str, Any]) -> None:
        check_preconditions(request, self.platform)

        status = self._status()
        if not status.get("running"):
            raise ExportError("Ableton Live is not running")
        busy = _busy_windows(status.get("windows", []))
        if busy:
            raise ExportError(
                "Live is showing a dialog or modal window ("
                + ", ".join(busy)
                + "); close it before exporting"
            )

        activated = self._require(self._call("activate"), "could not bring Live to the front")
        if not activated.get("frontmost"):
            raise ExportError("Ableton Live did not come to the front; export needs Live frontmost")

        if request.select_loop:
            # Select Loop acts on the view that has keyboard focus.
            self._require(
                self._call("menu", path=[NAVIGATE_MENU, ARRANGEMENT_VIEW_MENU_ITEM]),
                "could not give the Arrangement keyboard focus",
            )
            self._require(
                self._call("menu", path=[EDIT_MENU, SELECT_LOOP_MENU_ITEM]),
                "could not select the loop for the render range",
            )

        baseline = _window_names(self._status())
        untitled_shown = self._texts("") if "" in baseline else []
        opened = self._call("menu", path=[FILE_MENU, EXPORT_MENU_ITEM])
        if not opened.get("ok") and opened.get("missing"):
            # Menu item renamed or localized: fall back to the shortcut.
            self._call("keystroke", **EXPORT_SHORTCUT)
        elif not opened.get("ok"):
            raise ExportError(f"could not open the Export dialog: {opened.get('error')}")
        try:
            if not self._wait_for_window(EXPORT_DIALOG_TITLE, request.dialog_timeout):
                raise ExportError(
                    f"the '{EXPORT_DIALOG_TITLE}' dialog did not appear within "
                    f"{request.dialog_timeout:.0f}s"
                )
            for step in build_dialog_plan(request):
                self._apply(step)
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
        except Exception:
            self._cancel(SAVE_PANEL_TITLE)
            self._cancel(EXPORT_DIALOG_TITLE)
            raise

        path = self._wait_for_render(request, baseline, untitled_shown, start_wall, result)
        result["output_path"] = path
        result["output_files"] = [path]
        self._verify_audio(request, path, result)
        if request.create_video:
            videos = self._find_videos(request, start_wall)
            if not videos:
                raise ExportError(
                    f"audio was rendered to {path} but no new video file appeared in {request.directory}"
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

    def _wait_for_render(
        self,
        request: ExportRequest,
        baseline: Sequence[str],
        untitled_shown: Sequence[str],
        start_wall: float,
        result: Dict[str, Any],
    ) -> str:
        deadline = self.clock.monotonic() + request.render_timeout
        last_size: Optional[int] = None
        stable = 0
        found: Optional[str] = None
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

            found = None
            for candidate in request.expected_paths:
                try:
                    st = os.stat(candidate)
                except OSError:
                    continue
                if st.st_mtime >= start_wall - 1:
                    found = candidate
                    break
            if found:
                size = os.path.getsize(found)
                stable = stable + 1 if size and size == last_size else 0
                last_size = size
                if not extra and stable >= STABLE_POLLS - 1:
                    return found
            if self.clock.monotonic() >= deadline:
                texts = self._new_window_texts(extra, untitled_shown)
                if texts:
                    result["dialog_text"] = texts
                if found:
                    raise ExportError(
                        f"render did not finish within {request.render_timeout:.0f}s "
                        f"({found} is {last_size} bytes and Live still shows: {', '.join(extra) or 'nothing'})"
                    )
                raise ExportError(
                    f"no output file was produced within {request.render_timeout:.0f}s "
                    f"(expected {request.output_file})"
                )
            self.clock.sleep(self.poll_interval)

    def _verify_audio(self, request: ExportRequest, path: str, result: Dict[str, Any]) -> None:
        if os.path.getsize(path) == 0:
            raise ExportError(f"output file is empty: {path}")
        text = self.afinfo(path)
        if not text:
            result["warnings"].append("afinfo unavailable; the output file was not verified")
            return
        info = _parse_afinfo(text)
        result["file_info"] = info
        if not info:
            result["warnings"].append("could not read afinfo output; the output file was not verified")
            return
        problems = []
        unverified = []
        rate = info.get("sample_rate")
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
        result["rendered_seconds"] = duration
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
                result["length_verified"] = True
        if unverified:
            result["warnings"].append(
                "afinfo did not report the " + ", ".join(unverified) + "; not verified"
            )
        if problems:
            raise ExportError(f"{path} was written but does not match the request: " + "; ".join(problems))

    def _find_videos(self, request: ExportRequest, start_wall: float) -> List[str]:
        videos = []
        for name in sorted(os.listdir(request.directory)):
            if os.path.splitext(name)[1].lower() not in VIDEO_EXTENSIONS:
                continue
            full = os.path.join(request.directory, name)
            if os.path.getmtime(full) >= start_wall - 1 and os.path.getsize(full) > 0:
                videos.append(full)
        return videos


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
