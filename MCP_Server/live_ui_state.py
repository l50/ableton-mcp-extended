"""What Live's UI is doing right now, and whether it is safe to drive it.

Key Map and MIDI Map mode, dialogs, which view has keyboard focus and the
time selection are not in the Live Object Model, so they are read through
macOS accessibility (System Events, via the JXA driver in live_export.py).
This module is the small interface every UI-driving tool builds on:

- ``read_ui_state`` returns the state as one dict (``get_ui_state`` tool).
- ``require_safe_to_drive`` is the guard a UI tool calls before it clicks or
  types; it raises ``UnsafeUIState`` with the reasons when a mode or dialog
  would change what the clicks do.
- ``clear_selection`` removes the Arrangement time selection with a menu
  command (no click in a lane, no key) and checks the transport afterwards.
- ``transport_snapshot`` and ``compare_transport`` let a tool prove it left
  the transport as it found it.

Nothing here imports the ``mcp`` package, so it also runs on a Mac that only
has the standard library. Where accessibility cannot tell a state apart the
value is ``None`` and ``unreadable`` says why; nothing is guessed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

KEY_MAP_SWITCH = "Key Map Mode"
MIDI_MAP_SWITCH = "MIDI Map Mode"
COMPUTER_KEYBOARD_SWITCH = "Computer MIDI Keyboard"
SWITCH_PLAY = "Play"
SWITCH_ARRANGEMENT_RECORD = "Arrangement Record"
SWITCH_SESSION_RECORD = "Session Record"
SWITCH_OVERDUB = "MIDI Arrangement Overdub"
SWITCH_LOOP = "Arrangement Loop"
SWITCH_PUNCH_IN = "Punch-In"
SWITCH_PUNCH_OUT = "Punch-Out"

NAVIGATE_MENU = "Navigate"
ARRANGEMENT_VIEW_MENU_ITEM = "Arrangement View"
PLAYBACK_MENU = "Playback"
MOVE_MARKER_MENU_ITEM = "Move Insert Marker To Playhead"

# Edit menu commands that are enabled only while the Arrangement has a time
# selection and has keyboard focus.
TIME_COMMANDS = ("Cut Time", "Copy Time", "Duplicate Time", "Delete Time")
# Enabled while something Delete would remove (clips, devices) is selected.
DELETE_COMMAND = "Delete"

# Windows that are dialogs whatever else they report.
DIALOG_SUBROLES = ("AXDialog", "AXSystemDialog", "AXSheet")
KNOWN_DIALOG_NAMES = ("Export Audio/Video", "Save")

# The top-level groups of the set window, named by their accessibility title,
# and the view each one belongs to. Live 12.4 names the group of the view that
# is showing "Arrangement" or "Session", and the clip or device panel "Clip
# Detail" or "Device Detail".
FOCUS_GROUPS = {
    "Arrangement": "arrangement",
    "Session": "session",
    "Mixer": "mixer",
    "Control": "control",
    "Browser": "browser",
    "Learn View": "learn",
    "Status": "status",
}
DETAIL_MARK = "Detail"
CRASH_REPORT_GROUP = "Report a Crash"

TRANSPORT_KEYS = (
    "playing",
    "arrangement_record",
    "session_record",
    "overdub",
    "loop",
    "punch_in",
    "punch_out",
)

# JavaScript for Automation appended to the export driver (live_export.py
# builds its script from this). ``dispatchUi`` handles the ops below; the
# helpers ``safe``, ``windowInfo``, ``HEAVY_ROLES`` and ``press`` come from it.
UI_STATE_JXA = r"""
function uiKids(el) { return safe(function () { return el.uiElements(); }, []); }
function uiRole(el) { return safe(function () { return el.role(); }, ''); }
function uiDesc(el) { return safe(function () { return el.description(); }, ''); }
function uiName(el) { return safe(function () { return el.name(); }, ''); }
// The set window holds one unnamed top group; its children are the Control
// bar, Browser, Arrangement, Mixer, Device Detail and so on, titled by name.
function mainContent(w) {
  var tops = uiKids(w);
  for (var i = 0; i < tops.length; i++) {
    if (uiRole(tops[i]) !== 'AXGroup') continue;
    var kids = uiKids(tops[i]);
    for (var j = 0; j < kids.length; j++)
      if (uiRole(kids[j]) === 'AXGroup' && uiName(kids[j]) === 'Control')
        return {window: w, kids: kids};
  }
  return null;
}
function findMain(p) {
  var ws = safe(function () { return p.windows(); }, []);
  for (var i = 0; i < ws.length; i++) {
    var c = mainContent(ws[i]);
    if (c) return c;
  }
  return null;
}
function controlOf(content) {
  for (var i = 0; i < content.kids.length; i++)
    if (uiName(content.kids[i]) === 'Control') return content.kids[i];
  return null;
}
// Every checkbox of the Control bar by description: true, false or null when
// its value is neither 0 nor 1.
function switchesOf(content) {
  var out = {}, ctl = controlOf(content);
  if (!ctl) return out;
  uiKids(ctl).forEach(function (g) {
    uiKids(g).forEach(function (el) {
      if (uiRole(el) !== 'AXCheckBox') return;
      var d = uiDesc(el);
      if (!d) return;
      var v = safe(function () { return el.value(); }, null);
      out[d] = (v === 1 || v === true) ? true : ((v === 0 || v === false) ? false : null);
    });
  });
  return out;
}
function shallowInfo(root) {
  var queue = [root], texts = [], buttons = [], seen = 0;
  while (queue.length && seen < 400) {
    var kids = uiKids(queue.shift());
    for (var i = 0; i < kids.length; i++) {
      seen++;
      var role = uiRole(kids[i]);
      if (role === 'AXStaticText') {
        var v = safe(function () { return kids[i].value(); }, '');
        if (v) texts.push(String(v));
      } else if (role === 'AXButton') {
        var d = uiDesc(kids[i]) || uiName(kids[i]);
        if (d && ['close button', 'minimize button', 'zoom button', 'full screen button'].indexOf(d) < 0)
          buttons.push(d);
      }
      if (HEAVY_ROLES.indexOf(role) < 0) queue.push(kids[i]);
    }
  }
  return {texts: texts.slice(0, 20), buttons: buttons.slice(0, 20)};
}
function focusChain(p) {
  var f = safe(function () { return p.attributes.byName('AXFocusedUIElement').value(); }, null);
  var chain = [];
  for (var i = 0; f && i < 14; i++) {
    var role = uiRole(f);
    chain.push({role: role, description: uiDesc(f), name: uiName(f)});
    if (role === 'AXWindow') break;
    f = safe(function () { return f.attributes.byName('AXParent').value(); }, null);
  }
  return chain;
}
var EDIT_COMMANDS = ['Cut Time', 'Copy Time', 'Duplicate Time', 'Delete Time', 'Delete'];
function editState(p) {
  var items = safe(function () {
    return p.menuBars[0].menuBarItems.byName('Edit').menus[0].menuItems();
  }, null);
  if (items === null) return null;
  var out = {};
  for (var i = 0; i < items.length; i++) {
    var n = safe(function () { return items[i].name(); }, '');
    if (EDIT_COMMANDS.indexOf(n) >= 0) out[n] = !!safe(function () { return items[i].enabled(); }, false);
  }
  return out;
}
function uiState(p, cmd) {
  var out = {running: true, frontmost: safe(function () { return p.frontmost(); }, false),
             windows: [], main: null, unreadable: []};
  var ws = safe(function () { return p.windows(); }, null);
  if (ws === null) { out.unreadable.push('windows'); ws = []; }
  ws.forEach(function (w) {
    var info = windowInfo(w);
    var content = out.main ? null : mainContent(w);
    if (content) {
      info.main = true;
      out.main = {
        name: info.name,
        document: safe(function () { return w.attributes.byName('AXDocument').value(); }, null),
        switches: switchesOf(content),
        groups: content.kids.map(uiName)
      };
    } else {
      var shallow = shallowInfo(w);
      info.texts = shallow.texts;
      info.buttons = shallow.buttons;
    }
    if (info.sheets) {
      var sheet = safe(function () { return w.sheets[0]; }, null);
      var sheetInfo = sheet ? shallowInfo(sheet) : {texts: [], buttons: []};
      info.sheet_texts = sheetInfo.texts;
      info.sheet_buttons = sheetInfo.buttons;
    }
    out.windows.push(info);
  });
  out.focus = focusChain(p);
  out.edit = editState(p);
  return out;
}
function dispatchUi(cmd, p) {
  if (cmd.op === 'stop_transport') {
    var content = findMain(p);
    if (!content) return {ok: false, error: "Live's set window could not be read"};
    var ctl = controlOf(content), stop = null;
    if (ctl) uiKids(ctl).forEach(function (g) {
      uiKids(g).forEach(function (el) {
        if (!stop && uiRole(el) === 'AXButton' && uiDesc(el) === 'Stop') stop = el;
      });
    });
    if (!stop) return {ok: false, error: "the Stop button was not found in the Control bar"};
    press(stop);
    delay(0.4);
    return {pressed: 'Stop'};
  }
  return undefined;
}
"""


class UnsafeUIState(Exception):
    """Live's UI is in a state in which clicks or keys would do the wrong thing."""

    def __init__(self, reasons: list[str], state: dict[str, Any] | None = None):
        self.reasons = list(reasons)
        self.state = state
        super().__init__("; ".join(self.reasons))


def _default_driver() -> Any:
    from MCP_Server import live_export

    return live_export.JXADriver()


# ── reading ──────────────────────────────────────────────────────


def _document_path(document: Any) -> str | None:
    """The set file's path from the window's AXDocument (a file URL or a path)."""
    if not document:
        return None
    text = str(document)
    if text.startswith("file://"):
        from urllib.parse import unquote, urlparse

        text = unquote(urlparse(text).path)
    return text or None


def _window_is_dialog(window: dict[str, Any]) -> bool:
    if window.get("main"):
        return bool(window.get("modal") or window.get("sheets"))
    if window.get("modal") or window.get("sheets"):
        return True
    if window.get("subrole") in DIALOG_SUBROLES:
        return True
    return window.get("name") in KNOWN_DIALOG_NAMES


def _dialog_entry(window: dict[str, Any]) -> dict[str, Any]:
    texts = list(window.get("texts") or []) + list(window.get("sheet_texts") or [])
    buttons = list(window.get("buttons") or []) + list(window.get("sheet_buttons") or [])
    return {
        "name": window.get("name") or "(untitled)",
        "subrole": window.get("subrole") or "",
        "modal": bool(window.get("modal")),
        "sheets": int(window.get("sheets") or 0),
        "text": texts,
        "buttons": buttons,
    }


def classify_focus(chain: list[dict[str, Any]], main_title: str = "") -> dict[str, Any]:
    """Which Live view holds keyboard focus, from the focused element's ancestors.

    Returns {view, element, reason?}; view is "arrangement", "session",
    "detail", "mixer", "browser", "control", "learn", "status", "dialog" (focus is in
    another window) or "unknown" (nothing focused, or the chain matches no
    known group). The nearest ancestor whose name is a known group decides.
    """
    if not chain:
        return {"view": "unknown", "element": None, "reason": "no element has accessibility focus"}
    first = chain[0]
    element = first.get("name") or first.get("description") or first.get("role")
    for link in chain:
        name = link.get("name") or ""
        if DETAIL_MARK in name and link.get("role") == "AXGroup":
            return {"view": "detail", "element": element}
        if link.get("role") == "AXGroup" and name in FOCUS_GROUPS:
            return {"view": FOCUS_GROUPS[name], "element": element}
    last = chain[-1]
    if last.get("role") == "AXWindow" and (last.get("name") or "") != main_title:
        return {"view": "dialog", "element": element}
    return {
        "view": "unknown",
        "element": element,
        "reason": "the focused element is not inside a view accessibility names",
    }


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def read_ui_state(
    driver: Any = None,
    armed_reader: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Read Live's UI state. ``ok`` is false only when accessibility itself failed.

    armed_reader returns Live's armed tracks (from the Remote Script socket);
    when it is missing or fails, ``armed_tracks`` is None and
    ``armed_tracks_error`` says why. The result always carries
    ``safe_to_drive`` and ``unsafe_reasons`` (see ``unsafe_reasons``).
    """
    driver = driver or _default_driver()
    raw = driver.call("ui_state")
    if not raw.get("ok", True):
        # osascript ran but System Events could not answer (a busy Live, a
        # timeout): that is not "Live is not running", and it is not safe.
        error = str(raw.get("error") or "System Events did not answer")
        state = {"ok": False, "running": None, "error": error, "unreadable": [error]}
        state.update(_empty_state(f"accessibility did not answer: {error}"))
        state["armed_tracks"] = None
        state["armed_tracks_error"] = "Live's UI could not be read"
        return _finish(state)
    state: dict[str, Any] = {"ok": True, "running": bool(raw.get("running"))}
    unreadable: list[str] = list(raw.get("unreadable") or [])
    state["unreadable"] = unreadable
    if not state["running"]:
        state.update(_empty_state("Live is not running"))
        state["armed_tracks"] = None
        state["armed_tracks_error"] = "Live is not running"
        return _finish(state)

    state["frontmost"] = bool(raw.get("frontmost"))
    main = raw.get("main")
    windows = raw.get("windows") or []
    switches = (main or {}).get("switches") or {}
    if main is None:
        unreadable.append("the set window (its Control bar was not found)")
    state["set"] = {
        "window_title": (main or {}).get("name"),
        "path": _document_path((main or {}).get("document")),
    }

    def switch(name: str) -> bool | None:
        value = _bool_or_none(switches.get(name))
        if value is None and main is not None:
            unreadable.append(f"the '{name}' switch")
        return value

    state["key_map_mode"] = switch(KEY_MAP_SWITCH)
    state["midi_map_mode"] = switch(MIDI_MAP_SWITCH)
    state["computer_midi_keyboard"] = switch(COMPUTER_KEYBOARD_SWITCH)

    dialogs, others = [], []
    for window in windows:
        if window.get("main"):
            if _window_is_dialog(window):
                dialogs.append(_dialog_entry(window))
        elif _window_is_dialog(window):
            dialogs.append(_dialog_entry(window))
        else:
            others.append(
                {"name": window.get("name") or "(untitled)", "subrole": window.get("subrole") or ""}
            )
    state["dialogs"] = dialogs
    state["other_windows"] = others

    # Live shows its crash report in the Learn View panel: the top-level group
    # of the set window then carries the title "Report a Crash" (seen after a
    # real crash; not a window and not modal, so it is reported, not refused).
    groups = (main or {}).get("groups")
    state["crash_report_panel"] = None if main is None else CRASH_REPORT_GROUP in (groups or [])

    state["focus"] = classify_focus(raw.get("focus") or [], (main or {}).get("name") or "")
    state["transport"] = _transport(switch)

    edit = raw.get("edit")
    state["time_selection"] = _time_selection(edit, state["focus"])
    state["items_selected"] = (
        None if edit is None or DELETE_COMMAND not in edit else bool(edit[DELETE_COMMAND])
    )
    if edit is None:
        unreadable.append("the Edit menu")

    state["armed_tracks"] = None
    if armed_reader is None:
        state["armed_tracks_error"] = "no connection to Live's Remote Script was given"
    else:
        try:
            state["armed_tracks"] = list(armed_reader())
        except Exception as e:
            state["armed_tracks_error"] = f"could not ask Live for its armed tracks: {e}"
    return _finish(state)


def _empty_state(reason: str) -> dict[str, Any]:
    return {
        "frontmost": False,
        "set": {"window_title": None, "path": None},
        "key_map_mode": None,
        "midi_map_mode": None,
        "computer_midi_keyboard": None,
        "crash_report_panel": None,
        "dialogs": [],
        "other_windows": [],
        "focus": {"view": "unknown", "element": None, "reason": reason},
        "transport": dict.fromkeys(TRANSPORT_KEYS) | {"recording": None},
        "time_selection": {"present": None, "reason": reason},
        "items_selected": None,
    }


def _transport(switch: Callable[[str], bool | None]) -> dict[str, bool | None]:
    arrangement_record = switch(SWITCH_ARRANGEMENT_RECORD)
    session_record = switch(SWITCH_SESSION_RECORD)
    if arrangement_record is None or session_record is None:
        recording = None
    else:
        recording = arrangement_record or session_record
    return {
        "playing": switch(SWITCH_PLAY),
        "recording": recording,
        "arrangement_record": arrangement_record,
        "session_record": session_record,
        "overdub": switch(SWITCH_OVERDUB),
        "loop": switch(SWITCH_LOOP),
        "punch_in": switch(SWITCH_PUNCH_IN),
        "punch_out": switch(SWITCH_PUNCH_OUT),
    }


def _time_selection(edit: dict[str, bool] | None, focus: dict[str, Any]) -> dict[str, Any]:
    """Whether the Arrangement has a time selection.

    Edit > Cut/Copy/Duplicate/Delete Time are enabled only for a time
    selection, but Live enables them only while the Arrangement has focus,
    so with focus elsewhere "off" cannot be told from "no selection".
    """
    if edit is None:
        return {"present": None, "reason": "the Edit menu could not be read"}
    enabled = [name for name in TIME_COMMANDS if edit.get(name)]
    if enabled:
        return {"present": True, "commands_enabled": enabled}
    if focus.get("view") == "arrangement":
        return {"present": False}
    return {
        "present": None,
        "reason": "Edit's time commands only reflect the Arrangement while it has "
        f"keyboard focus (focus is on: {focus.get('view')})",
    }


def _finish(state: dict[str, Any]) -> dict[str, Any]:
    reasons = unsafe_reasons(state)
    state["unsafe_reasons"] = reasons
    state["safe_to_drive"] = not reasons
    return state


# ── the guard ────────────────────────────────────────────────────


def unsafe_reasons(state: dict[str, Any], ignore_transport: bool = False) -> list[str]:
    """Why clicks or keys would be unsafe in this state; empty when they are safe.

    Anything accessibility could not read counts as unsafe: the guard never
    assumes a mode is off. ``ignore_transport`` skips the playing/recording
    checks for post-tool cleanup (``finish_ui_tool``), which stops playback a
    click started.
    """
    if state.get("ok") is False:
        return [f"could not read Live's UI through accessibility: {state.get('error')}"]
    if not state.get("running"):
        return ["Ableton Live is not running"]
    reasons: list[str] = []
    # With no set window (Live is still starting behind a dialog) every switch is
    # unreadable for that one reason; say it once, below, instead of six times.
    no_set_window = any(
        item.startswith("the set window") for item in state.get("unreadable") or []
    )
    if state.get("key_map_mode") is True:
        reasons.append(
            "Key Map mode is on (every click assigns a key); turn it off with the Key button "
            "or Cmd+K"
        )
    elif state.get("key_map_mode") is None and not no_set_window:
        reasons.append("could not read whether Key Map mode is on, so clicks may assign keys")
    if state.get("midi_map_mode") is True:
        reasons.append(
            "MIDI Map mode is on (every click assigns a MIDI control); turn it off with the "
            "MIDI button or Cmd+M"
        )
    elif state.get("midi_map_mode") is None and not no_set_window:
        reasons.append("could not read whether MIDI Map mode is on, so clicks may map controls")
    for dialog in state.get("dialogs") or []:
        text = " | ".join(dialog.get("text") or [])
        detail = f": {text}" if text else ""
        reasons.append(f"Live is showing a dialog, '{dialog.get('name')}'{detail}")
    if state.get("computer_midi_keyboard") is True:
        reasons.append(
            "Computer MIDI Keyboard is on (plain letter keys play notes); turn it off with Caps Lock"
            " or the keyboard button"
        )
    if not ignore_transport:
        transport = state.get("transport") or {}
        if transport.get("playing") is True:
            reasons.append("Live is playing; stop playback first")
        elif transport.get("playing") is None and not no_set_window:
            reasons.append("could not read whether Live is playing")
        if transport.get("recording") is True:
            reasons.append("Live is recording (Arrangement Record or Session Record is on)")
        elif transport.get("recording") is None and not no_set_window:
            reasons.append("could not read whether Live is recording")
    for item in state.get("unreadable") or []:
        if item == "windows" or item.startswith("the set window"):
            reasons.append(f"accessibility could not read {item}")
    return reasons


def require_safe_to_drive(
    driver: Any = None,
    armed_reader: Callable[[], list[dict[str, Any]]] | None = None,
    ignore_transport: bool = False,
) -> dict[str, Any]:
    """Read the UI state and raise ``UnsafeUIState`` unless it is safe to drive.

    Call this first in every tool that clicks or types in Live. Returns the
    state it read when it is safe.
    """
    state = read_ui_state(driver, armed_reader)
    reasons = unsafe_reasons(state, ignore_transport=ignore_transport)
    if reasons:
        raise UnsafeUIState(reasons, state)
    return state


# ── transport ────────────────────────────────────────────────────


def transport_snapshot(state: dict[str, Any]) -> dict[str, bool | None]:
    """The transport switches of a UI state, for comparing before and after."""
    transport = state.get("transport") or {}
    return {key: transport.get(key) for key in TRANSPORT_KEYS}


def compare_transport(before: dict[str, bool | None], after: dict[str, bool | None]) -> list[str]:
    """The transport switches that differ between two snapshots, as sentences."""
    changes = []
    for key in TRANSPORT_KEYS:
        if before.get(key) != after.get(key):
            changes.append(f"{key} changed from {before.get(key)} to {after.get(key)}")
    return changes


# ── clearing the selection ───────────────────────────────────────


def _menu(driver: Any, steps: list[dict[str, Any]], path: list[str]) -> dict[str, Any]:
    result = driver.call("menu", path=path)
    steps.append({"op": "menu", "path": path, "clicked": result.get("clicked")})
    if not result.get("ok", False):
        raise UIStateError(f"{' > '.join(path)}: {result.get('error') or 'failed'}")
    return result


class UIStateError(Exception):
    """A step of a UI-state action failed; the message is reported verbatim."""


def clear_selection(
    driver: Any = None,
    armed_reader: Callable[[], list[dict[str, Any]]] | None = None,
    ignore_transport: bool = False,
) -> dict[str, Any]:
    """Remove the Arrangement time selection and prove the transport is unchanged.

    Uses Playback > Move Insert Marker To Playhead, which replaces the
    selection with the insert marker: no click in a lane and no key is sent
    (a click once started playback). When accessibility cannot tell whether
    there is a selection because another view has focus, Navigate >
    Arrangement View moves focus to the Arrangement first. After clearing,
    the selection is read back and the transport compared with how it was;
    if the transport started playing, Stop is pressed. A transport that
    still differs, or a selection that is still there, makes ``ok`` false.
    Refuses (UnsafeUIState) in Key Map or MIDI Map mode, with a dialog open
    and, like every UI tool, while Live is playing or recording; the cleanup
    that runs after a UI tool passes ``ignore_transport`` because stopping
    playback a click started is part of its job.
    """
    driver = driver or _default_driver()
    steps: list[dict[str, Any]] = []
    result: dict[str, Any] = {"ok": False, "steps": steps, "warnings": []}
    before = require_safe_to_drive(driver, armed_reader, ignore_transport=ignore_transport)
    transport_before = transport_snapshot(before)
    result["transport_before"] = transport_before
    result["time_selection_before"] = before["time_selection"]
    try:
        present = before["time_selection"]["present"]
        if present is None:
            _menu(driver, steps, [NAVIGATE_MENU, ARRANGEMENT_VIEW_MENU_ITEM])
            present = read_ui_state(driver, armed_reader)["time_selection"]["present"]
        if present is not False:
            _menu(driver, steps, [PLAYBACK_MENU, MOVE_MARKER_MENU_ITEM])
        after = read_ui_state(driver, armed_reader)
        changes = compare_transport(transport_before, transport_snapshot(after))
        if (
            changes
            and transport_before.get("playing") is False
            and (transport_snapshot(after).get("playing") is True)
        ):
            result["stopped_playback"] = True
            result["warnings"].append("playback started while clearing the selection; stopped it")
            driver.call("stop_transport")
            steps.append({"op": "stop_transport"})
            after = read_ui_state(driver, armed_reader)
            changes = compare_transport(transport_before, transport_snapshot(after))
        result["transport_after"] = transport_snapshot(after)
        result["transport_changed"] = changes
        result["time_selection_after"] = after["time_selection"]
        result["items_selected_after"] = after["items_selected"]
        remaining = after["time_selection"]["present"]
        if remaining is not False:
            reason = (
                "a time selection is still present"
                if remaining
                else (
                    "could not confirm the time selection is gone: "
                    f"{after['time_selection'].get('reason')}"
                )
            )
            result["error"] = reason
        elif changes:
            result["error"] = "the transport changed: " + "; ".join(changes)
        else:
            result["ok"] = True
    except UIStateError as e:
        result["error"] = str(e)
    return result


def finish_ui_tool(
    driver: Any,
    transport_before: dict[str, bool | None],
    armed_reader: Callable[[], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Leave Live as a UI tool found it: no time selection, same transport.

    Clears the selection (see ``clear_selection``) and checks the transport
    against ``transport_before``, stopping playback a click started. Never
    raises: the outcome is returned for the tool to put in its reply:
    {selection_cleared, transport_unchanged, transport_changed, error?}.
    """
    outcome: dict[str, Any] = {}
    try:
        cleared = clear_selection(driver, armed_reader, ignore_transport=True)
    except UnsafeUIState as e:
        outcome.update(
            {
                "selection_cleared": False,
                "transport_unchanged": None,
                "error": "Live's UI is not safe to touch, so the selection was not cleared: "
                + "; ".join(e.reasons),
            }
        )
        return outcome
    except Exception as e:
        outcome.update(
            {
                "selection_cleared": False,
                "transport_unchanged": None,
                "error": f"could not clear the selection: {e}",
            }
        )
        return outcome
    changes = compare_transport(transport_before, cleared.get("transport_after") or {})
    outcome["selection_cleared"] = cleared.get("time_selection_after", {}).get("present") is False
    outcome["transport_unchanged"] = not changes
    outcome["transport_changed"] = changes
    if cleared.get("stopped_playback"):
        outcome["stopped_playback"] = True
    if not cleared.get("ok") or changes:
        outcome["error"] = cleared.get("error") or "the transport changed: " + "; ".join(changes)
    return outcome
