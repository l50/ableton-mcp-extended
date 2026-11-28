"""Shared test fixtures for Ableton MCP Extended tests."""

import os
import re
import subprocess

import pytest

# The tools that drive Live's UI run osascript (System Events, JXA) and a screen
# capture would show the owner's screen. No test may start either, from a test,
# a fixture of any scope, or code imported while collecting. The block is
# installed when this file is imported, before any test module is collected,
# and removed only when pytest shuts down.
SCREEN_TOOLS = ("osascript", "screencapture")
# Programs that run another program from their arguments or a command string.
_WRAPPERS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "env", "xargs", "nohup", "sudo",
             "arch", "exec", "command", "time", "caffeinate"}
_COMMAND_TEXT = re.compile(r"(?:^|[\s;&|(`'\"=/])(?:%s)(?=$|[\s;&|)`'\"])" % "|".join(SCREEN_TOOLS))


class ScreenAccessBlocked(BaseException):
    """A test tried to start osascript or screencapture.

    A BaseException on purpose: the tools catch ``Exception`` and turn it
    into an error reply, which would let a run that tried to reach Live look
    like an ordinary failure.
    """


def _tokens(value):
    if isinstance(value, (str, bytes, os.PathLike)):
        return [os.fsdecode(value)]
    if isinstance(value, (list, tuple)):
        return [t for item in value for t in _tokens(item)]
    return []


def launches_screen_tool(args, shell=False):
    """Whether a process launch (argv list, or a string run by a shell) starts a screen tool."""
    tokens = _tokens(args)
    if not tokens:
        return False
    if any(os.path.basename(t) in SCREEN_TOOLS for t in tokens[:1]):
        return True
    if shell or os.path.basename(tokens[0]) in _WRAPPERS or len(tokens) == 1:
        if any(os.path.basename(t) in SCREEN_TOOLS for t in tokens):
            return True
        return any(_COMMAND_TEXT.search(t) for t in tokens)
    return False


def _refuse(what):
    raise ScreenAccessBlocked(
        f"tests must not start osascript or screencapture (it would drive or show the real "
        f"Live UI): {what}"
    )


_real = {}


def _install_screen_block():
    popen_init = subprocess.Popen.__init__

    def popen(self, args, *a, **kw):
        shell = kw.get("shell", a[7] if len(a) > 7 else False)
        if launches_screen_tool(args, shell=bool(shell)):
            _refuse(f"subprocess {args!r}")
        return popen_init(self, args, *a, **kw)

    _real["Popen.__init__"] = popen_init
    subprocess.Popen.__init__ = popen

    def wrap(name, shell=False):
        original = getattr(os, name, None)
        if original is None:
            return
        _real[name] = original

        def guarded(*a, **kw):
            if launches_screen_tool(list(a), shell=shell) or any(
                os.path.basename(t) in SCREEN_TOOLS for t in _tokens(list(a))
            ):
                _refuse(f"os.{name}{a!r}")
            return original(*a, **kw)

        setattr(os, name, guarded)

    wrap("system", shell=True)
    for name in ("posix_spawn", "posix_spawnp", "execv", "execve", "execvp", "execvpe",
                 "execl", "execle", "execlp", "execlpe", "spawnv", "spawnve", "spawnvp",
                 "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe"):
        wrap(name)


def _remove_screen_block():
    original = _real.pop("Popen.__init__", None)
    if original is not None:
        subprocess.Popen.__init__ = original
    for name, fn in list(_real.items()):
        setattr(os, name, fn)
        del _real[name]


_install_screen_block()


def pytest_unconfigure(config):
    _remove_screen_block()


@pytest.fixture
def time_signature_4_4():
    """Standard 4/4 time signature."""
    return {"numerator": 4, "denominator": 4}


@pytest.fixture
def time_signature_3_4():
    """Waltz 3/4 time signature."""
    return {"numerator": 3, "denominator": 4}


@pytest.fixture
def time_signature_6_8():
    """Compound 6/8 time signature."""
    return {"numerator": 6, "denominator": 8}
