"""Run the real JXA driver (live_export.JXA_DRIVER) against a fake System Events.

osascript cannot run in unit tests, but the driver is plain JavaScript, so
these tests execute it under Node with ``Application('System Events')``
replaced by a small fake of Live's accessibility tree. The fake copies what
Live 12.4 really exposes, observed against a running Live: a chooser
(``AXPopUpButton``) ignores ``AXValue`` writes and, when pressed, opens a new
*untitled* ``AXUnknown`` window of ``AXMenuItem``s whose current choice is
prefixed with a check mark, while another untitled ``AXUnknown`` window is
usually already open. Esc closes only that menu.

Skipped when Node is not installed.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from MCP_Server import live_export as le  # noqa: E402

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# Builds the fake world, then the driver source is appended and run() is
# called with the JSON command passed on argv.
_PRELUDE = r"""
var LOG = {keys: [], stray: [], pressed: [], clicked: [], typed: [], activated: 0, relaunched: 0, focused: 0, stale_focus: 0, heavy: 0};  // stray: keys sent while Live was not frontmost
var WORLD = JSON.parse(process.argv[2]);
var CLOCK = {now: 0, g: []};  // fake seconds; g: when each Cmd+Shift+G arrived
var SHEET = WORLD.sheet || {};  // late: seconds before the sheet shows; ignore: shortcuts swallowed
var ignored = 0;
function delay(s) {
  CLOCK.now += s;
  if (FOCUS_READY_AT !== null && CLOCK.now >= FOCUS_READY_AT - 1e-9) {
    KEYBOARD_FOCUS = true;
    FOCUS_READY_AT = null;
  }
  if (SHEET.closes !== undefined && CLOCK.now >= SHEET.closes - 1e-9) closePanel();
  if (SHEET.dismisses !== undefined && CLOCK.now >= SHEET.dismisses - 1e-9) dismissSheet();
  syncSheet();
}
function closePanel() {
  // The Save panel goes away; System Events throws on a closed window's reference.
  windows = windows.filter(function (w) {
    if (w.name() !== 'Save') return true;
    w.name = function () { throw new Error("Can't get object."); };
    return false;
  });
}
function dismissSheet() {
  CLOCK.dismissed = true;
  windows.forEach(function (w) { w.sheets = []; });
}
function syncSheet() {
  // The sheet shows SHEET.late seconds after the first shortcut that was not swallowed.
  if (SHEET.never || CLOCK.opened === undefined || CLOCK.done || CLOCK.dismissed) return;
  if (CLOCK.now - CLOCK.opened < (SHEET.late || 0) - 1e-9) return;
  windows.forEach(function (w) {
    if (w.name() !== 'Save' || w.sheets.length) return;
    w.sheets = [mk({role: 'AXSheet', kids: SHEET.no_field ? [] : [
      {role: 'AXTextField', description: 'path', writable: true}]})];
    if (SHEET.front) setFront(SHEET.front);
  });
}
function mk(spec) {
  var o = {spec: spec};
  o.role = function () { return spec.role; };
  o.subrole = function () { return spec.subrole || null; };
  o.description = function () { return spec.description || ''; };
  o.title = function () { return spec.title || ''; };
  o.name = function () { return spec.name || ''; };
  o.focused = function () {
    return spec.role === 'AXWindow' && spec.name === 'Save' && KEYBOARD_FOCUS;
  };
  // Live ignores AXValue writes, so assigning el.value does nothing.
  var valueFn = function () { return spec.value; };
  // Text fields (spec.writable) do take writes.
  Object.defineProperty(o, 'value', {get: function () { return valueFn; },
    set: function (v) { if (spec.writable) { spec.value = v; LOG.typed.push(v); } }});
  o.enabled = function () { return spec.enabled !== false; };
  o.sheets = [];
  o.attributes = {byName: function () { return {value: function () { return !!spec.modal; }}; }};
  o.entireContents = function () {
    var out = [];
    (function walk(list) { list.forEach(function (c) { out.push(c); walk(c.kids || []); }); })(o.kids || []);
    return out;
  };
  o.actions = {byName: function (n) { return {perform: function () {
    if ((n === 'AXIncrement' || n === 'AXDecrement') && spec.slider) {
      // Like Live's sliders: one step each, wrapping (beats, sixteenths) or
      // stopping at the end (bars).
      LOG.nudges = (LOG.nudges || 0) + 1;
      var sl = spec.slider, v = parseInt(spec.value, 10) + (n === 'AXIncrement' ? 1 : -1);
      if (v > sl.max) v = sl.wrap ? sl.min : sl.max;
      if (v < sl.min) v = sl.wrap ? sl.max : sl.min;
      spec.value = String(v);
      return;
    }
    if (n !== 'AXPress') throw new Error('unsupported action ' + n);
    LOG.pressed.push(spec.description || spec.name);
    if (spec.press) ACTIONS[spec.press](o);
  }}; }};
  o.kids = (spec.kids || []).map(mk);
  o.uiElements = function () {
    if (spec.heavy) LOG.heavy++;  // a file list the driver should not walk
    return o.kids;
  };
  return o;
}
var windows = WORLD.windows.map(mk);
// A sheet already on a window (open_sheet), e.g. a Go to Folder sheet left
// behind when osascript was killed mid save_panel.
windows.forEach(function (w) { if (w.spec.open_sheet) w.sheets = [mk(w.spec.open_sheet)]; });
function byDesc(desc) {
  var hit = null;
  windows.forEach(function (w) { w.entireContents().forEach(function (c) {
    if (c.spec.description === desc) hit = c; }); });
  return hit;
}
var ACTIONS = {
  openMenu: function (popup) {
    var menu = mk({role: 'AXWindow', subrole: 'AXUnknown', name: '', kids: [
      {role: 'AXGroup', description: 'menu', kids: popup.spec.options.map(function (opt, i) {
        var current = popup.spec.selected === undefined ? opt === popup.spec.value : i === popup.spec.selected;
        var mark = popup.spec.mark === undefined ? '✔ ' : popup.spec.mark;
        return {role: 'AXMenuItem', description: 'menu item', target: popup.spec.description,
                index: i, option: opt, name: (current ? mark : '') + opt, press: 'choose'};
      })}]});
    windows.unshift(menu);
  },
  choose: function (item) {
    var popup = byDesc(item.spec.target);
    popup.spec.value = item.spec.option;
    popup.spec.selected = item.spec.index;
    windows = windows.filter(function (w) {
      return !w.entireContents().some(function (c) { return c.role() === 'AXMenuItem'; });
    });
  },
  toggle: function (sw) { sw.spec.value = sw.spec.value === 'On' ? 'Off' : 'On'; },
  // A Cancel button closes its window, unless a sheet on it blocks it.
  closeWindow: function (btn) {
    windows = windows.filter(function (w) {
      return w.sheets.length || w.entireContents().indexOf(btn) < 0;
    });
  },
  closeSheet: function (btn) {
    windows.forEach(function (w) {
      w.sheets = w.sheets.filter(function (s) { return s.entireContents().indexOf(btn) < 0; });
    });
  }
};
// Live is frontmost unless the world says it lost the front: front: 'lost' gets
// it back on a focus request; front: 'never' ignores one. Keys sent while
// it is not frontmost go to another app: they do nothing here and are logged
// as stray.
var FRONT_MODE, FRONT, KEYBOARD_FOCUS, FOCUS_READY_AT = null;
var RUNNING = true, PROCESS_LOOKUPS = 0;
function setFront(mode) {
  FRONT_MODE = mode;
  FRONT = mode === 'yes';
  KEYBOARD_FOCUS = FRONT;
  FOCUS_READY_AT = null;
}
setFront(WORLD.front || 'yes');
var FAKE_PROC = {
  windows: function () { return windows; },
  menuBars: [{menuBarItems: {byName: function (menu) {
    return {menus: [{menuItems: function () {
      if (!(WORLD.menus || {})[menu]) throw new Error("Can't get object.");
      return WORLD.menus[menu].map(function (item) {
        return {name: function () { return item.name; },
                enabled: function () { return item.enabled !== false; },
                click: function () { LOG.clicked.push(menu + ' > ' + item.name); }};
      });
    }}]};
  }}}]
};
Object.defineProperty(FAKE_PROC, 'frontmost', {
  get: function () { return function () {
    if (FRONT_MODE === 'quit') { RUNNING = false; FRONT = false; }
    return FRONT;
  }; },
  set: function (value) {
    LOG.focused++;
    if (FRONT_MODE === 'quit_during_focus') RUNNING = false;
    if (!RUNNING) { LOG.stale_focus++; throw new Error("Can't get object."); }
    if (value && FRONT_MODE !== 'never') {
      FRONT = true;
      if (FRONT_MODE === 'settling') FOCUS_READY_AT = CLOCK.now + 0.3;
      else KEYBOARD_FOCUS = true;
    }
  }
});
// The driver asks Foundation for a folder's Finder display name; the world
// gives them per path as {file: displayNameAtPath, url: NSURLLocalizedNameKey}.
var DISPLAY = WORLD.display_names || {};
var ObjC = {import: function () {}, unwrap: function (x) { return x; }};
function Ref() { return []; }
var $ = {
  NSURLLocalizedNameKey: 'NSURLLocalizedNameKey',
  NSFileManager: {defaultManager: {displayNameAtPath: function (p) {
    return {js: (DISPLAY[p] || {}).file || p.split('/').pop()};
  }}},
  NSURL: {fileURLWithPath: function (p) {
    return {getResourceValueForKeyError: function (ref) { ref[0] = (DISPLAY[p] || {}).url || null; return true; }};
  }}
};
function Application(name) {
  if (name !== 'System Events') return {activate: function () {
    LOG.activated++;
    if (!RUNNING) { LOG.relaunched++; RUNNING = true; }
    if (FRONT_MODE === 'lost') FRONT = true;
  }};
  return {
    processes: {whose: function () {
      PROCESS_LOOKUPS++;
      var found = RUNNING ? [FAKE_PROC] : [];
      if (FRONT_MODE === 'quit_during_focus' && PROCESS_LOOKUPS > 1 && found.length) RUNNING = false;
      return found;
    }},
    keyCode: function (code) {
      if (!FRONT || !KEYBOARD_FOCUS) { LOG.stray.push(code); return; }
      LOG.keys.push(code);
      if (code === 36) { CLOCK.done = true; windows.forEach(function (w) { w.sheets = []; }); }  // Return: go
      if (code === 53) {  // Esc: close a sheet, else a window that Esc cancels, else a menu
        var sheeted = windows.filter(function (w) { return w.sheets.length; });
        if (sheeted.length) { sheeted[0].sheets = []; return; }
        var escapable = windows.filter(function (w) { return w.spec.esc_closes; });
        if (escapable.length) { windows = windows.filter(function (w) { return w !== escapable[0]; }); return; }
        windows = windows.filter(function (w) {
          return !w.entireContents().some(function (c) { return c.role() === 'AXMenuItem'; });
        });
      }
    },
    keystroke: function (k, opts) {
      if (!FRONT || !KEYBOARD_FOCUS) { LOG.stray.push(k); return; }
      LOG.keys.push(k);
      // Cmd+Shift+G opens the Go to Folder sheet on the Save panel, once the
      // world's slow-sheet settings allow it.
      if (k === 'g' && opts && (opts.using || []).indexOf('command down') >= 0) {
        CLOCK.g.push(CLOCK.now);
        if (ignored < (SHEET.ignore || 0)) { ignored++; return; }
        if (CLOCK.opened === undefined) CLOCK.opened = CLOCK.now;
        syncSheet();
      }
    },
    click: function (el) { el.actions.byName('AXPress').perform(); }
  };
}
"""

_EPILOGUE = r"""
var reply = JSON.parse(run([process.argv[1]]));
reply._log = LOG;
reply._clock = CLOCK;
reply._windows = windows.map(function (w) { return [w.name(), w.subrole()]; });
reply._values = {};
reply._selected = {};
windows.forEach(function (w) { w.entireContents().forEach(function (c) {
  if (c.spec.description && c.spec.value !== undefined) reply._values[c.spec.description] = c.spec.value;
  if (c.spec.selected !== undefined) reply._selected[c.spec.description] = c.spec.selected; }); });
console.log(JSON.stringify(reply));
"""


def export_dialog_world(sample_rate="44100", normalize="Off"):
    """Live with the Export dialog open, as observed on Live 12.4.5."""
    return {"windows": [
        {"role": "AXWindow", "subrole": "AXDialog", "name": "Export Audio/Video", "kids": [
            {"role": "AXStaticText", "name": "Sample Rate", "value": "Sample Rate"},
            {"role": "AXPopUpButton", "description": "Sample Rate", "value": sample_rate,
             "options": ["22050", "32000", "44100", "48000", "88200", "96000"], "press": "openMenu"},
            {"role": "AXStaticText", "name": "Normalize", "value": "Normalize"},
            {"role": "AXButton", "subrole": "AXSwitch", "description": "Normalize",
             "value": normalize, "press": "toggle"},
            {"role": "AXButton", "description": "Cancel"},
        ]},
        # Live keeps an untitled AXUnknown window open next to its dialogs.
        {"role": "AXWindow", "subrole": "AXUnknown", "name": ""},
        {"role": "AXWindow", "subrole": "AXStandardWindow", "name": "Untitled"},
    ]}


def drive(op, world, **params):
    cmd = dict(params, op=op, bundle=le.LIVE_BUNDLE_ID)
    proc = subprocess.run(
        [NODE, "-e", _PRELUDE + le.JXA_DRIVER + _EPILOGUE, "--", json.dumps(cmd), json.dumps(world)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_set_choice_picks_from_untitled_menu_window():
    # Regression: the menu window is untitled like an already-open window, so
    # filtering new windows by name never found the menu and Sample Rate
    # stayed at 44100 against a real Live.
    reply = drive("set_choice", export_dialog_world(), window="Export Audio/Video",
                  label="Sample Rate", values=["48000"])
    assert reply["ok"] is True, reply
    assert (reply["before"], reply["after"]) == ("44100", "48000")
    assert reply["_values"]["Sample Rate"] == "48000"
    assert "✔ 44100" in reply["seen"]
    assert reply["_log"]["pressed"] == ["Sample Rate", "menu item"]
    # the menu closed and the Export dialog is still open
    assert ["Export Audio/Video", "AXDialog"] in reply["_windows"]
    assert len(reply["_windows"]) == 3


def test_set_choice_already_selected_does_nothing():
    reply = drive("set_choice", export_dialog_world(sample_rate="48000"),
                  window="Export Audio/Video", label="Sample Rate", values=["48000"])
    assert (reply["before"], reply["after"]) == ("48000", "48000")
    assert reply["_log"]["pressed"] == []


def test_set_choice_missing_option_closes_menu_with_esc_and_lists_choices():
    reply = drive("set_choice", export_dialog_world(), window="Export Audio/Video",
                  label="Sample Rate", values=["192000"])
    assert reply["after"] == "44100"
    assert reply["seen"] == ["22050", "32000", "✔ 44100", "48000", "88200", "96000"]
    assert reply["_log"]["keys"] == [53]
    assert ["Export Audio/Video", "AXDialog"] in reply["_windows"]
    assert len(reply["_windows"]) == 3


def test_set_toggle_presses_switch_and_reads_it_back():
    reply = drive("set_toggle", export_dialog_world(), window="Export Audio/Video",
                  label="Normalize", on=True)
    assert reply["ok"] is True, reply
    assert (reply["before"], reply["after"]) == (False, True)
    assert reply["_values"]["Normalize"] == "On"


def test_status_reports_windows_and_modal_flag():
    world = export_dialog_world()
    world["windows"][0]["modal"] = True
    reply = drive("status", world)
    assert reply["running"] is True
    assert reply["windows"][0] == {"name": "Export Audio/Video", "subrole": "AXDialog",
                                   "modal": True, "sheets": 0}


def untitled_windows_world():
    """An untitled window that was already open, listed before Live's untitled alert."""
    return {"windows": [
        {"role": "AXWindow", "subrole": "AXUnknown", "name": "", "kids": [
            {"role": "AXStaticText", "value": "Info View"}]},
        {"role": "AXWindow", "subrole": "AXDialog", "name": "", "kids": [
            {"role": "AXGroup", "kids": [
                {"role": "AXStaticText", "value": "The file could not be written."}]},
            {"role": "AXButton", "description": "OK"}]},
        {"role": "AXWindow", "subrole": "AXStandardWindow", "name": "Untitled", "kids": [
            {"role": "AXStaticText", "value": "1 Audio"}]},
    ]}


def test_texts_reads_every_untitled_window():
    # Regression: only the first untitled window was read, so an untitled
    # alert listed after an older untitled window was never seen.
    reply = drive("texts", untitled_windows_world(), window="")
    assert reply["ok"] is True, reply
    assert reply["texts"] == ["Info View", "The file could not be written."]


def test_texts_of_a_titled_window_are_only_its_own():
    reply = drive("texts", untitled_windows_world(), window="Untitled")
    assert reply["texts"] == ["1 Audio"]


def test_texts_of_a_window_that_is_not_open_are_empty():
    reply = drive("texts", untitled_windows_world(), window="Alert")
    assert reply["ok"] is True, reply
    assert reply["texts"] == []


def menu_world(select_loop_enabled=True):
    world = export_dialog_world()
    world["menus"] = {
        "Navigate": [{"name": "Session View"}, {"name": "Arrangement View"}],
        "Edit": [{"name": "Select All"}, {"name": "Select Loop", "enabled": select_loop_enabled}],
    }
    return world


def test_menu_clicks_an_enabled_item():
    reply = drive("menu", menu_world(), path=["Navigate", "Arrangement View"])
    assert reply["ok"] is True, reply
    assert reply["clicked"] == "Arrangement View"
    assert reply["_log"]["clicked"] == ["Navigate > Arrangement View"]


def test_menu_does_not_click_a_disabled_item():
    reply = drive("menu", menu_world(select_loop_enabled=False), path=["Edit", "Select Loop"])
    assert reply["ok"] is False
    assert reply["disabled"] is True
    assert reply["error"] == "menu item 'Edit > Select Loop' is disabled"
    assert reply["_log"]["clicked"] == []


def test_menu_reports_a_missing_item_and_a_missing_menu():
    reply = drive("menu", menu_world(), path=["Edit", "Select Loop Brace"])
    assert reply["ok"] is False and reply["missing"] is True
    assert reply["_log"]["clicked"] == []

    reply = drive("menu", menu_world(), path=["View", "Arrangement View"])
    assert reply["ok"] is False
    assert reply["_log"]["clicked"] == []


TRACK_LIST = ["Main", "All Individual Tracks", "Selected Tracks Only", "1 Audio", "Master"]


def rendered_track_world(options, selected, opens=True):
    """The Export dialog with the Rendered Track list as Live 12.4.5 orders it:
    Live's own bus first, then the track modes, then every track by name."""
    world = export_dialog_world()
    chooser = {"role": "AXPopUpButton", "description": "Rendered Track Chooser",
               "value": options[selected], "selected": selected, "options": options}
    if opens:
        chooser["press"] = "openMenu"
    world["windows"][0]["kids"].insert(0, chooser)
    return world


def choose_main(world):
    return drive("set_choice", world, window="Export Audio/Video",
                 label="Rendered Track Chooser", values=["Main", "Master"], first=True)


def test_set_choice_first_takes_the_top_entry_when_a_track_named_master_is_selected():
    # Regression: the chooser read "Master", a wanted name, so nothing was
    # pressed and that one track was rendered instead of Live's Main bus.
    reply = choose_main(rendered_track_world(TRACK_LIST, selected=4))
    assert reply["ok"] is True, reply
    assert (reply["before"], reply["after"], reply["picked"]) == ("Master", "Main", "Main")
    assert reply["_selected"]["Rendered Track Chooser"] == 0
    assert reply["seen"] == ["Main"]
    assert reply["_log"]["pressed"] == ["Rendered Track Chooser", "menu item"]
    assert len(reply["_windows"]) == 3


def test_set_choice_first_takes_the_top_entry_when_a_track_named_main_is_selected():
    options = ["Main", "All Individual Tracks", "Selected Tracks Only", "Main"]
    reply = choose_main(rendered_track_world(options, selected=3))
    assert reply["ok"] is True, reply
    assert (reply["before"], reply["after"], reply["picked"]) == ("Main", "Main", "Main")
    assert reply["_selected"]["Rendered Track Chooser"] == 0


def test_set_choice_first_presses_the_top_entry_even_when_it_is_already_checked():
    reply = choose_main(rendered_track_world(TRACK_LIST, selected=0))
    assert reply["ok"] is True, reply
    assert reply["seen"] == ["✔ Main"]
    assert reply["picked"] == "Main"
    assert reply["_selected"]["Rendered Track Chooser"] == 0
    assert reply["_log"]["pressed"] == ["Rendered Track Chooser", "menu item"]


def test_set_choice_first_refuses_a_wanted_name_that_is_not_the_top_entry():
    options = ["Selected Tracks Only", "Main", "Master"]
    reply = choose_main(rendered_track_world(options, selected=2))
    assert reply["ok"] is False
    assert reply["error"] == ("the first entry in the 'Rendered Track Chooser' list is "
                              "'Selected Tracks Only', expected Main or Master")
    assert "picked" not in reply
    assert reply["_selected"]["Rendered Track Chooser"] == 2
    # only the chooser was pressed; Esc closed its list and left the dialog open
    assert reply["_log"]["pressed"] == ["Rendered Track Chooser"]
    assert reply["_log"]["keys"] == [53]
    assert ["Export Audio/Video", "AXDialog"] in reply["_windows"]
    assert len(reply["_windows"]) == 3


def test_set_choice_first_fails_when_the_list_does_not_open():
    reply = choose_main(rendered_track_world(TRACK_LIST, selected=4, opens=False))
    assert reply["ok"] is False
    assert reply["error"] == ("the first entry in the 'Rendered Track Chooser' list is "
                              "missing, expected Main or Master")
    assert reply["_selected"]["Rendered Track Chooser"] == 4


@pytest.mark.parametrize("mark", ["\u2714\ufe0e", "\u2714\ufe0e ", "\u2714 ", "\u2713", "\u2713 "])
def test_set_choice_first_accepts_any_check_mark_on_the_top_entry(mark):
    # Live's check mark may carry a variation selector (U+FE0E) and a space;
    # every leading character that is not a letter or digit is dropped.
    world = rendered_track_world(TRACK_LIST, selected=0)
    world["windows"][0]["kids"][0]["mark"] = mark
    reply = choose_main(world)
    assert reply["ok"] is True, reply
    assert reply["seen"] == [mark + "Main"]
    assert reply["picked"] == "Main"
    assert reply["_selected"]["Rendered Track Chooser"] == 0


@pytest.mark.parametrize("mark", ["\u2714\ufe0e", "\u2714 "])
def test_set_choice_matches_a_checked_entry_that_is_not_first(mark):
    # The chooser reads a value that is not wanted, so the list is opened and
    # the checked entry is matched by name despite its mark.
    world = export_dialog_world()
    chooser = world["windows"][0]["kids"][1]
    chooser.update(value="unknown", selected=2, mark=mark)
    reply = drive("set_choice", world, window="Export Audio/Video",
                  label="Sample Rate", values=["44100"])
    assert reply["ok"] is True, reply
    assert mark + "44100" in reply["seen"]
    assert reply["picked"] == "44100"
    assert reply["after"] == "44100"


def save_panel_world(where, display_names=None, sheet=None, front="yes"):
    """Live's Save panel: a writable Save As field and a read-only Where pop-up.

    ``sheet`` shapes the Go to Folder sheet: ``late`` seconds before it shows,
    ``ignore`` shortcuts swallowed first, ``never`` to leave it closed,
    ``no_field`` for a sheet whose path can only be typed, ``front`` for what
    becomes of Live's front once it shows (``lost``, ``never``, ``quit``, or
    ``quit_during_focus``, or ``settling``), and
    ``closes`` seconds before the Save panel goes away or ``dismisses`` seconds
    before its sheet goes away.
    """
    return {"sheet": sheet or {}, "front": front, "windows": [
        {"role": "AXWindow", "subrole": "AXDialog", "name": "Save", "kids": [
            {"role": "AXTextField", "description": "Save As:", "value": "", "writable": True},
            {"role": "AXPopUpButton", "description": "Where:", "value": where},
            {"role": "AXButton", "description": "Cancel"},
            {"role": "AXButton", "description": "Save"},
        ]},
        {"role": "AXWindow", "subrole": "AXStandardWindow", "name": "Untitled"},
    ], "display_names": display_names or {}}


def save(world, directory):
    return drive("save_panel", world, window="Save", directory=directory, filename="song",
                 folder_name=os.path.basename(directory.rstrip("/")), replace=False)


def test_save_panel_accepts_the_on_disk_folder_name():
    reply = save(save_panel_world("live-test"), "/tmp/x/live-test")
    assert reply["ok"] is True, reply
    assert reply["where"] == "live-test"
    assert reply["_values"]["Save As:"] == "song"
    assert reply["_log"]["pressed"] == ["Save"]


@pytest.mark.parametrize("where", ["Desktop \u2014 iCloud", "Desktop \u2013 iCloud", "Desktop - iCloud"])
def test_save_panel_accepts_a_synced_folder_with_an_icloud_suffix(where):
    # Regression: Desktop and Documents synced to iCloud show as
    # "Desktop — iCloud", which failed an otherwise correct export.
    reply = save(save_panel_world(where), "/Users/me/Desktop")
    assert reply["ok"] is True, reply
    assert reply["where"] == where
    assert reply["_log"]["pressed"] == ["Save"]


def test_save_panel_accepts_the_finder_display_name_of_icloud_drive():
    path = "/Users/me/Library/Mobile Documents/com~apple~CloudDocs"
    world = save_panel_world("iCloud Drive", {path: {"url": "iCloud Drive"}})
    reply = save(world, path)
    assert reply["ok"] is True, reply
    assert reply["_log"]["pressed"] == ["Save"]


def test_save_panel_accepts_a_localized_display_name():
    path = "/Users/me/Desktop"
    world = save_panel_world("Schreibtisch \u2014 iCloud", {path: {"file": "Schreibtisch"}})
    reply = save(world, path)
    assert reply["ok"] is True, reply


def test_save_panel_refuses_a_different_folder_without_saving():
    path = "/Users/me/Library/Mobile Documents/com~apple~CloudDocs"
    world = save_panel_world("Documents \u2014 iCloud", {path: {"url": "iCloud Drive"}})
    reply = save(world, path)
    assert reply["ok"] is False
    assert reply["error"] == ("the Save panel is in folder 'Documents \u2014 iCloud', expected "
                              "'com~apple~CloudDocs' or 'iCloud Drive'")
    assert reply["_log"]["pressed"] == []


@pytest.mark.parametrize("where, folder", [
    ("_bounces", "bounces"), ("bounces", "_bounces"), ("\U0001f3b8", "\U0001f941"),
])
def test_save_panel_tells_folders_apart_by_their_leading_symbols(where, folder):
    # Regression: the check mark tolerance meant for chooser menu items also
    # dropped leading symbols from folder names, so these compared equal.
    reply = save(save_panel_world(where), "/tmp/x/" + folder)
    assert reply["ok"] is False
    assert reply["where"] == where
    assert reply["error"] == f"the Save panel is in folder '{where}', expected '{folder}'"
    assert reply["_log"]["pressed"] == []


def slow_save(sheet, directory="/tmp/x/live-test", front="yes"):
    return save(save_panel_world("live-test", sheet=sheet, front=front), directory)


def test_save_panel_fast_sheet_sends_one_shortcut_and_does_not_wait():
    reply = slow_save({})
    assert reply["ok"] is not False, reply
    assert len(reply["_clock"]["g"]) == 1
    assert reply["_clock"]["g"][0] == 0
    assert reply["_clock"]["now"] < 3.5  # the Replace check alone is 2 s; no slow-path wait on top


def test_save_panel_waits_for_a_sheet_that_opens_late_without_resending():
    # 4 s is past the old 3 s limit but inside the first 5 s wait.
    reply = slow_save({"late": 4.0})
    assert reply["ok"] is not False, reply
    assert len(reply["_clock"]["g"]) == 1
    assert reply["where"] == "live-test"
    assert "/tmp/x/live-test" in reply["_log"]["typed"]


def test_save_panel_resends_the_shortcut_once_when_the_first_is_swallowed():
    reply = slow_save({"ignore": 1})
    assert reply["ok"] is not False, reply
    g = reply["_clock"]["g"]
    assert len(g) == 2 and 4.9 < g[1] - g[0] < 5.2
    assert "/tmp/x/live-test" in reply["_log"]["typed"]


def test_save_panel_gives_up_safely_when_the_sheet_never_opens():
    reply = slow_save({"never": True})
    assert reply["ok"] is False
    assert reply["error"] == "the Go to Folder sheet did not open in the Save panel"
    assert len(reply["_clock"]["g"]) == 2  # one re-send, no more
    assert 9.5 < reply["_clock"]["now"] <= 10.5  # bounded at about 10 s
    # nothing typed anywhere and nothing pressed: no Return, no Save
    assert reply["_log"]["keys"] == ["g", "g"]
    assert reply["_log"]["pressed"] == []
    assert reply["_log"]["typed"] == ["song"]  # only the file name, never the folder path


def test_save_panel_skips_the_resend_when_the_panel_is_gone():
    reply = slow_save({"never": True, "closes": 2.0})  # gone during the first wait
    assert reply["ok"] is False
    assert ["Save", "AXDialog"] not in reply["_windows"]
    assert len(reply["_clock"]["g"]) == 1
    assert reply["_log"]["keys"] == ["g"]
    assert reply["_log"]["pressed"] == []
    assert reply["_log"]["typed"] == ["song"]  # only the file name, never the folder path


def test_save_panel_reactivates_live_before_sending_when_it_lost_the_front():
    # Observed on Live 12.4.16: the Save panel was open but Live was no longer
    # frontmost, so Cmd+Shift+G went to another app and no sheet ever opened.
    reply = slow_save({}, front="lost")
    assert reply["ok"] is not False, reply
    assert reply["_log"]["focused"] >= 1
    assert reply["_log"]["activated"] == 0
    assert len(reply["_clock"]["g"]) == 1
    assert reply["_log"]["stray"] == []
    assert "/tmp/x/live-test" in reply["_log"]["typed"]


def test_save_panel_does_not_send_shortcut_when_panel_closes_during_focus_recovery():
    reply = slow_save({"closes": 0.05}, front="lost")
    assert reply["ok"] is False
    assert reply["error"] == "the Save panel was no longer open, so Cmd+Shift+G was not sent"
    assert reply["_log"]["keys"] == []
    assert reply["_log"]["stray"] == []
    assert reply["_log"]["typed"] == ["song"]


def test_save_panel_sends_nothing_while_live_cannot_get_the_front():
    reply = slow_save({}, front="never")
    assert reply["ok"] is False
    assert reply["error"] == "Ableton Live was not frontmost, so Cmd+Shift+G was not sent"
    assert reply["_log"]["keys"] == [] and reply["_log"]["stray"] == []  # no keystroke reached another app
    assert reply["_log"]["typed"] == ["song"]
    assert reply["_log"]["focused"] >= 2
    assert reply["_log"]["activated"] == 0


@pytest.mark.parametrize(("sheet", "front", "keys", "sent"), [
    ({}, "quit", "Cmd+Shift+G", []),
    ({"no_field": True, "front": "quit"}, "yes", "the folder path", ["g"]),
    ({"front": "quit"}, "yes", "Return", ["g"]),
])
@pytest.mark.parametrize("quit_mode", ["quit", "quit_during_focus"])
def test_save_panel_does_not_relaunch_live_during_focus_recovery(
    sheet, front, keys, sent, quit_mode,
):
    if front == "quit":
        front = quit_mode
    else:
        sheet = dict(sheet, front=quit_mode)
    reply = slow_save(sheet, front=front)
    assert reply["ok"] is False
    assert reply["error"] == f"Ableton Live is not running, so {keys} was not sent"
    assert reply["_log"]["keys"] == sent
    assert reply["_log"]["activated"] == 0
    assert reply["_log"]["relaunched"] == 0
    assert reply["_log"]["stale_focus"] == 1


def test_save_panel_types_the_path_into_a_sheet_that_has_no_field_to_set():
    reply = slow_save({"no_field": True})
    assert reply["ok"] is True, reply
    assert reply["_log"]["keys"] == ["g", "/tmp/x/live-test", 36]
    assert reply["_log"]["pressed"] == ["Save"]


def test_save_panel_does_not_type_the_path_while_live_is_not_frontmost():
    reply = slow_save({"no_field": True, "front": "never"})
    assert reply["ok"] is False
    assert reply["error"] == "Ableton Live was not frontmost, so the folder path was not sent"
    assert reply["_log"]["keys"] == ["g"]
    assert reply["_log"]["stray"] == []
    assert reply["_log"]["pressed"] == []


def test_save_panel_waits_for_sheet_keyboard_focus_before_typing_path():
    reply = slow_save({"no_field": True, "front": "settling"})
    assert reply["ok"] is True, reply
    assert reply["_log"]["keys"] == ["g", "/tmp/x/live-test", 36]
    assert reply["_log"]["stray"] == []
    assert reply["_log"]["pressed"] == ["Save"]


def test_save_panel_does_not_type_path_when_sheet_closes_during_focus_recovery():
    reply = slow_save({"no_field": True, "front": "lost", "dismisses": 0.15})
    assert reply["ok"] is False
    assert reply["error"] == (
        "the Go to Folder sheet was no longer open, so the folder path was not sent"
    )
    assert reply["_log"]["keys"] == ["g"]
    assert reply["_log"]["stray"] == []
    assert reply["_log"]["pressed"] == []


def test_save_panel_does_not_send_return_while_live_is_not_frontmost():
    reply = slow_save({"front": "never"})
    assert reply["ok"] is False
    assert reply["error"] == "Ableton Live was not frontmost, so Return was not sent"
    assert reply["_log"]["keys"] == ["g"]
    assert reply["_log"]["stray"] == []
    assert reply["_log"]["pressed"] == []


def test_save_panel_does_not_send_return_when_sheet_closes_during_focus_recovery():
    reply = slow_save({"front": "lost", "dismisses": 0.45})
    assert reply["ok"] is False
    assert reply["error"] == "the Go to Folder sheet was no longer open, so Return was not sent"
    assert reply["_log"]["keys"] == ["g"]
    assert reply["_log"]["stray"] == []
    assert reply["_log"]["pressed"] == []


@pytest.mark.parametrize("no_field, keys", [
    (True, ["g", "/tmp/x/live-test", 36]), (False, ["g", 36]),
])
def test_save_panel_reactivates_live_before_the_path_and_return_keystrokes(no_field, keys):
    reply = slow_save({"no_field": no_field, "front": "lost"})
    assert reply["ok"] is True, reply
    assert reply["_log"]["focused"] >= 1
    assert reply["_log"]["activated"] == 0
    assert reply["_log"]["keys"] == keys
    assert reply["_log"]["stray"] == []
    assert reply["_log"]["pressed"] == ["Save"]


def cancel_world(sheet=None, cancel_works=True):
    """The Save panel, maybe with a sheet left on it, and Live's main window."""
    panel = {"role": "AXWindow", "subrole": "AXDialog", "name": "Save", "kids": [
        {"role": "AXTextField", "description": "Save As:", "value": "song", "writable": True},
        {"role": "AXButton", "description": "Cancel", "press": "closeWindow" if cancel_works else None},
        {"role": "AXButton", "description": "Save"},
    ]}
    if sheet:
        panel["open_sheet"] = sheet
    return {"windows": [panel, {"role": "AXWindow", "subrole": "AXStandardWindow", "name": "Untitled"}]}


def test_cancel_presses_cancel_and_waits_for_the_window_to_close():
    reply = drive("cancel", cancel_world(), window="Save")
    assert reply["ok"] is True, reply
    assert (reply["was_open"], reply["closed"]) == (True, True)
    assert reply["_log"]["pressed"] == ["Cancel"]
    assert reply["_windows"] == [["Untitled", "AXStandardWindow"]]


def test_cancel_of_a_window_that_is_not_open_does_nothing():
    reply = drive("cancel", cancel_world(), window="Export Audio/Video")
    assert reply["ok"] is True
    assert (reply["was_open"], reply["closed"]) == (False, True)
    assert reply["_log"]["pressed"] == [] and reply["_log"]["keys"] == []


def test_cancel_dismisses_a_go_to_folder_sheet_with_esc_first():
    sheet = {"role": "AXSheet", "kids": [{"role": "AXTextField", "description": "path"}]}
    reply = drive("cancel", cancel_world(sheet=sheet), window="Save")
    assert reply["ok"] is True, reply
    assert reply["_log"]["keys"] == [53]
    assert reply["_log"]["pressed"] == ["Cancel"]
    assert reply["_windows"] == [["Untitled", "AXStandardWindow"]]


def test_cancel_uses_a_sheets_own_cancel_button():
    sheet = {"role": "AXSheet", "kids": [
        {"role": "AXButton", "description": "Cancel", "press": "closeSheet"},
        {"role": "AXButton", "description": "Replace"}]}
    reply = drive("cancel", cancel_world(sheet=sheet), window="Save")
    assert reply["ok"] is True, reply
    assert reply["_log"]["keys"] == []
    assert reply["_log"]["pressed"] == ["Cancel", "Cancel"]
    assert reply["_windows"] == [["Untitled", "AXStandardWindow"]]


def test_cancel_does_not_send_esc_while_live_cannot_get_the_front():
    sheet = {"role": "AXSheet", "kids": []}
    world = cancel_world(sheet=sheet)
    world["front"] = "never"
    reply = drive("cancel", world, window="Save")
    assert reply["ok"] is False
    assert "was not frontmost" in reply["error"]
    assert reply["_log"]["keys"] == [] and reply["_log"]["stray"] == []


def test_cancel_reports_a_window_that_stays_open():
    reply = drive("cancel", cancel_world(cancel_works=False), window="Save")
    assert reply["ok"] is False
    assert (reply["was_open"], reply["closed"]) == (True, False)
    assert reply["error"] == "'Save' was still open after pressing Cancel"


def nested_save_panel_world(where="live-test"):
    """The Save panel as Live 12.4.16 shows it (2026-11-23): controls under a
    split group, beside a scroll area holding the folder's file list, which
    in a busy folder is thousands of elements."""
    files = [{"role": "AXRow", "kids": [{"role": "AXCell", "kids": [
        {"role": "AXStaticText", "value": f"take{i}.wav"}]}]} for i in range(50)]
    # A decoy: a file-list element labelled like the panel's buttons.
    files.append({"role": "AXButton", "description": "Cancel", "press": "closeWindow"})
    return {"sheet": {}, "front": "yes", "windows": [
        {"role": "AXWindow", "subrole": "AXDialog", "name": "Save", "kids": [
            {"role": "AXSplitGroup", "kids": [
                {"role": "AXScrollArea", "heavy": True, "kids": [
                    {"role": "AXOutline", "heavy": True, "kids": files}]},
                {"role": "AXGroup", "kids": [
                    {"role": "AXTextField", "name": "Save As:", "value": "", "writable": True},
                    {"role": "AXPopUpButton", "title": "Where:", "value": where},
                    {"role": "AXButton", "title": "Cancel", "description": "button",
                     "press": "closeWindow"},
                    {"role": "AXButton", "title": "Save", "description": "button"},
                ]},
            ]},
        ]},
        {"role": "AXWindow", "subrole": "AXStandardWindow", "name": "Untitled"},
    ], "display_names": {}}


def test_save_panel_finds_its_controls_without_walking_the_file_list():
    reply = save(nested_save_panel_world(), "/tmp/x/live-test")
    assert reply["ok"] is True, reply
    assert reply["where"] == "live-test"
    assert reply["_log"]["typed"] == ["song", "/tmp/x/live-test", "song"]
    assert reply["_log"]["pressed"] == ["button"]  # the panel's Save
    assert reply["_log"]["heavy"] == 0


def test_cancel_presses_the_panels_cancel_without_walking_the_file_list():
    reply = drive("cancel", nested_save_panel_world(), window="Save")
    assert reply["ok"] is True, reply
    assert (reply["was_open"], reply["closed"]) == (True, True)
    assert reply["_log"]["pressed"] == ["button"]  # the panel's Cancel, not the decoy
    assert reply["_log"]["heavy"] == 0
    assert reply["_windows"] == [["Untitled", "AXStandardWindow"]]


def test_cancel_sends_esc_when_the_window_has_no_cancel_button_in_reach():
    world = cancel_world()
    panel = world["windows"][0]
    panel["kids"] = [k for k in panel["kids"] if k.get("description") != "Cancel"]
    panel["esc_closes"] = True
    reply = drive("cancel", world, window="Save")
    assert reply["ok"] is True, reply
    assert reply["_log"]["keys"] == [53]
    assert reply["_log"]["pressed"] == []
    assert reply["_windows"] == [["Untitled", "AXStandardWindow"]]


def test_cancel_does_not_send_esc_to_another_app():
    world = cancel_world()
    panel = world["windows"][0]
    panel["kids"] = [k for k in panel["kids"] if k.get("description") != "Cancel"]
    panel["esc_closes"] = True
    world["front"] = "never"
    reply = drive("cancel", world, window="Save")
    assert reply["ok"] is False
    assert "was not frontmost" in reply["error"]
    assert reply["_log"]["keys"] == [] and reply["_log"]["stray"] == []


# ── Render Start / Render Length fields (issue 11) ───────────────


def time_field_world(start=("34", "1", "1"), length=("0", "0", "1"), clamp_start_bar=True):
    """The Export dialog's time sliders as Live 12.4.16 exposes them (observed on the second Mac).

    Each field is three AXSliders described "Start of Rendered Audio File (Bar)" and so on.
    They take AXIncrement / AXDecrement steps; beats and sixteenths wrap, bars stop at their ends.
    AXValue writes and clicks do nothing, and Return would press Export.
    """
    def sliders(prefix, values, first_bar, wrap_max):
        return [{"role": "AXSlider", "description": f"{prefix} ({unit})", "value": value,
                 "slider": {"min": lo, "max": hi, "wrap": unit != "Bar"}}
                for unit, value, (lo, hi) in zip(
                    ("Bar", "Beat", "Sixteenths"), values,
                    [(first_bar, 9999), (first_bar if first_bar else 0, wrap_max),
                     (first_bar if first_bar else 0, wrap_max)])]
    kids = (sliders("Start of Rendered Audio File", start, 1, 4)
            + [{"role": "AXStaticText", "name": ".", "value": "."}]
            + sliders("Length of Rendered Audio File", length, 0, 3)
            + [{"role": "AXButton", "description": "Export"}, {"role": "AXButton", "description": "Cancel"}])
    return {"windows": [
        {"role": "AXWindow", "subrole": "AXDialog", "name": "Export Audio/Video", "kids": kids},
        {"role": "AXWindow", "subrole": "AXStandardWindow", "name": "Untitled"},
    ]}


def test_get_time_field_reads_the_three_parts_as_numbers():
    world = time_field_world()
    start = drive("get_time_field", world, window="Export Audio/Video", label="Render Start")
    length = drive("get_time_field", world, window="Export Audio/Video", label="Render Length")
    assert start["fields"] == [34, 1, 1] and length["fields"] == [0, 0, 1]


def test_set_time_field_steps_each_part_to_its_value_without_a_key_or_a_click():
    reply = drive("set_time_field", time_field_world(), window="Export Audio/Video",
                  label="Render Length", fields=[14, 0, 0])
    assert reply["ok"] is True, reply
    assert reply["before"] == [0, 0, 1] and reply["after"] == [14, 0, 0]
    log = reply["_log"]
    # 14 bar steps up, then one step down for the sixteenths (1 -> 0)
    assert log["nudges"] == 14 + 1
    # Return in the dialog presses Export, so no key is ever sent
    assert log["keys"] == [] and log["stray"] == [] and log["pressed"] == []


def test_set_time_field_goes_down_for_a_smaller_value():
    reply = drive("set_time_field", time_field_world(), window="Export Audio/Video",
                  label="Render Start", fields=[1, 1, 1])
    assert reply["before"] == [34, 1, 1] and reply["after"] == [1, 1, 1]
    assert reply["_log"]["nudges"] == 33


def test_set_time_field_wraps_beats_the_short_way_round():
    reply = drive("set_time_field", time_field_world(), window="Export Audio/Video",
                  label="Render Start", fields=[34, 4, 3])
    assert reply["after"] == [34, 4, 3]


def test_set_time_field_leaves_parts_that_already_match_alone():
    reply = drive("set_time_field", time_field_world(), window="Export Audio/Video",
                  label="Render Start", fields=[34, 1, 1])
    assert reply["before"] == reply["after"] == [34, 1, 1]
    assert reply["_log"].get("nudges", 0) == 0


def test_a_value_the_slider_cannot_reach_is_an_error_not_a_loop():
    # bars stop at their end: asking for bar 0 on the start (minimum 1) never changes
    reply = drive("set_time_field", time_field_world(), window="Export Audio/Video",
                  label="Render Start", fields=[0, 1, 1])
    assert reply["ok"] is False
    assert "Render Start bar stays at 1 and cannot be set to 0" in reply["error"]


def test_a_field_with_other_than_three_parts_is_refused():
    reply = drive("set_time_field", time_field_world(), window="Export Audio/Video",
                  label="Render Length", fields=[14, 0])
    assert reply["ok"] is False and "three parts" in reply["error"]


def test_a_label_that_is_not_a_time_field_is_refused():
    reply = drive("get_time_field", time_field_world(), window="Export Audio/Video", label="Sample Rate")
    assert reply["ok"] is False and "is not a time field" in reply["error"]


def test_fields_that_are_not_in_the_dialog_say_so_and_point_at_the_dump_op():
    reply = drive("get_time_field", export_dialog_world(), window="Export Audio/Video",
                  label="Render Start")
    assert reply["ok"] is False
    assert "three parts of 'Render Start' were not found" in reply["error"] and "dump" in reply["error"]


def test_dump_lists_every_element_of_a_window_with_its_labels_and_values():
    reply = drive("dump", time_field_world(), window="Export Audio/Video")
    assert reply["ok"] is True
    rows = {(e["role"], e["description"]): e["value"] for e in reply["elements"]}
    assert rows[("AXSlider", "Start of Rendered Audio File (Bar)")] == "34"
    assert rows[("AXSlider", "Length of Rendered Audio File (Sixteenths)")] == "1"
    assert ("AXButton", "Cancel") in rows
