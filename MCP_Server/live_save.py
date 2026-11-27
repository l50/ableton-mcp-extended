"""Save the Live Set through File > Save Live Set (As...), and prove it was written.

Saving is not in the Live Object Model, so this drives the menu through the
same accessibility driver as the export (live_export.py). It runs only when a
caller asks for it: no other tool imports this module, and nothing here is
reached from the export or recording paths.

``save_set`` returns the path of the open Set (read from the Live window's
accessibility document) and the file's modification time read after the save;
it fails unless that time moved. ``save_set_as`` writes a new .als file and
checks Live now reports that path as the open Set. Both refuse, before any
menu click, while a dialog, Key Map mode, MIDI Map mode or running playback makes
the click unsafe (see live_ui_state.py).
"""

from __future__ import annotations

import contextlib
import datetime
import os
import time
from collections.abc import Callable
from typing import Any

from MCP_Server import live_ui_state

FILE_MENU = "File"
SAVE_MENU_ITEM = "Save Live Set"
SAVE_AS_MENU_ITEM = "Save Live Set As"
SAVE_PANEL_TITLE = "Save"
SET_EXTENSION = ".als"

DEFAULT_TIMEOUT = 20.0
DEFAULT_POLL_INTERVAL = 0.25
STABLE_POLLS = 3


class SaveError(Exception):
    """A save failed or could not be proven; the message is reported verbatim."""


def _stat(path: str) -> os.stat_result | None:
    try:
        return os.stat(path)
    except OSError:
        return None


def _describe(stat: os.stat_result) -> dict[str, Any]:
    return {
        "mtime": stat.st_mtime,
        "modified": datetime.datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
        "size_bytes": stat.st_size,
    }


def _refuse(state_error: live_ui_state.UnsafeUIState) -> dict[str, Any]:
    return {
        "ok": False,
        "saved": False,
        "error": "Live's UI is not safe to drive, so nothing was clicked: "
        + "; ".join(state_error.reasons),
        "unsafe_reasons": state_error.reasons,
    }


def _wait_until(
    predicate: Callable[[], bool],
    timeout: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    interval: float,
) -> bool:
    deadline = monotonic() + timeout
    while True:
        if predicate():
            return True
        if monotonic() >= deadline:
            return False
        sleep(interval)


def _settled(
    path: str,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    interval: float,
    timeout: float,
) -> os.stat_result | None:
    """The file's stat once its size and time have stopped changing."""
    last: tuple | None = None
    stable = 0
    deadline = monotonic() + timeout
    while True:
        stat = _stat(path)
        key = (stat.st_size, stat.st_mtime_ns) if stat else None
        stable = stable + 1 if key is not None and key == last else 0
        last = key
        if stable >= STABLE_POLLS - 1 and stat is not None:
            return stat
        if monotonic() >= deadline:
            return stat
        sleep(interval)


def _dialogs_note(driver: Any, armed_reader: Any) -> list[str]:
    try:
        state = live_ui_state.read_ui_state(driver, armed_reader)
    except Exception:
        return []
    return [
        f"'{d.get('name')}': {' | '.join(d.get('text') or [])}" for d in state.get("dialogs") or []
    ]


def save_set(
    driver: Any = None,
    armed_reader: Callable[[], list[dict[str, Any]]] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> dict[str, Any]:
    """File > Save Live Set on the open Set; return its path and new modification time.

    ``ok`` is true only when the Set's .als file exists and its modification
    time is later than before the save. An untitled Set (no file yet) is
    refused: use ``save_set_as``. Live disables File > Save Live Set while the
    Set has no unsaved changes; that is reported as a failure (``unmodified``)
    because the file's time cannot change.
    """
    driver = driver or live_ui_state._default_driver()
    try:
        state = live_ui_state.require_safe_to_drive(driver, armed_reader)
    except live_ui_state.UnsafeUIState as e:
        return _refuse(e)
    path = (state.get("set") or {}).get("path")
    title = (state.get("set") or {}).get("window_title")
    if not path:
        return {
            "ok": False,
            "saved": False,
            "set_title": title,
            "error": f"the open Set ('{title}') has no file yet, so there is nothing to save in "
            "place; use save_set_as(path)",
        }
    before = _stat(path)
    if before is None:
        return {
            "ok": False,
            "saved": False,
            "path": path,
            "error": f"Live says the open Set is {path}, but that file does not exist",
        }
    result: dict[str, Any] = {
        "ok": False,
        "saved": False,
        "path": path,
        "mtime_before": before.st_mtime,
        "transport_before": live_ui_state.transport_snapshot(state),
    }
    clicked = driver.call("menu", path=[FILE_MENU, SAVE_MENU_ITEM])
    if not clicked.get("ok", False):
        result["error"] = f"File > {SAVE_MENU_ITEM}: {clicked.get('error') or 'failed'}"
        if clicked.get("disabled"):
            result["error"] += (
                ": Live only enables it when the Set has unsaved changes, so there was "
                "nothing to save and the file was not touched"
            )
            result["unmodified"] = True
        return result
    moved = _wait_until(
        lambda: (lambda s: s is not None and s.st_mtime_ns > before.st_mtime_ns)(_stat(path)),
        timeout,
        monotonic,
        sleep,
        poll_interval,
    )
    if not moved:
        dialogs = _dialogs_note(driver, armed_reader)
        extra = f"; Live is showing {', '.join(dialogs)}" if dialogs else ""
        result["error"] = (
            f"the modification time of {path} did not change within {timeout:.0f}s after "
            f"Save Live Set, so the Set was not saved{extra}"
        )
        if dialogs:
            result["dialogs"] = dialogs
        return result
    after = _settled(path, monotonic, sleep, poll_interval, timeout)
    if after is None:
        result["error"] = f"{path} disappeared after the save"
        return result
    result.update(_describe(after))
    result["mtime_before"] = before.st_mtime
    result["ok"] = True
    result["saved"] = True
    return result


def _validate_target(path: str, overwrite: bool) -> str:
    raw = str(path or "").strip()
    if not raw:
        raise SaveError("path is required (an absolute path ending in .als)")
    full = os.path.expanduser(raw)
    if not os.path.isabs(full):
        raise SaveError(f"path must be absolute (got '{raw}')")
    if full.endswith(os.sep):
        raise SaveError(f"path must name a file, not a folder (got '{raw}')")
    directory, filename = os.path.split(full)
    stem, ext = os.path.splitext(filename)
    if ext.lower() != SET_EXTENSION or not stem:
        raise SaveError(f"path must end in {SET_EXTENSION} (got '{raw}')")
    if not os.path.isdir(directory):
        raise SaveError(f"folder does not exist: {directory}")
    if not os.access(directory, os.W_OK | os.X_OK):
        raise SaveError(f"folder is not writable: {directory}")
    for existing in (full, _project_path(full)):
        if os.path.exists(existing) and not overwrite:
            raise SaveError(f"{existing} already exists (pass overwrite=True to replace it)")
    return full


def _project_path(target: str) -> str:
    """Where Live puts a Set it saves for the first time: in a "<name> Project" folder."""
    directory, filename = os.path.split(target)
    stem = os.path.splitext(filename)[0]
    return os.path.join(directory, f"{stem} Project", filename)


def save_set_as(
    path: str,
    overwrite: bool = False,
    driver: Any = None,
    armed_reader: Callable[[], list[dict[str, Any]]] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> dict[str, Any]:
    """File > Save Live Set As... to ``path``; return the saved path and modification time.

    ``ok`` is true only when the new .als file exists, is not empty, and Live
    reports it as the open Set. A Set that was never saved goes into a
    "<name> Project" folder that Live makes: ``path`` is where the file really
    is, ``requested_path`` what was asked for and ``in_project_folder`` says
    which happened. The Save panel's Replace sheet is answered only when
    overwrite is true. On a failure after the panel opened, the panel is
    cancelled.
    """
    try:
        target = _validate_target(path, overwrite)
    except SaveError as e:
        return {"ok": False, "saved": False, "error": str(e)}
    driver = driver or live_ui_state._default_driver()
    try:
        live_ui_state.require_safe_to_drive(driver, armed_reader)
    except live_ui_state.UnsafeUIState as e:
        return _refuse(e)
    result: dict[str, Any] = {"ok": False, "saved": False, "path": target}
    started = time.time()
    clicked = driver.call("menu", path=[FILE_MENU, SAVE_AS_MENU_ITEM])
    if not clicked.get("ok", False):
        result["error"] = f"File > {SAVE_AS_MENU_ITEM}: {clicked.get('error') or 'failed'}"
        return result
    try:
        opened = _wait_until(
            lambda: SAVE_PANEL_TITLE in [w.get("name") for w in driver.call("status")["windows"]],
            timeout,
            monotonic,
            sleep,
            poll_interval,
        )
        if not opened:
            raise SaveError(f"the Save panel did not appear within {timeout:.0f}s")
        filled = driver.call(
            "save_panel",
            window=SAVE_PANEL_TITLE,
            directory=os.path.dirname(target),
            filename=os.path.splitext(os.path.basename(target))[0],
            folder_name=os.path.basename(os.path.dirname(target).rstrip(os.sep)),
            replace=bool(overwrite),
        )
        if not filled.get("ok", False):
            raise SaveError(f"could not fill in the Save panel: {filled.get('error') or 'failed'}")
        candidates = [target, _project_path(target)]
        found: list[str] = []

        def written() -> bool:
            for candidate in candidates:
                stat = _stat(candidate)
                if stat is not None and stat.st_size > 0 and stat.st_mtime >= started - 1:
                    found.append(candidate)
                    return True
            return False

        if not _wait_until(written, timeout, monotonic, sleep, poll_interval):
            raise SaveError(
                f"neither {candidates[0]} nor {candidates[1]} was written within {timeout:.0f}s"
            )
        written_path = found[0]
        stat = _settled(written_path, monotonic, sleep, poll_interval, timeout)
        if stat is None:
            raise SaveError(f"{written_path} disappeared after the save")
        state = live_ui_state.read_ui_state(driver, armed_reader)
        open_path = (state.get("set") or {}).get("path")
        result["open_set_path"] = open_path
        result["set_title"] = (state.get("set") or {}).get("window_title")
        if state.get("dialogs"):
            raise SaveError(
                "Live is still showing a dialog after the save: "
                + "; ".join(
                    f"'{d.get('name')}': {' | '.join(d.get('text') or [])}"
                    for d in state["dialogs"]
                )
            )
        if open_path is None or os.path.realpath(open_path) != os.path.realpath(written_path):
            raise SaveError(
                f"{written_path} was written but Live reports the open Set as "
                f"{open_path or 'untitled'}"
            )
        result["path"] = written_path
        result["requested_path"] = target
        result["in_project_folder"] = written_path != target
        result.update(_describe(stat))
        result["ok"] = True
        result["saved"] = True
    except SaveError as e:
        result["error"] = str(e)
        with contextlib.suppress(Exception):
            driver.call("cancel", window=SAVE_PANEL_TITLE)
    except Exception as e:
        result["error"] = f"unexpected save error: {e}"
        with contextlib.suppress(Exception):
            driver.call("cancel", window=SAVE_PANEL_TITLE)
    return result
