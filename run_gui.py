#!/usr/bin/env python
"""Double-click entry point for the desktop UI.

`python run_gui.py` — or run the built jobhunter.exe, which bundles this file.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running straight from a source checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    from jobhunter.logging_setup import setup_logging

    setup_logging(log_dir="logs", level="INFO", quiet=True)

    try:
        from jobhunter.gui import launch
    except ImportError as exc:
        print(f"Could not start the UI: {exc}")
        print("Tkinter is required. On Windows it ships with Python; on Linux install python3-tk.")
        return 1

    config_arg = sys.argv[1] if len(sys.argv) > 1 else None
    return launch(config_arg)


if __name__ == "__main__":
    raise SystemExit(main())
