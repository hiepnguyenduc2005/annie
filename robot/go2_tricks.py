"""Compatibility shim: this module moved to `robot.dog.link.tricks` (2026-09-20 cleanup). Import the new path."""
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # repository root
import robot.dog.link.tricks as _module  # noqa: E402

_sys.modules[__name__] = _module  # every attribute, including private helpers, resolves on the new module

if __name__ == "__main__":
    _sys.exit(_module.main())
