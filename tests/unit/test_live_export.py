"""Tests for the Export Audio/Video UI automation (MCP_Server/live_export.py).

osascript is never run: the exporter talks to a FakeLive that plays the part
of Live's UI (windows, dialog controls, the Save panel) and writes the output
file when the Save panel is confirmed. The tests check which menu items,
shortcuts and control values get driven for given parameters, and that every
way the export can go wrong comes back as a failure rather than success.

A real export needs a running Live and cannot run here; see
tests/integration/test_live_export_live.py for the guarded manual check.
"""
import json
import os
import subprocess
import sys

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, _ROOT)

from MCP_Server import live_export as le  # noqa: E402

# Data formats exactly as afinfo prints them (macOS 26.4, files made with
# afconvert). 16- and 32-bit WAV get the short form, everything else "N-bit".
WAV_24 = "lpcm (0x0000000C) 24-bit little-endian signed integer"
WAV_16 = "Int16, interleaved"
WAV_32_FLOAT = "Float32, interleaved"
AIFF_16 = "lpcm (0x0000000E) 16-bit big-endian signed integer"
AIFF_24 = "lpcm (0x0000000E) 24-bit big-endian signed integer"
AIFC_32_FLOAT = "lpcm (0x0000000B) 32-bit big-endian float"
FLAC_24 = "flac (0x00000003) from 24-bit source, 4608 frames/packet"
MP3 = ".mp3 (0x00000000) 0 bits/channel"

WAV_AFINFO = f"""File:           song.wav
File type ID:   WAVE
Num Tracks:     1
----
Data format:     2 ch,  48000 Hz, {WAV_24}
                no channel layout.
estimated duration: 12.500000 sec
audio bytes: 3600000
audio packets: 600000
bit rate: 2304000 bits per second
packet size upper bound: 6
maximum packet size: 6
audio data file offset: 4096
optimized
source bit depth: I24
----
"""

STATUS_ERROR = "System Events got an error: AppleEvent timed out. (-1712)"


def afinfo_for(rate, fmt):
    return f"File type ID:   WAVE\nData format:     2 ch,  {rate} Hz, {fmt}\nestimated duration: 3.0 sec\n"


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = 0

    def now(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps += 1
        self.t += seconds


def default_controls():
    return {
        le.RENDERED_TRACK: {"kind": "choice", "value": "Master Bus", "options": ["Main", "All Individual Tracks"]},
        le.ENCODE_PCM: {"kind": "toggle", "value": False},
        le.FILE_TYPE: {"kind": "choice", "value": "AIFF", "options": ["WAV", "AIFF", "FLAC"]},
        le.BIT_DEPTH: {"kind": "choice", "value": "16", "options": ["16", "24", "32"]},
        le.ENCODE_MP3: {"kind": "toggle", "value": True},
        le.SAMPLE_RATE: {"kind": "choice", "value": "44100",
                         "options": [str(r) for r in le.SAMPLE_RATES]},
        le.NORMALIZE: {"kind": "toggle", "value": False},
        le.CREATE_VIDEO: {"kind": "toggle", "value": False, "enabled": False},
        le.EXPORT_BUTTON: {"kind": "button"},
        le.CANCEL_BUTTON: {"kind": "button"},
    }


def fake_window(name, subrole="AXDialog", modal=False, texts=()):
    return {"name": name, "subrole": subrole, "modal": modal, "sheets": 0, "texts": list(texts)}


class FakeLive:
    """A scripted stand-in for Live's UI, driven through JXADriver's ops."""

    def __init__(self, clock, **behaviour):
        self.clock = clock
        self.calls = []
        self.running = True
        self.frontmost = False
        self.arrangement_focused = False
        self.windows = [fake_window("My Song", "AXStandardWindow")]
        self.controls = default_controls()
        self.render_bytes = b"RIFF" + b"\0" * 64
        self.render_polls = 1          # status polls before the file appears
        self.extra_render_files = []   # e.g. a video written beside the audio
        self.b = dict(behaviour)
        self._rendering = None

    # helpers
    def open_window(self, name, subrole="AXDialog", modal=False, texts=None):
        if texts is None:
            texts = self.b.get("window_texts", {}).get(name, [])
        opened = fake_window(name, subrole, modal, texts)
        self.windows.insert(0, opened)
        return opened

    def close_window(self, name):
        self.windows = [w for w in self.windows if w["name"] != name]

    def names(self):
        return [w["name"] for w in self.windows]

    def ops(self):
        return [c["op"] for c in self.calls]

    def driven(self, *ops):
        return [c for c in self.calls if c["op"] in ops]

    # JXADriver interface
    def call(self, op, **params):
        self.calls.append(dict(params, op=op))
        handler = getattr(self, "op_" + op)
        return handler(**params)

    def op_status(self):
        # status_fails_after: the op after which System Events stops answering
        # status (None = from the start), as a busy Live makes it do.
        if "status_fails_after" in self.b:
            after = self.b["status_fails_after"]
            if after is None or after in self.ops():
                return {"ok": False, "error": STATUS_ERROR, "code": -1712}
        if self._rendering is not None:
            self._tick_render()
        if not self.running:
            return {"ok": True, "running": False, "frontmost": False, "windows": []}
        return {"ok": True, "running": True, "frontmost": self.frontmost,
                "windows": [{k: w[k] for k in ("name", "subrole", "modal", "sheets")}
                            for w in self.windows]}

    def op_activate(self):
        self.frontmost = self.b.get("comes_to_front", True)
        return {"ok": True, "frontmost": self.frontmost}

    def op_menu(self, path):
        if path[0] == le.FILE_MENU:
            state = self.b.get("export_menu", "ok")
            if state == "missing":
                return {"ok": False, "missing": True, "error": "menu item 'File > Export Audio/Video' not found"}
            if state == "disabled":
                return {"ok": False, "disabled": True, "error": "menu item 'File > Export Audio/Video...' is disabled"}
            if self.b.get("dialog_appears", True):
                self.open_window(le.EXPORT_DIALOG_TITLE)
            return {"ok": True, "clicked": "Export Audio/Video..."}
        if path[0] == le.NAVIGATE_MENU:
            state = self.b.get("arrangement_focus", "ok")
            if state == "missing":
                return {"ok": False, "missing": True, "error": "menu item 'Navigate > Arrangement View' not found"}
            if state == "disabled":
                return {"ok": False, "disabled": True, "error": "menu item 'Navigate > Arrangement View' is disabled"}
            self.arrangement_focused = True
            return {"ok": True, "clicked": "Arrangement View"}
        # Live only enables Select Loop while the Arrangement has keyboard focus.
        if not self.arrangement_focused or self.b.get("select_loop_disabled"):
            return {"ok": False, "disabled": True, "error": "menu item 'Edit > Select Loop' is disabled"}
        return {"ok": True, "clicked": "Select Loop"}

    def op_keystroke(self, key, modifiers):
        if self.b.get("dialog_appears", True):
            self.open_window(le.EXPORT_DIALOG_TITLE)
        return {"ok": True}

    def _window_or_error(self, window):
        if window not in self.names():
            return {"ok": False, "error": f"Live window '{window}' is not open"}
        return None

    def op_set_toggle(self, window, label, on):
        missing = self._window_or_error(window)
        if missing:
            return missing
        c = self.controls[label]
        before = c["value"]
        enabled = c.get("enabled", True)
        if before == on:
            return {"ok": True, "before": before, "after": before, "enabled": enabled}
        if not enabled:
            return {"ok": False, "before": before, "after": before, "enabled": False,
                    "error": f"'{label}' is disabled in the Export dialog"}
        if label not in self.b.get("stuck_toggles", ()):
            c["value"] = on
        return {"ok": True, "before": before, "after": c["value"], "enabled": True}

    def op_set_choice(self, window, label, values, first=False):
        missing = self._window_or_error(window)
        if missing:
            return missing
        if label in self.b.get("missing_controls", ()):
            return {"ok": False, "error": f"control '{label}' not found in '{window}'"}
        c = self.controls[label]
        before = c["value"]
        if not first and before in values:
            return {"ok": True, "before": before, "after": before}
        # Like the driver, entries are told apart by where they are in the
        # list: its first entry when asked for that, else the first wanted name.
        options = c["options"][:1] if first else c["options"]
        index = next((i for i, o in enumerate(options) if o in values), None)
        if index is None:
            reply = {"ok": True, "before": before, "after": before, "seen": list(options)}
            if first:
                reply.update(ok=False, error=f"the first entry in the '{label}' list is "
                                             f"'{options[0]}', expected " + " or ".join(values))
            return reply
        if label not in self.b.get("stuck_choices", ()):
            c["value"], c["index"] = options[index], index
        return {"ok": True, "before": before, "after": c["value"],
                "seen": options[:index + 1], "picked": options[index]}

    def op_controls(self, window):
        out = []
        for label, c in self.controls.items():
            if c["kind"] == "button":
                out.append({"role": "AXButton", "label": label, "value": "", "enabled": True})
            elif c["kind"] == "toggle":
                out.append({"role": "AXButton", "label": label, "value": "On" if c["value"] else "Off",
                            "enabled": c.get("enabled", True)})
            else:
                out.append({"role": "AXPopUpButton", "label": label, "value": c["value"], "enabled": True})
        return {"ok": True, "controls": out}

    def op_texts(self, window):
        # Like the driver: the text of every window with this name.
        return {"ok": True, "texts": [t for w in self.windows if w["name"] == window
                                      for t in w["texts"]]}

    def op_press(self, window, label):
        missing = self._window_or_error(window)
        if missing:
            return missing
        if label == le.CANCEL_BUTTON:
            self.close_window(window)
            return {"ok": True, "pressed": label}
        if label == le.EXPORT_BUTTON:
            if self.b.get("export_disabled"):
                return {"ok": False, "error": "'Export' button is disabled"}
            if self.b.get("save_panel_appears", True):
                self.open_window(le.SAVE_PANEL_TITLE, modal=True)
            elif self.b.get("error_window"):
                name, texts = self.b["error_window"]
                self.open_window(name, texts=texts)
            return {"ok": True, "pressed": label}
        return {"ok": False, "error": f"unexpected press {label}"}

    def op_save_panel(self, window, directory, filename, folder_name, replace):
        if self.b.get("save_panel_error"):
            return {"ok": False, "error": self.b["save_panel_error"]}
        self.close_window(le.SAVE_PANEL_TITLE)
        if not self.b.get("dialog_lingers"):
            self.close_window(le.EXPORT_DIALOG_TITLE)
        ext = self.b.get("written_ext", ".wav")
        self._rendering = {
            "path": os.path.join(directory, filename + ext),
            "polls": 0,
            "progress": self.open_window("", subrole="AXUnknown"),
            "alerted": False,
        }
        return {"ok": True, "where": folder_name, "replaced": bool(replace)}

    def _tick_render(self):
        r = self._rendering
        r["polls"] += 1
        if self.b.get("quits_during_render"):
            self.running = False
            return
        if self.b.get("render_error"):
            if not r["alerted"]:
                name, texts = self.b["render_error"]
                self.open_window(name, texts=texts)
                r["alerted"] = True
            return
        if r["polls"] >= self.render_polls and not self.b.get("never_writes"):
            if r["polls"] == self.render_polls:
                with open(r["path"], "wb") as f:
                    f.write(self.render_bytes)
                os.utime(r["path"], (self.clock.now(), self.clock.now()))
                for path, data in self.extra_render_files:
                    with open(path, "wb") as f:
                        f.write(data)
                    os.utime(path, (self.clock.now(), self.clock.now()))
            if not self.b.get("window_stays") and r["polls"] >= self.render_polls + 1:
                self.windows = [w for w in self.windows
                                if w is not r["progress"] and w["name"] != le.EXPORT_DIALOG_TITLE]


@pytest.fixture
def clock():
    return FakeClock()


def make(clock, afinfo=WAV_AFINFO, platform="darwin", **behaviour):
    live = FakeLive(clock, **behaviour)
    exporter = le.LiveExporter(
        driver=live,
        clock=le._Clock(now=clock.now, monotonic=clock.monotonic, sleep=clock.sleep),
        afinfo=(lambda path: afinfo),
        platform=platform,
    )
    return live, exporter


def request(tmp_path, name="song.wav", **kw):
    kw.setdefault("sample_rate", 48000)
    kw.setdefault("render_timeout", 30)
    kw.setdefault("dialog_timeout", 3)
    return le.ExportRequest(output_path=str(tmp_path / name), **kw)


# ── validation ───────────────────────────────────────────────────


class TestValidate:
    def test_defaults(self, tmp_path):
        r = le.ExportRequest(output_path=str(tmp_path / "mix.wav")).validate()
        assert (r.format, r.sample_rate, r.bit_depth, r.normalize) == ("wav", 44100, 24, False)
        assert r.directory == str(tmp_path)
        assert r.stem == "mix"
        assert r.output_file == str(tmp_path / "mix.wav")

    @pytest.mark.parametrize("fmt,expected,exts", [
        ("WAV", "wav", [".wav"]),
        (".flac", "flac", [".flac"]),
        ("aif", "aiff", [".aif", ".aiff"]),
        ("aiff", "aiff", [".aif", ".aiff"]),
        ("mp3", "mp3", [".mp3"]),
    ])
    def test_formats(self, tmp_path, fmt, expected, exts):
        r = le.ExportRequest(output_path=str(tmp_path / "mix"), format=fmt).validate()
        assert r.format == expected
        assert r.expected_paths == [str(tmp_path / ("mix" + e)) for e in exts]

    def test_mp3_ignores_bit_depth(self, tmp_path):
        r = le.ExportRequest(output_path=str(tmp_path / "mix.mp3"), format="mp3", bit_depth=7).validate()
        assert r.format == "mp3"

    def test_tilde_is_expanded(self):
        r = le.ExportRequest(output_path="~/mix.wav").validate()
        assert r.directory == os.path.expanduser("~")

    def test_non_audio_extension_stays_in_name(self, tmp_path):
        r = le.ExportRequest(output_path=str(tmp_path / "Song v1.2")).validate()
        assert r.stem == "Song v1.2"
        assert r.output_file == str(tmp_path / "Song v1.2.wav")

    @pytest.mark.parametrize("kwargs,message", [
        ({"format": "ogg"}, "format must be one of wav, aiff, flac, mp3"),
        ({"sample_rate": 12345}, "sample_rate must be one Live offers"),
        ({"sample_rate": "fast"}, "sample_rate must be a number"),
        ({"bit_depth": 8}, "bit_depth must be 16, 24 or 32 (got 8)"),
        ({"bit_depth": "deep"}, "bit_depth must be 16, 24 or 32"),
        ({"render_timeout": 0}, "render_timeout must be greater than 0"),
        ({"dialog_timeout": -1}, "dialog_timeout must be greater than 0"),
    ])
    def test_rejects_bad_options(self, tmp_path, kwargs, message):
        with pytest.raises(ValueError) as excinfo:
            le.ExportRequest(output_path=str(tmp_path / "mix.wav"), **kwargs).validate()
        assert message in str(excinfo.value)

    @pytest.mark.parametrize("path,message", [
        ("", "output_path is required"),
        ("mix.wav", "output_path must be absolute"),
        ("/tmp/folder/", "must name a file, not a folder"),
        ("/tmp/mix.mp3", "extension '.mp3' does not match format 'wav'"),
    ])
    def test_rejects_bad_paths(self, path, message):
        with pytest.raises(ValueError) as excinfo:
            le.ExportRequest(output_path=path).validate()
        assert message in str(excinfo.value)


# ── parameter → dialog mapping ───────────────────────────────────


class TestDialogPlan:
    def test_pcm_plan(self, tmp_path):
        r = request(tmp_path, bit_depth=16, sample_rate=96000, normalize=True).validate()
        assert le.build_dialog_plan(r) == [
            {"op": "set_choice", "label": "Rendered Track Chooser", "values": ["Main", "Master"],
             "first": True},
            {"op": "set_toggle", "label": "Encode PCM", "on": True},
            {"op": "set_choice", "label": "File Type", "values": ["WAV"]},
            {"op": "set_choice", "label": "Recording Bit Depth", "values": ["16"]},
            {"op": "set_toggle", "label": "Encode MP3", "on": False},
            {"op": "set_choice", "label": "Sample Rate", "values": ["96000"]},
            {"op": "set_toggle", "label": "Normalize", "on": True},
            {"op": "set_toggle", "label": "Create Video with Rendered Audio", "on": False,
             "allow_disabled_off": True},
        ]

    @pytest.mark.parametrize("fmt,file_type", [("aiff", "AIFF"), ("flac", "FLAC")])
    def test_file_types(self, tmp_path, fmt, file_type):
        r = request(tmp_path, name="x", format=fmt).validate()
        plan = le.build_dialog_plan(r)
        assert {"op": "set_choice", "label": "File Type", "values": [file_type]} in plan

    def test_mp3_plan_turns_pcm_off(self, tmp_path):
        r = request(tmp_path, name="x.mp3", format="mp3").validate()
        plan = le.build_dialog_plan(r)
        labels = [s["label"] for s in plan]
        assert {"op": "set_toggle", "label": "Encode MP3", "on": True} in plan
        assert {"op": "set_toggle", "label": "Encode PCM", "on": False} in plan
        assert "File Type" not in labels and "Recording Bit Depth" not in labels
        assert labels.index("Encode MP3") < labels.index("Encode PCM")

    @pytest.mark.parametrize("fmt", le.FORMATS)
    def test_only_rendered_track_is_taken_by_position(self, tmp_path, fmt):
        r = request(tmp_path, name="x", format=fmt).validate()
        by_position = [s["label"] for s in le.build_dialog_plan(r) if s.get("first")]
        assert by_position == ["Rendered Track Chooser"]

    def test_video_plan(self, tmp_path):
        r = request(tmp_path, create_video=True).validate()
        assert le.build_dialog_plan(r)[-1]["on"] is True


# ── end to end against the fake UI ───────────────────────────────


class TestExportSuccess:
    def test_wav_export_drives_menu_dialog_and_save_panel(self, tmp_path, clock):
        live, exporter = make(clock)
        result = exporter.run(request(tmp_path, normalize=True))

        assert result["ok"] is True, result
        assert result["status"] == "success"
        assert result["output_path"] == str(tmp_path / "song.wav")
        assert result["output_files"] == [str(tmp_path / "song.wav")]
        assert os.path.getsize(result["output_path"]) > 0
        assert result["file_info"] == {
            "file_type": "WAVE", "channels": 2, "sample_rate": 48000,
            "data_format": WAV_24, "bit_depth": 24, "duration_seconds": 12.5,
        }
        assert result["warnings"] == []
        # No expected length was given, so the range is reported, not verified.
        assert result["rendered_seconds"] == 12.5
        assert result["expected_seconds"] is None
        assert result["length_verified"] is False

        menu = live.driven("menu")
        assert [m["path"] for m in menu] == [["File", "Export Audio/Video"]]
        assert "keystroke" not in live.ops()
        assert live.ops().index("activate") < live.ops().index("menu")

        settings = [(c["op"], c["label"], c.get("on", c.get("values")))
                    for c in live.driven("set_toggle", "set_choice")]
        assert settings == [
            ("set_choice", "Rendered Track Chooser", ["Main", "Master"]),
            ("set_toggle", "Encode PCM", True),
            ("set_choice", "File Type", ["WAV"]),
            ("set_choice", "Recording Bit Depth", ["24"]),
            ("set_toggle", "Encode MP3", False),
            ("set_choice", "Sample Rate", ["48000"]),
            ("set_toggle", "Normalize", True),
            ("set_toggle", "Create Video with Rendered Audio", False),
        ]
        assert all(c["window"] == "Export Audio/Video" for c in live.driven("set_toggle", "set_choice"))
        assert live.driven("press")[-1]["label"] == "Export"
        save = live.driven("save_panel")[0]
        assert save == {"op": "save_panel", "window": "Save", "directory": str(tmp_path),
                        "filename": "song", "folder_name": tmp_path.name, "replace": False}

        assert result["dialog_settings"]["Sample Rate"] == "48000"
        assert result["dialog_settings"]["Encode PCM"] == "On"
        assert result["dialog_settings"]["Encode MP3"] == "Off"
        assert result["dialog_settings"]["Rendered Track Chooser"] == "Main"
        # steps record what changed, e.g. File Type AIFF -> WAV
        file_type = next(s for s in result["steps"] if s.get("label") == "File Type")
        assert (file_type["before"], file_type["after"]) == ("AIFF", "WAV")
        assert all(s["op"] not in ("status", "texts", "controls") for s in result["steps"])

    def test_mp3_export(self, tmp_path, clock):
        live, exporter = make(clock, afinfo=afinfo_for(48000, MP3), written_ext=".mp3")
        result = exporter.run(request(tmp_path, name="song.mp3", format="mp3"))
        assert result["ok"] is True, result
        assert result["bit_depth"] is None
        assert result["warnings"] == []
        assert live.controls[le.ENCODE_MP3]["value"] is True
        assert live.controls[le.ENCODE_PCM]["value"] is False

    def test_aiff_accepts_aif_extension(self, tmp_path, clock):
        live, exporter = make(clock, afinfo=afinfo_for(48000, AIFF_24), written_ext=".aif")
        result = exporter.run(request(tmp_path, name="song", format="aiff"))
        assert result["ok"] is True, result
        assert result["output_path"] == str(tmp_path / "song.aif")

    def test_loop_range_focuses_arrangement_then_selects_loop_then_opens_dialog(self, tmp_path, clock):
        # The fake, like Live, keeps Select Loop disabled until the Arrangement
        # has keyboard focus, so the export only works in this order.
        live, exporter = make(clock)
        result = exporter.run(request(tmp_path, select_loop=True))
        assert result["ok"] is True, result
        assert [m["path"] for m in live.driven("menu")] == [
            ["Navigate", "Arrangement View"], ["Edit", "Select Loop"], ["File", "Export Audio/Video"],
        ]
        assert [s.get("clicked") for s in result["steps"] if s["op"] == "menu"] == [
            "Arrangement View", "Select Loop", "Export Audio/Video...",
        ]

    @pytest.mark.parametrize("track_name", ["Master", "Main"])
    def test_rendered_track_ends_on_the_top_entry_not_a_track_of_that_name(self, tmp_path, clock, track_name):
        # Regression: a track named like Live's bus was selected, the chooser
        # read a wanted name, and that one track was rendered as the Main mix.
        live, exporter = make(clock)
        options = ["Main", "All Individual Tracks", "Selected Tracks Only", "1 Audio", track_name]
        live.controls[le.RENDERED_TRACK] = {"kind": "choice", "value": track_name, "index": 4,
                                            "options": options}
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True, result
        assert live.controls[le.RENDERED_TRACK]["index"] == 0
        assert result["dialog_settings"]["Rendered Track Chooser"] == "Main"
        chosen = {c["label"]: c.get("first", False) for c in live.driven("set_choice")}
        assert chosen == {"Rendered Track Chooser": True, "File Type": False,
                          "Recording Bit Depth": False, "Sample Rate": False}

    def test_other_choosers_already_on_the_wanted_value_are_left_alone(self, tmp_path, clock):
        live, exporter = make(clock)
        live.controls[le.SAMPLE_RATE]["value"] = "48000"
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True, result
        assert "index" not in live.controls[le.SAMPLE_RATE]

    def test_missing_menu_item_falls_back_to_shortcut(self, tmp_path, clock):
        live, exporter = make(clock, export_menu="missing")
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True, result
        assert live.driven("keystroke") == [
            {"op": "keystroke", "key": "r", "modifiers": ["command down", "shift down"]},
        ]

    def test_overwrite_replaces_existing_file(self, tmp_path, clock):
        target = tmp_path / "song.wav"
        target.write_bytes(b"old")
        os.utime(target, (1.0, 1.0))
        live, exporter = make(clock)
        result = exporter.run(request(tmp_path, overwrite=True))
        assert result["ok"] is True, result
        assert live.driven("save_panel")[0]["replace"] is True
        assert target.read_bytes() != b"old"

    def test_video_export_finds_video_file(self, tmp_path, clock):
        live, exporter = make(clock)
        live.controls[le.CREATE_VIDEO]["enabled"] = True
        live.extra_render_files = [(str(tmp_path / "song.mp4"), b"\0" * 32)]
        (tmp_path / "old.mov").write_bytes(b"\0")
        os.utime(tmp_path / "old.mov", (1.0, 1.0))
        (tmp_path / "notes.txt").write_text("x")
        result = exporter.run(request(tmp_path, create_video=True))
        assert result["ok"] is True, result
        assert result["video_files"] == [str(tmp_path / "song.mp4")]
        assert result["output_files"] == [str(tmp_path / "song.wav"), str(tmp_path / "song.mp4")]

    def test_afinfo_unavailable_is_a_warning(self, tmp_path, clock):
        live, exporter = make(clock, afinfo=None)
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True
        assert result["warnings"] == ["afinfo unavailable; the output file was not verified"]
        assert result["length_verified"] is False

    def test_afinfo_unparseable_is_a_warning(self, tmp_path, clock):
        live, exporter = make(clock, afinfo="garbage")
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True
        assert "could not read afinfo output" in result["warnings"][0]

    @pytest.mark.parametrize("afinfo,missing", [
        (afinfo_for(48000, "lpcm (0x0000000C) packed"), "bit depth"),
        ("File type ID:   WAVE\nestimated duration: 3.0 sec\n", "sample rate, bit depth"),
    ])
    def test_property_afinfo_does_not_report_is_a_warning(self, tmp_path, clock, afinfo, missing):
        live, exporter = make(clock, afinfo=afinfo)
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True, result
        assert result["warnings"] == [f"afinfo did not report the {missing}; not verified"]

    @pytest.mark.parametrize("expected", [12.5, 12.1, 12.9])
    def test_length_matching_the_expected_range_is_verified(self, tmp_path, clock, expected):
        live, exporter = make(clock)
        result = exporter.run(request(tmp_path, expected_seconds=expected))
        assert result["ok"] is True, result
        assert result["expected_seconds"] == expected
        assert result["rendered_seconds"] == 12.5
        assert result["length_verified"] is True
        assert result["warnings"] == []

    def test_unreadable_length_is_a_warning_not_a_verified_range(self, tmp_path, clock):
        afinfo = f"File type ID:   WAVE\nData format:     2 ch,  48000 Hz, {WAV_24}\n"
        live, exporter = make(clock, afinfo=afinfo)
        result = exporter.run(request(tmp_path, expected_seconds=12.5))
        assert result["ok"] is True, result
        assert result["rendered_seconds"] is None
        assert result["length_verified"] is False
        assert result["warnings"] == ["afinfo did not report the length; not verified"]

    def test_export_dialog_lingering_during_render_is_not_an_error(self, tmp_path, clock):
        live, exporter = make(clock, dialog_lingers=True,
                              window_texts={le.EXPORT_DIALOG_TITLE: ["Cannot be shown"]})
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True, result

    def test_disabled_video_switch_with_unreadable_state_is_left_alone(self, tmp_path, clock):
        live, exporter = make(clock)
        live.controls[le.CREATE_VIDEO]["value"] = None
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True, result

    def test_progress_window_named_like_existing_window(self, tmp_path, clock):
        live, exporter = make(clock)
        # An untitled window was already open; the progress window is also
        # untitled, so the render must still be tracked by count.
        live.windows.append(fake_window("", "AXUnknown"))
        live.render_polls = 2
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True, result
        assert live.names() == ["My Song", ""]

    def test_text_an_untitled_window_already_showed_is_not_a_live_error(self, tmp_path, clock):
        live, exporter = make(clock)
        # Every untitled window is read during the render, this one included;
        # what it said before the export started is not Live reporting on it.
        live.windows.append(fake_window("", "AXUnknown", texts=["Could not connect to Push"]))
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True, result
        assert "dialog_text" not in result


class TestExportFailure:
    """Every failure mode must come back as ok=False with a clear error."""

    def assert_failed(self, result, message):
        assert result["ok"] is False, result
        assert result["status"] == "failed"
        assert message in result["error"]
        return result

    def test_not_macos(self, tmp_path, clock):
        live, exporter = make(clock, platform="linux")
        self.assert_failed(exporter.run(request(tmp_path)), "not supported on 'linux'")
        assert live.calls == []

    def test_invalid_request(self, tmp_path, clock):
        live, exporter = make(clock)
        self.assert_failed(exporter.run(request(tmp_path, format="ogg")), "format must be one of")
        assert live.calls == []

    def test_missing_folder(self, tmp_path, clock):
        live, exporter = make(clock)
        result = exporter.run(le.ExportRequest(output_path=str(tmp_path / "nope" / "a.wav")))
        self.assert_failed(result, "output folder does not exist")
        assert live.calls == []

    def test_unwritable_folder(self, tmp_path, clock, monkeypatch):
        live, exporter = make(clock)
        monkeypatch.setattr(le.os, "access", lambda path, mode: False)
        self.assert_failed(exporter.run(request(tmp_path)), "output folder is not writable")
        assert live.calls == []

    def test_existing_file_without_overwrite(self, tmp_path, clock):
        (tmp_path / "song.wav").write_bytes(b"keep")
        live, exporter = make(clock)
        result = self.assert_failed(exporter.run(request(tmp_path)), "output file already exists")
        assert "overwrite=True" in result["error"]
        assert (tmp_path / "song.wav").read_bytes() == b"keep"
        assert live.calls == []

    def test_live_not_running(self, tmp_path, clock):
        live, exporter = make(clock)
        live.running = False
        self.assert_failed(exporter.run(request(tmp_path)), "Ableton Live is not running")

    def test_live_mid_dialog(self, tmp_path, clock):
        live, exporter = make(clock)
        live.open_window("Settings", modal=True)
        result = self.assert_failed(exporter.run(request(tmp_path)), "Live is showing a dialog")
        assert "Settings" in result["error"]
        assert "activate" not in live.ops()

    def test_live_not_frontmost(self, tmp_path, clock):
        live, exporter = make(clock, comes_to_front=False)
        self.assert_failed(exporter.run(request(tmp_path)), "export needs Live frontmost")
        assert "menu" not in live.ops()

    @pytest.mark.parametrize("state,reason", [
        ("missing", "menu item 'Navigate > Arrangement View' not found"),
        ("disabled", "menu item 'Navigate > Arrangement View' is disabled"),
    ])
    def test_arrangement_focus_unavailable(self, tmp_path, clock, state, reason):
        live, exporter = make(clock, arrangement_focus=state)
        self.assert_failed(exporter.run(request(tmp_path, select_loop=True)),
                           "could not give the Arrangement keyboard focus: " + reason)
        # Neither the loop is selected nor the dialog opened on an unfocused view.
        assert [m["path"] for m in live.driven("menu")] == [["Navigate", "Arrangement View"]]
        assert le.EXPORT_DIALOG_TITLE not in live.names()

    def test_select_loop_unavailable(self, tmp_path, clock):
        live, exporter = make(clock, select_loop_disabled=True)
        self.assert_failed(exporter.run(request(tmp_path, select_loop=True)),
                           "could not select the loop for the render range: "
                           "menu item 'Edit > Select Loop' is disabled")
        assert [m["path"] for m in live.driven("menu")] == [
            ["Navigate", "Arrangement View"], ["Edit", "Select Loop"],
        ]
        assert le.EXPORT_DIALOG_TITLE not in live.names()

    def test_export_menu_disabled(self, tmp_path, clock):
        live, exporter = make(clock, export_menu="disabled")
        self.assert_failed(exporter.run(request(tmp_path)), "could not open the Export dialog")
        assert "keystroke" not in live.ops()

    def test_dialog_never_appears(self, tmp_path, clock):
        live, exporter = make(clock, dialog_appears=False)
        self.assert_failed(exporter.run(request(tmp_path)),
                           "'Export Audio/Video' dialog did not appear within 3s")
        assert "set_toggle" not in live.ops()

    def test_toggle_that_does_not_change_cancels(self, tmp_path, clock):
        live, exporter = make(clock, stuck_toggles=(le.NORMALIZE,))
        self.assert_failed(exporter.run(request(tmp_path, normalize=True)),
                           "'Normalize' is False after toggling, expected True")
        assert live.driven("press")[-1]["label"] == "Cancel"
        assert le.EXPORT_DIALOG_TITLE not in live.names()
        assert "save_panel" not in live.ops()

    def test_choice_that_does_not_take_reports_choices(self, tmp_path, clock):
        live, exporter = make(clock)
        live.controls[le.SAMPLE_RATE]["options"] = ["44100"]
        result = self.assert_failed(exporter.run(request(tmp_path)),
                                    "could not set 'Sample Rate' to 48000: it reads '44100'")
        assert "choices seen: 44100" in result["error"]
        assert live.driven("press")[-1]["label"] == "Cancel"

    def test_rendered_track_list_not_led_by_the_main_bus(self, tmp_path, clock):
        live, exporter = make(clock)
        live.controls[le.RENDERED_TRACK] = {"kind": "choice", "value": "Main", "index": 2,
                                            "options": ["All Individual Tracks", "1 Audio", "Main"]}
        self.assert_failed(
            exporter.run(request(tmp_path)),
            "could not set 'Rendered Track Chooser': the first entry in the 'Rendered Track "
            "Chooser' list is 'All Individual Tracks', expected Main or Master")
        # The track named Main further down is neither accepted nor chosen.
        assert live.controls[le.RENDERED_TRACK]["index"] == 2
        assert live.driven("press")[-1]["label"] == "Cancel"
        assert "save_panel" not in live.ops()

    def test_rendered_track_still_on_a_track_after_choosing_the_top_entry(self, tmp_path, clock):
        # The chooser is read back against the entry that was pressed, so a
        # track named Master that stayed selected does not pass for Main.
        live, exporter = make(clock, stuck_choices=(le.RENDERED_TRACK,))
        live.controls[le.RENDERED_TRACK] = {"kind": "choice", "value": "Master", "index": 2,
                                            "options": ["Main", "1 Audio", "Master"]}
        self.assert_failed(exporter.run(request(tmp_path)),
                           "could not set 'Rendered Track Chooser' to Main: it reads 'Master'")
        assert live.driven("press")[-1]["label"] == "Cancel"
        assert "save_panel" not in live.ops()

    def test_control_not_found(self, tmp_path, clock):
        live, exporter = make(clock, missing_controls=(le.FILE_TYPE,))
        self.assert_failed(exporter.run(request(tmp_path)),
                           "could not set 'File Type': control 'File Type' not found")

    def test_video_switch_disabled(self, tmp_path, clock):
        live, exporter = make(clock)
        self.assert_failed(exporter.run(request(tmp_path, create_video=True)),
                           "Create Video is disabled")

    def test_disabled_toggle_that_needs_changing(self, tmp_path, clock):
        live, exporter = make(clock)
        live.controls[le.NORMALIZE]["enabled"] = False
        self.assert_failed(exporter.run(request(tmp_path, normalize=True)),
                           "could not set 'Normalize': 'Normalize' is disabled")

    def test_video_switch_stuck_on_while_disabled(self, tmp_path, clock):
        live, exporter = make(clock)
        live.controls[le.CREATE_VIDEO]["value"] = True
        self.assert_failed(exporter.run(request(tmp_path)), "Create Video is disabled")

    def test_export_button_disabled(self, tmp_path, clock):
        live, exporter = make(clock, export_disabled=True)
        self.assert_failed(exporter.run(request(tmp_path)), "could not press Export")

    def test_save_panel_never_appears_surfaces_live_text(self, tmp_path, clock):
        live, exporter = make(clock, save_panel_appears=False,
                              error_window=("Alert", ["Not enough disk space to render."]))
        result = self.assert_failed(exporter.run(request(tmp_path)), "Save panel did not appear")
        assert result["dialog_text"]["Alert"] == ["Not enough disk space to render."]

    def test_save_panel_never_appears_surfaces_untitled_alert_beside_older_untitled_window(
            self, tmp_path, clock):
        live, exporter = make(clock, save_panel_appears=False,
                              error_window=("", ["Not enough disk space to render."]))
        live.windows.append(fake_window("", "AXUnknown", texts=["Info View"]))
        result = self.assert_failed(exporter.run(request(tmp_path)), "Save panel did not appear")
        assert result["dialog_text"]["(untitled)"] == ["Not enough disk space to render."]

    def test_save_panel_never_appears_cancels_export_dialog(self, tmp_path, clock):
        live, exporter = make(clock, save_panel_appears=False)
        self.assert_failed(exporter.run(request(tmp_path)), "Save panel did not appear")
        assert le.EXPORT_DIALOG_TITLE not in live.names()
        # Live is left ready for the next export.
        assert le._busy_windows(live.op_status()["windows"]) == []

    @pytest.mark.parametrize("after", [None, "activate", "menu", "press", "save_panel"])
    def test_failed_status_is_a_scripting_error_not_a_state_of_live(self, tmp_path, clock, after):
        live, exporter = make(clock, status_fails_after=after)
        result = self.assert_failed(exporter.run(request(tmp_path)),
                                    "could not read Live's windows: " + STATUS_ERROR)
        for misreading in ("not running", "did not appear", "quit during"):
            assert misreading not in result["error"]
        assert result["output_path"] is None

    @pytest.mark.parametrize("after", ["menu", "press"])
    def test_failed_status_with_a_dialog_open_cancels_it(self, tmp_path, clock, after):
        live, exporter = make(clock, status_fails_after=after)
        exporter.run(request(tmp_path))
        assert le.EXPORT_DIALOG_TITLE not in live.names()
        assert le.SAVE_PANEL_TITLE not in live.names()

    def test_save_panel_error_cancels_both_windows(self, tmp_path, clock):
        live, exporter = make(clock, save_panel_error="the Save panel is in folder 'Music', expected 'out'")
        self.assert_failed(exporter.run(request(tmp_path)),
                           "could not fill in the Save panel: the Save panel is in folder 'Music'")
        cancels = [c["window"] for c in live.driven("press") if c["label"] == "Cancel"]
        assert cancels == ["Save", "Export Audio/Video"]

    def test_no_output_file(self, tmp_path, clock):
        live, exporter = make(clock, never_writes=True)
        result = self.assert_failed(exporter.run(request(tmp_path, render_timeout=5)),
                                    "no output file was produced within 5s")
        assert str(tmp_path / "song.wav") in result["error"]
        assert result["output_path"] is None
        assert result["output_files"] == []

    def test_wrong_extension_written_counts_as_absent(self, tmp_path, clock):
        live, exporter = make(clock, written_ext=".aif")
        self.assert_failed(exporter.run(request(tmp_path, render_timeout=4)), "no output file was produced")

    def test_render_never_finishes(self, tmp_path, clock):
        live, exporter = make(clock, window_stays=True)
        result = self.assert_failed(exporter.run(request(tmp_path, render_timeout=6)),
                                    "render did not finish within 6s")
        assert "Live still shows" in result["error"]

    def test_live_error_during_render_is_verbatim(self, tmp_path, clock):
        message = "The file could not be written: permission denied."
        live, exporter = make(clock, render_error=("Live", [message]))
        result = self.assert_failed(exporter.run(request(tmp_path)), message)
        assert result["dialog_text"] == {"Live": [message]}

    def test_untitled_live_error_is_read_beside_an_older_untitled_window(self, tmp_path, clock):
        # Live's alert is untitled, like the progress window and like a window
        # that was open before the export; it must not wait out the timeout.
        message = "The file could not be written: permission denied."
        live, exporter = make(clock, render_error=("", [message]))
        live.windows.append(fake_window("", "AXUnknown", texts=["Info View"]))
        result = self.assert_failed(exporter.run(request(tmp_path)),
                                    "Live reported an error during export: " + message)
        assert result["dialog_text"] == {"(untitled)": [message]}
        assert clock.sleeps < 5

    def test_progress_text_is_not_an_error(self, tmp_path, clock):
        live, exporter = make(clock, window_texts={"": ["Rendering audio..."]})
        result = exporter.run(request(tmp_path))
        assert result["ok"] is True, result

    def test_live_quits_during_render(self, tmp_path, clock):
        live, exporter = make(clock, quits_during_render=True)
        self.assert_failed(exporter.run(request(tmp_path)), "Ableton Live quit during the export")

    def test_empty_output_file(self, tmp_path, clock):
        live, exporter = make(clock)
        live.render_bytes = b""
        self.assert_failed(exporter.run(request(tmp_path, render_timeout=6)), "render did not finish")

    def test_empty_file_after_stable_render(self, tmp_path, clock, monkeypatch):
        live, exporter = make(clock)
        real_getsize = os.path.getsize
        calls = {"n": 0}

        def getsize(path):
            calls["n"] += 1
            # stable while polling, then empty when verified
            return 0 if calls["n"] > 3 else real_getsize(path)

        monkeypatch.setattr(le.os.path, "getsize", getsize)
        self.assert_failed(exporter.run(request(tmp_path)), "output file is empty")

    @pytest.mark.parametrize("fmt,message", [
        (WAV_16, "bit depth is 16, expected 24"),
        (WAV_32_FLOAT, "bit depth is 32, expected 24"),
        (AIFF_16, "bit depth is 16, expected 24"),
        (AIFC_32_FLOAT, "bit depth is 32, expected 24"),
    ])
    def test_bit_depth_mismatch(self, tmp_path, clock, fmt, message):
        live, exporter = make(clock, afinfo=afinfo_for(48000, fmt))
        result = self.assert_failed(exporter.run(request(tmp_path)), message)
        assert "was written but does not match the request" in result["error"]

    def test_24_bit_wav_does_not_pass_for_a_16_bit_request(self, tmp_path, clock):
        live, exporter = make(clock)
        self.assert_failed(exporter.run(request(tmp_path, bit_depth=16)),
                           "bit depth is 24, expected 16")

    def test_sample_rate_mismatch(self, tmp_path, clock):
        live, exporter = make(clock, afinfo=afinfo_for(44100, WAV_24))
        self.assert_failed(exporter.run(request(tmp_path)), "sample rate is 44100 Hz, expected 48000")

    def test_length_not_matching_the_expected_range(self, tmp_path, clock):
        live, exporter = make(clock)
        result = self.assert_failed(exporter.run(request(tmp_path, expected_seconds=32.25)),
                                    "it is 12.50 s long, expected 32.25 s for the requested range")
        assert result["output_path"] == str(tmp_path / "song.wav")
        assert result["rendered_seconds"] == 12.5
        assert result["length_verified"] is False

    def test_video_missing_after_render(self, tmp_path, clock):
        live, exporter = make(clock)
        live.controls[le.CREATE_VIDEO]["enabled"] = True
        self.assert_failed(exporter.run(request(tmp_path, create_video=True)),
                           "no new video file appeared")

    def test_driver_exception_is_failure(self, tmp_path, clock):
        class Broken:
            def call(self, op, **params):
                raise le.UIScriptingError("macOS blocked UI scripting.")

        exporter = le.LiveExporter(driver=Broken(), platform="darwin",
                                   clock=le._Clock(clock.now, clock.monotonic, clock.sleep))
        result = exporter.run(request(tmp_path))
        self.assert_failed(result, "macOS blocked UI scripting.")
        assert result["steps"] == []

    def test_unexpected_exception_is_failure(self, tmp_path, clock):
        class Broken:
            def call(self, op, **params):
                raise RuntimeError("boom")

        exporter = le.LiveExporter(driver=Broken(), platform="darwin",
                                   clock=le._Clock(clock.now, clock.monotonic, clock.sleep))
        self.assert_failed(exporter.run(request(tmp_path)), "unexpected export error: boom")

    def test_cancel_failure_does_not_mask_error(self, tmp_path, clock):
        live, exporter = make(clock, stuck_toggles=(le.NORMALIZE,))
        original = live.op_press

        def press(window, label):
            if label == le.CANCEL_BUTTON:
                raise le.UIScriptingError("gone")
            return original(window, label)

        live.op_press = press
        self.assert_failed(exporter.run(request(tmp_path, normalize=True)), "'Normalize' is False")

    def test_texts_error_is_skipped(self, tmp_path, clock):
        live, exporter = make(clock, never_writes=True)
        live.op_texts = lambda window: (_ for _ in ()).throw(le.ExportError("no window"))
        self.assert_failed(exporter.run(request(tmp_path, render_timeout=3)), "no output file")


def test_export_from_live_uses_given_exporter(tmp_path, clock):
    live, exporter = make(clock)
    assert le.export_from_live(request(tmp_path), exporter)["ok"] is True


def test_export_from_live_default_exporter(tmp_path, monkeypatch):
    monkeypatch.setattr(le.LiveExporter, "run", lambda self, req: {"ok": "sentinel"})
    assert le.export_from_live(request(tmp_path)) == {"ok": "sentinel"}


# ── helpers ──────────────────────────────────────────────────────


class TestHelpers:
    def test_busy_windows(self):
        windows = [
            {"name": "Song", "subrole": "AXStandardWindow", "modal": False, "sheets": 0},
            {"name": "", "subrole": "AXUnknown", "modal": False, "sheets": 0},
            {"name": "Export Audio/Video", "subrole": "AXDialog", "modal": False, "sheets": 0},
            {"name": "Save", "subrole": "AXDialog", "modal": True, "sheets": 0},
            {"name": "Song 2", "subrole": "AXStandardWindow", "modal": False, "sheets": 1},
            {"name": "", "subrole": "AXSystemDialog"},
        ]
        assert le._busy_windows(windows) == ["Export Audio/Video", "Save", "Song 2", "(untitled)"]

    def test_extra_windows_counts_duplicates(self):
        status = {"windows": [{"name": ""}, {"name": "Song"}, {"name": ""}]}
        assert le._extra_windows(status, ["Song", ""]) == [""]
        assert le._extra_windows(status, ["Song", "", ""]) == []

    @pytest.mark.parametrize("text,expected", [
        (WAV_AFINFO, {"file_type": "WAVE", "channels": 2, "sample_rate": 48000,
                      "data_format": WAV_24, "bit_depth": 24, "duration_seconds": 12.5}),
        (f"File type ID:   AIFC\nData format:     1 ch,  96000 Hz, {AIFC_32_FLOAT}\n",
         {"file_type": "AIFC", "channels": 1, "sample_rate": 96000,
          "data_format": AIFC_32_FLOAT, "bit_depth": 32}),
        (f"File type ID:   flac\nData format:     2 ch,  44100 Hz, {FLAC_24}\n",
         {"file_type": "flac", "channels": 2, "sample_rate": 44100, "bit_depth": 24,
          "data_format": FLAC_24}),
        (f"File type ID:   MPG3\nData format:     2 ch,  44100 Hz, {MP3}\n",
         {"file_type": "MPG3", "channels": 2, "sample_rate": 44100, "data_format": MP3}),
        ("", {}),
    ])
    def test_parse_afinfo(self, text, expected):
        assert le._parse_afinfo(text) == expected

    @pytest.mark.parametrize("fmt,depth", [
        (WAV_16, 16), (WAV_24, 24), (WAV_32_FLOAT, 32),
        (AIFF_16, 16), (AIFF_24, 24), (AIFC_32_FLOAT, 32), (FLAC_24, 24),
    ])
    def test_parse_afinfo_reads_bit_depth_of_every_pcm_format(self, fmt, depth):
        assert le._parse_afinfo(afinfo_for(48000, fmt))["bit_depth"] == depth

    def test_same(self):
        assert le._same("44,100", "44100")
        assert le._same(" Main ", "main")
        assert not le._same("Main", "Master")

    def test_run_afinfo(self, monkeypatch):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr(le.subprocess, "run", fake_run)
        assert le._run_afinfo("/x.wav") == "ok"
        assert seen["cmd"] == ["afinfo", "/x.wav"]

    def test_run_afinfo_failures(self, monkeypatch):
        monkeypatch.setattr(le.subprocess, "run",
                            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="bad"))
        assert le._run_afinfo("/x.wav") is None

        def missing(cmd, **kw):
            raise FileNotFoundError

        monkeypatch.setattr(le.subprocess, "run", missing)
        assert le._run_afinfo("/x.wav") is None


# ── osascript driver ─────────────────────────────────────────────


class TestJXADriver:
    def runner(self, returncode=0, stdout="", stderr="", exc=None):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            if exc:
                raise exc
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

        return run, calls

    def test_call_passes_op_and_bundle_as_json(self):
        run, calls = self.runner(stdout='{"ok": true, "frontmost": true}\n')
        driver = le.JXADriver(runner=run, timeout=7)
        assert driver.call("menu", path=["File", "Export Audio/Video"]) == {"ok": True, "frontmost": True}
        cmd, kw = calls[0]
        assert cmd[:4] == ["osascript", "-l", "JavaScript", "-e"]
        assert cmd[4] == le.JXA_DRIVER
        assert json.loads(cmd[5]) == {"op": "menu", "path": ["File", "Export Audio/Video"],
                                      "bundle": "com.ableton.live"}
        assert kw["timeout"] == 7 and kw["capture_output"] and kw["text"]

    def test_ok_false_is_returned_for_caller_to_handle(self):
        run, _ = self.runner(stdout='{"ok": false, "error": "control not found"}')
        assert le.JXADriver(runner=run).call("press", window="w", label="x")["ok"] is False

    @pytest.mark.parametrize("stdout", [
        '{"ok": false, "error": "System Events got an error: osascript is not allowed assistive access.", "code": -1719}',
        '{"ok": false, "error": "Not authorized to send Apple events to System Events.", "code": -1743}',
    ])
    def test_permission_errors_raise_with_hint(self, stdout):
        run, _ = self.runner(stdout=stdout)
        with pytest.raises(le.UIScriptingError) as excinfo:
            le.JXADriver(runner=run).call("status")
        assert "Accessibility" in str(excinfo.value)

    def test_nonzero_exit_with_permission_error(self):
        run, _ = self.runner(returncode=1, stderr="execution error: not allowed assistive access. (-25211)")
        with pytest.raises(le.UIScriptingError) as excinfo:
            le.JXADriver(runner=run).call("status")
        assert "System Settings > Privacy & Security > Accessibility" in str(excinfo.value)

    def test_nonzero_exit_other_error(self):
        run, _ = self.runner(returncode=1, stderr="syntax error")
        with pytest.raises(le.UIScriptingError, match="syntax error"):
            le.JXADriver(runner=run).call("status")

    def test_nonzero_exit_no_stderr(self):
        run, _ = self.runner(returncode=3, stderr="")
        with pytest.raises(le.UIScriptingError, match="osascript exited 3"):
            le.JXADriver(runner=run).call("status")

    def test_bad_json(self):
        run, _ = self.runner(stdout="not json")
        with pytest.raises(le.UIScriptingError, match="unreadable reply"):
            le.JXADriver(runner=run).call("status")

    def test_timeout(self):
        run, _ = self.runner(exc=subprocess.TimeoutExpired("osascript", 5))
        with pytest.raises(le.UIScriptingError, match="timed out after 5s"):
            le.JXADriver(runner=run, timeout=5).call("status")

    def test_osascript_missing(self):
        run, _ = self.runner(exc=FileNotFoundError())
        with pytest.raises(le.UIScriptingError, match="needs macOS"):
            le.JXADriver(runner=run).call("status")
