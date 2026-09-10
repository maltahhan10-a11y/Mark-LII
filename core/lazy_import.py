"""Deferred imports for libraries that are expensive to load and rarely used.

Measured on this machine: importing pyautogui costs 94 MB of resident memory,
because on macOS it pulls in pyobjc, Quartz and AppKit behind it. Five action
modules imported it at module scope and main.py imports all five at startup,
so every session paid that 94 MB whether or not the user ever asked Jarvis to
move the mouse -- and two of those modules never called it at all.

The proxy below defers the real import to the first attribute access. Call
sites keep working untouched (`pyautogui.click(...)` still reads the same),
which matters when there are over a hundred of them and rewriting each one is
a hundred chances to introduce a bug in working code.
"""

from __future__ import annotations

import importlib
import importlib.util
import threading


def available(module: str) -> bool:
    """Is a module installed? Answers without importing it.

    find_spec walks the import machinery but stops before execution, so this
    stays cheap even for a module that would cost 94 MB to load.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


class LazyModule:
    """Stands in for a module until something actually touches it."""

    def __init__(self, name: str, configure=None):
        self._name = name
        self._configure = configure
        self._module = None
        self._lock = threading.Lock()

    def _load(self):
        if self._module is None:
            with self._lock:
                if self._module is None:      # re-check: another thread may
                    mod = importlib.import_module(self._name)   # have won the race
                    if self._configure is not None:
                        self._configure(mod)
                    self._module = mod
        return self._module

    def __getattr__(self, item):
        # Dunder lookups happen during interpreter bookkeeping (copy, pickle,
        # inspect) and must not drag in a 94 MB library as a side effect.
        if item.startswith("__") and item.endswith("__"):
            raise AttributeError(item)
        return getattr(self._load(), item)

    def __setattr__(self, item, value):
        if item.startswith("_") and item in ("_name", "_configure", "_module", "_lock"):
            object.__setattr__(self, item, value)
        else:
            setattr(self._load(), item, value)

    def __bool__(self) -> bool:
        return available(self._name)

    def loaded(self) -> bool:
        """Whether the real module has actually been pulled in yet."""
        return self._module is not None


def _configure_pyautogui(mod) -> None:
    # The failsafe stops a runaway script: slamming the pointer into a corner
    # aborts it. Worth keeping on a machine driven by voice.
    mod.FAILSAFE = True
    mod.PAUSE = 0.05


def pyautogui() -> LazyModule:
    return _PYAUTOGUI_PROXY


_PYAUTOGUI_PROXY = LazyModule("pyautogui", _configure_pyautogui)
