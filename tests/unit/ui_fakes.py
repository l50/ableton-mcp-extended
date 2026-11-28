"""A stateful stand-in for Live's UI, driven through the JXA driver's ops.

``FakeUI`` answers the ``ui_state`` op from fields a test sets (Key Map mode,
dialogs, transport, time selection, focus) and reacts to the menu and
button ops the way Live does, so clear_selection, the export guard and the
save tools can be checked end to end without osascript.
"""

import os


def raw_window(name, subrole="AXDialog", modal=False, texts=(), buttons=(), sheets=0, **extra):
    return {"name": name, "subrole": subrole, "modal": modal, "sheets": sheets,
            "texts": list(texts), "buttons": list(buttons), **extra}


class FakeUI:
    def __init__(self, **state):
        self.calls = []
        self.running = state.pop("running", True)
        self.title = state.pop("title", "Song")
        self.document = state.pop("document", None)
        self.key_map = state.pop("key_map", False)
        self.midi_map = state.pop("midi_map", False)
        self.computer_keyboard = state.pop("computer_keyboard", False)
        self.playing = state.pop("playing", False)
        self.recording = state.pop("recording", False)
        self.session_record = state.pop("session_record", False)
        self.loop = state.pop("loop", False)
        self.selection = state.pop("selection", False)
        self.items_selected = state.pop("items_selected", False)
        self.focus_group = state.pop("focus_group", "Arrangement")
        self.dialogs = state.pop("dialogs", [])
        self.main_sheets = state.pop("main_sheets", [])
        self.unreadable = state.pop("unreadable", [])
        self.no_main = state.pop("no_main", False)
        self.help_panel = state.pop("help_panel", "Learn View")
        self.edit_unreadable = state.pop("edit_unreadable", False)
        self.switch_values = state.pop("switch_values", {})
        # behaviours that make an action go wrong
        self.marker_starts_playback = state.pop("marker_starts_playback", False)
        self.marker_keeps_selection = state.pop("marker_keeps_selection", False)
        self.stop_fails = state.pop("stop_fails", False)
        self.menu_errors = state.pop("menu_errors", {})
        self.on_save = state.pop("on_save", None)       # called on File > Save Live Set
        self.save_panel_error = state.pop("save_panel_error", None)
        self.save_panel_never_opens = state.pop("save_panel_never_opens", False)
        self.project_folders = state.pop("project_folders", False)
        assert not state, f"unknown FakeUI state: {sorted(state)}"

    # JXADriver interface
    def call(self, op, **params):
        self.calls.append(dict(params, op=op))
        return getattr(self, "op_" + op)(**params)

    def ops(self):
        return [c["op"] for c in self.calls]

    def clicks(self):
        """Everything that could change Live: every call except the read-only ones."""
        return [c for c in self.calls if c["op"] not in ("ui_state", "status", "texts", "controls")]

    # read ops
    def op_status(self):
        if not self.running:
            return {"ok": True, "running": False, "frontmost": False, "windows": []}
        windows = [{k: w[k] for k in ("name", "subrole", "modal", "sheets")} for w in self.dialogs]
        windows.append({"name": self.title, "subrole": "AXStandardWindow", "modal": False,
                        "sheets": len(self.main_sheets)})
        return {"ok": True, "running": True, "frontmost": True, "windows": windows}

    def op_ui_state(self):
        if not self.running:
            return {"ok": True, "running": False}
        windows = [dict(w) for w in self.dialogs]
        main = {"name": self.title, "subrole": "AXStandardWindow", "modal": False,
                "sheets": len(self.main_sheets), "main": not self.no_main}
        if self.main_sheets:
            main["sheet_texts"] = list(self.main_sheets)
            main["sheet_buttons"] = ["Cancel"]
        windows.append(main)
        switches = {"Key Map Mode": self.key_map, "MIDI Map Mode": self.midi_map,
                    "Computer MIDI Keyboard": self.computer_keyboard, "Play": self.playing,
                    "Arrangement Record": self.recording, "Session Record": self.session_record,
                    "MIDI Arrangement Overdub": False, "Arrangement Loop": self.loop,
                    "Punch-In": False, "Punch-Out": False}
        switches.update(self.switch_values)
        enabled = self.selection and self.focus_group == "Arrangement"
        edit = None if self.edit_unreadable else {
            "Cut Time": enabled, "Copy Time": enabled, "Duplicate Time": enabled,
            "Delete Time": enabled, "Delete": self.items_selected}
        return {
            "ok": True, "running": True, "frontmost": True, "windows": windows,
            "main": None if self.no_main else {
                "name": self.title, "document": self.document, "switches": switches,
                "groups": ["Control", "Arrangement", "Mixer", "Device Detail", self.help_panel]},
            "focus": [{"role": "AXStaticText", "description": "Arrangement Editor", "name": "Timeline"},
                      {"role": "AXGroup", "description": "group", "name": self.focus_group},
                      {"role": "AXGroup", "description": "group", "name": ""},
                      {"role": "AXWindow", "description": "standard window", "name": self.title}],
            "edit": edit,
            "unreadable": list(self.unreadable),
        }

    # actions
    def op_menu(self, path):
        error = self.menu_errors.get(tuple(path))
        if error:
            return {"ok": False, "error": error}
        menu, item = path[0], path[1]
        if menu == "Navigate":
            self.focus_group = "Arrangement"
            return {"ok": True, "clicked": item}
        if menu == "Playback":
            if not self.marker_keeps_selection:
                self.selection = False
            if self.marker_starts_playback:
                self.playing = True
            return {"ok": True, "clicked": item}
        if menu == "Edit":
            if self.focus_group != "Arrangement":
                return {"ok": False, "disabled": True, "error": f"menu item '{menu} > {item}' is disabled"}
            self.selection = True
            return {"ok": True, "clicked": item}
        if menu == "File" and item.startswith("Save Live Set As"):
            if not self.save_panel_never_opens:
                self.dialogs.insert(0, raw_window("Save", modal=True))
            return {"ok": True, "clicked": item}
        if menu == "File" and item.startswith("Save Live Set"):
            if self.on_save:
                self.on_save()
            return {"ok": True, "clicked": item}
        return {"ok": True, "clicked": item}

    def op_stop_transport(self):
        if self.stop_fails:
            return {"ok": False, "error": "the Stop button was not found in the Control bar"}
        self.playing = False
        return {"ok": True, "pressed": "Stop"}

    def op_save_panel(self, window, directory, filename, folder_name, replace):
        if self.save_panel_error:
            return {"ok": False, "error": self.save_panel_error}
        target = os.path.join(directory, filename + ".als")
        if self.project_folders:
            # Live puts a Set it never saved in a "<name> Project" folder it makes
            os.makedirs(os.path.join(directory, filename + " Project"), exist_ok=True)
            target = os.path.join(directory, filename + " Project", filename + ".als")
        with open(target, "wb") as f:
            f.write(b"<Ableton/>")
        self.document = "file://" + target
        self.title = filename
        self.dialogs = [d for d in self.dialogs if d["name"] != window]
        return {"ok": True, "where": folder_name, "replaced": False}

    def op_cancel(self, window):
        was_open = any(d["name"] == window for d in self.dialogs)
        self.dialogs = [d for d in self.dialogs if d["name"] != window]
        return {"ok": True, "was_open": was_open, "closed": True}


def safe_raw_state(**over):
    return FakeUI(**over).op_ui_state()
