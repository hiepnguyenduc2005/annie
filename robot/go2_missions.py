"""Compatibility shim: this module moved to `robot.dog.planning.missions` (2026-09-20 cleanup). Import the new path."""
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repository root
import robot.dog.planning.missions as _module  # noqa: E402

_sys.modules[__name__] = _module  # every attribute, including private helpers, resolves on the new module
