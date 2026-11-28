"""Run the UI-state JXA ops (live_ui_state.UI_STATE_JXA) against a fake System Events.

osascript cannot run in unit tests, but the driver is plain JavaScript, so
these tests execute the real script under Node with ``Application('System
Events')`` replaced by a fake of Live 12.4's accessibility tree, laid out as
observed on a running Live: the set window holds one unnamed group whose
children are the Control bar (with Modes, Playback and Record groups of
checkboxes), Browser, Arrangement, Mixer and so on, and a dialog is its own
AXDialog window.

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

_PRELUDE = r"""
var WORLD = JSON.parse(process.argv[2]);
var LOG = {pressed: [], clicked: []};
function delay() {}
var BY_ID = {};
function build(spec, parent) {
  var o = {spec: spec, parent: parent};
  if (spec.id) BY_ID[spec.id] = o;
  o.role = function () { return spec.role; };
  o.subrole = function () { return spec.subrole || null; };
  o.description = function () { return spec.description || ''; };
  o.title = function () { return spec.title || ''; };
  o.name = function () { return spec.name || ''; };
  o.value = function () { return spec.value; };
  o.enabled = function () { return spec.enabled !== false; };
  o.focused = function () { return false; };
  o.sheets = (spec.sheets || []).map(function (s) { return build(s, o); });
  o.kids = (spec.kids || []).map(function (k) { return build(k, o); });
  o.uiElements = function () {
    if (spec.unreadable) throw new Error("Can't get object.");
    return o.kids;
  };
  o.entireContents = function () {
    var out = [];
    (function walk(list) { list.forEach(function (c) { out.push(c); walk(c.kids); }); })(o.kids);
    return out;
  };
  o.attributes = {byName: function (n) { return {value: function () {
    if (n === 'AXModal') return !!spec.modal;
    if (n === 'AXDocument') return spec.document === undefined ? null : spec.document;
    if (n === 'AXParent') return o.parent;
    throw new Error("Can't get object.");
  }}; }};
  o.actions = {byName: function () { return {perform: function () {
    LOG.pressed.push(spec.description || spec.name);
    if (spec.onPress === 'toggle') spec.value = spec.value ? 0 : 1;
  }}; }};
  return o;
}
var windows = WORLD.windows.map(function (w) { return build(w, null); });
var RUNNING = WORLD.running !== false;
var FOCUS = WORLD.focus ? BY_ID[WORLD.focus] : null;
var FAKE_PROC = {
  windows: function () {
    if (WORLD.windows_unreadable) throw new Error("Can't get object.");
    return windows;
  },
  frontmost: function () { return WORLD.frontmost !== false; },
  attributes: {byName: function (n) { return {value: function () {
    if (n === 'AXFocusedUIElement') return FOCUS;
    throw new Error("Can't get object.");
  }}; }},
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
function Application(name) {
  return {processes: {whose: function () { return RUNNING ? [FAKE_PROC] : []; }}};
}
"""

_EPILOGUE = r"""
var reply = JSON.parse(run([process.argv[1]]));
reply._log = LOG;
console.log(JSON.stringify(reply));
"""


def checkbox(description, value):
    return {"role": "AXCheckBox", "description": description, "value": value, "onPress": "toggle"}


def group(name, kids=(), role="AXGroup"):
    return {"role": role, "description": "group", "name": name, "kids": list(kids)}


def control_bar(**on):
    """The Control bar with the switches observed on Live 12.4, each off unless named."""
    names = {
        "Playback": ["Follow", "Play"],
        "Record and capture": ["Arrangement Record", "MIDI Arrangement Overdub", "Session Record"],
        "Arrangement looping": ["Punch-In", "Arrangement Loop", "Punch-Out"],
        "Modes": ["Draw Mode", "Computer MIDI Keyboard", "Key Map Mode", "MIDI Map Mode"],
    }
    groups = []
    for title, switches in names.items():
        kids = [checkbox(s, 1 if on.get(s.replace(" ", "_").replace("-", "_")) else 0) for s in switches]
        if title == "Playback":
            kids.append({"role": "AXButton", "description": "Stop"})
        groups.append(group(title, kids))
    return group("Control", groups)


def set_window(title="Song", document=None, control=None, extra_groups=(), **spec):
    top = group("", [control or control_bar(), group("Browser"),
                     group("Arrangement", [{"id": "loop", **group("Loop Brace")}]),
                     group("Mixer", [{"id": "slot", "role": "AXGroup", "name": "Slot",
                                      "description": "Slot"}]),
                     group("Device Detail", [{"id": "device", **group("Device Group")}]),
                     *extra_groups])
    return {"role": "AXWindow", "subrole": "AXStandardWindow", "name": title,
            "document": document, "kids": [top], **spec}


def dialog(name, texts, buttons, **spec):
    kids = [{"role": "AXStaticText", "description": "text", "value": t} for t in texts]
    kids += [{"role": "AXButton", "description": b} for b in buttons]
    return {"role": "AXWindow", "subrole": "AXDialog", "name": name, "kids": kids, **spec}


EDIT_OFF = [{"name": n, "enabled": False} for n in
            ("Cut", "Copy", "Delete", "Cut Time", "Copy Time", "Duplicate Time", "Delete Time")]


def drive(op, world, **params):
    cmd = dict(params, op=op, bundle=le.LIVE_BUNDLE_ID)
    proc = subprocess.run(
        [NODE, "-e", _PRELUDE + le.JXA_DRIVER + _EPILOGUE, "--", json.dumps(cmd), json.dumps(world)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_ui_state_not_running_reports_it():
    reply = drive("ui_state", {"windows": [], "running": False})
    assert reply == {"running": False, "ok": True, "_log": reply["_log"]}


def test_ui_state_reads_mode_and_transport_switches_from_the_control_bar():
    world = {"windows": [set_window(control=control_bar(Key_Map_Mode=True, Play=True))],
             "menus": {"Edit": EDIT_OFF}, "focus": "loop"}
    reply = drive("ui_state", world)
    switches = reply["main"]["switches"]
    assert switches["Key Map Mode"] is True
    assert switches["Play"] is True
    assert switches["MIDI Map Mode"] is False
    assert switches["Arrangement Record"] is False
    assert reply["main"]["groups"][:2] == ["Control", "Browser"]
    # reading changes nothing
    assert reply["_log"]["pressed"] == [] and reply["_log"]["clicked"] == []


def test_ui_state_marks_the_set_window_and_gives_its_document():
    world = {"windows": [set_window(title="Coven", document="file:///Users/me/My%20Set.als")],
             "menus": {"Edit": EDIT_OFF}, "focus": "loop"}
    reply = drive("ui_state", world)
    window = reply["windows"][0]
    assert window["main"] is True and window["name"] == "Coven"
    assert reply["main"]["document"] == "file:///Users/me/My%20Set.als"


def test_ui_state_dialog_windows_carry_their_text_and_buttons():
    save = dialog("", ['Save changes to "Untitled" before closing?'], ["Don’t Save", "Cancel", "Save"],
                  modal=True)
    world = {"windows": [save, {"role": "AXWindow", "subrole": "AXUnknown", "name": ""}, set_window()],
             "menus": {"Edit": EDIT_OFF}, "focus": "loop"}
    reply = drive("ui_state", world)
    first = reply["windows"][0]
    assert first["subrole"] == "AXDialog" and first["modal"] is True
    assert first["texts"] == ['Save changes to "Untitled" before closing?']
    assert first["buttons"] == ["Don’t Save", "Cancel", "Save"]
    assert "main" not in reply["windows"][1]
    assert reply["windows"][2]["main"] is True


def test_ui_state_sheet_on_a_window_is_read():
    sheeted = set_window(sheets=[{"role": "AXSheet", "kids": [
        {"role": "AXStaticText", "value": "Replace it?"}, {"role": "AXButton", "description": "Replace"}]}])
    reply = drive("ui_state", {"windows": [sheeted], "menus": {"Edit": EDIT_OFF}, "focus": "loop"})
    window = reply["windows"][0]
    assert window["sheets"] == 1
    assert window["sheet_texts"] == ["Replace it?"] and window["sheet_buttons"] == ["Replace"]


def test_ui_state_focus_chain_runs_from_the_focused_element_up_to_the_window():
    world = {"windows": [set_window()], "menus": {"Edit": EDIT_OFF}, "focus": "device"}
    reply = drive("ui_state", world)
    chain = reply["focus"]
    assert chain[0]["name"] == "Device Group"
    assert [link["name"] for link in chain][1:3] == ["Device Detail", ""]
    assert chain[-1]["role"] == "AXWindow" and chain[-1]["name"] == "Song"


def test_ui_state_no_focused_element_gives_an_empty_chain():
    reply = drive("ui_state", {"windows": [set_window()], "menus": {"Edit": EDIT_OFF}})
    assert reply["focus"] == []


def test_ui_state_edit_commands_report_enabled_or_not():
    edit = [{"name": "Cut Time", "enabled": True}, {"name": "Delete Time", "enabled": True},
            {"name": "Copy Time", "enabled": False}, {"name": "Delete", "enabled": False},
            {"name": "Other", "enabled": True}]
    reply = drive("ui_state", {"windows": [set_window()], "menus": {"Edit": edit}, "focus": "loop"})
    assert reply["edit"] == {"Cut Time": True, "Delete Time": True, "Copy Time": False,
                             "Delete": False}


def test_ui_state_unreadable_edit_menu_is_null_not_empty():
    reply = drive("ui_state", {"windows": [set_window()], "focus": "loop"})
    assert reply["edit"] is None


def test_ui_state_without_a_control_bar_has_no_main_window():
    world = {"windows": [{"role": "AXWindow", "subrole": "AXStandardWindow", "name": "Odd",
                          "kids": [group("", [group("Browser")])]}],
             "menus": {"Edit": EDIT_OFF}}
    reply = drive("ui_state", world)
    assert reply["main"] is None
    assert "main" not in reply["windows"][0]


def test_ui_state_unreadable_windows_are_listed_as_unreadable():
    reply = drive("ui_state", {"windows": [set_window()], "windows_unreadable": True,
                               "menus": {"Edit": EDIT_OFF}})
    assert reply["unreadable"] == ["windows"] and reply["windows"] == []


def test_stop_transport_presses_the_stop_button_in_the_control_bar():
    reply = drive("stop_transport", {"windows": [set_window()]})
    assert reply["ok"] is True and reply["pressed"] == "Stop"
    assert reply["_log"]["pressed"] == ["Stop"]


def test_stop_transport_without_a_stop_button_fails_instead_of_pretending():
    bar = group("Control", [group("Playback", [checkbox("Play", 1)])])
    reply = drive("stop_transport", {"windows": [set_window(control=bar)]})
    assert reply["ok"] is False and "Stop button" in reply["error"]


def test_stop_transport_without_a_set_window_fails():
    reply = drive("stop_transport", {"windows": []})
    assert reply["ok"] is False and "set window" in reply["error"]
