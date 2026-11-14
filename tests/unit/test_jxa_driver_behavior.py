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
var LOG = {keys: [], pressed: [], clicked: []};
var WORLD = JSON.parse(process.argv[2]);
function delay() {}
function mk(spec) {
  var o = {spec: spec};
  o.role = function () { return spec.role; };
  o.subrole = function () { return spec.subrole || null; };
  o.description = function () { return spec.description || ''; };
  o.title = function () { return spec.title || ''; };
  o.name = function () { return spec.name || ''; };
  // Live ignores AXValue writes, so assigning el.value does nothing.
  var valueFn = function () { return spec.value; };
  // Text fields (spec.writable) do take writes.
  Object.defineProperty(o, 'value', {get: function () { return valueFn; },
    set: function (v) { if (spec.writable) spec.value = v; }});
  o.enabled = function () { return spec.enabled !== false; };
  o.sheets = [];
  o.attributes = {byName: function () { return {value: function () { return !!spec.modal; }}; }};
  o.entireContents = function () {
    var out = [];
    (function walk(list) { list.forEach(function (c) { out.push(c); walk(c.kids || []); }); })(o.kids || []);
    return out;
  };
  o.actions = {byName: function (n) { return {perform: function () {
    if (n !== 'AXPress') throw new Error('unsupported action ' + n);
    LOG.pressed.push(spec.description || spec.name);
    if (spec.press) ACTIONS[spec.press](o);
  }}; }};
  o.kids = (spec.kids || []).map(mk);
  return o;
}
var windows = WORLD.windows.map(mk);
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
  toggle: function (sw) { sw.spec.value = sw.spec.value === 'On' ? 'Off' : 'On'; }
};
var FAKE_PROC = {
  windows: function () { return windows; },
  frontmost: function () { return true; },
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
  if (name !== 'System Events') return {activate: function () {}};
  return {
    processes: {whose: function () { return [FAKE_PROC]; }},
    keyCode: function (code) {
      LOG.keys.push(code);
      if (code === 36) windows.forEach(function (w) { w.sheets = []; });  // Return: go
      if (code === 53) {  // Esc: close an open menu window only
        windows = windows.filter(function (w) {
          return !w.entireContents().some(function (c) { return c.role() === 'AXMenuItem'; });
        });
      }
    },
    keystroke: function (k, opts) {
      LOG.keys.push(k);
      // Cmd+Shift+G opens the Go to Folder sheet on the Save panel.
      if (k === 'g' && opts && (opts.using || []).indexOf('command down') >= 0) {
        windows.forEach(function (w) {
          if (w.name() === 'Save') w.sheets = [mk({role: 'AXSheet', kids: [
            {role: 'AXTextField', description: 'path', writable: true}]})];
        });
      }
    },
    click: function (el) { el.actions.byName('AXPress').perform(); }
  };
}
"""

_EPILOGUE = r"""
var reply = JSON.parse(run([process.argv[1]]));
reply._log = LOG;
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


def save_panel_world(where, display_names=None):
    """Live's Save panel: a writable Save As field and a read-only Where pop-up."""
    return {"windows": [
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
                              "'com~apple~CloudDocs' or 'com~apple~CloudDocs' or 'iCloud Drive'")
    assert reply["_log"]["pressed"] == []
