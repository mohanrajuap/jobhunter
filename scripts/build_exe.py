#!/usr/bin/env python
"""Build a standalone jobhunter.exe (Windows) / jobhunter binary (macOS, Linux).

    pip install pyinstaller
    python scripts/build_exe.py

The result lands in dist/. It bundles Python, the UI and all dependencies, so it runs
on a machine without Python installed.

Two things are deliberately NOT bundled:
  * Your config, resumes and database — they stay beside the exe as normal files, so
    you can edit them without rebuilding.
  * The Playwright browser (~150 MB). On first run the exe tells you to install it
    with `python -m playwright install chromium`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. Run:\n    pip install pyinstaller")
        return 1

    separator = ";" if sys.platform == "win32" else ":"

    args = [
        sys.executable, "-m", "PyInstaller",
        "--name", "jobhunter",
        "--onefile",
        "--windowed",                       # no console window behind the UI
        "--noconfirm",
        "--clean",
        # The skill lexicon is read at runtime and must travel with the binary.
        "--add-data", f"{ROOT / 'jobhunter' / 'data'}{separator}jobhunter/data",
        "--add-data", f"{ROOT / 'config' / 'config.example.yaml'}{separator}config",
        # PyInstaller's static analysis misses these — they're imported lazily.
        "--hidden-import", "jobhunter.gui.app",
        "--hidden-import", "jobhunter.sources.naukri",
        "--hidden-import", "jobhunter.appliers.browser_apply",
        "--hidden-import", "jobhunter.appliers.naukri_apply",
        "--hidden-import", "pdfplumber",
        "--hidden-import", "docx",
        "--hidden-import", "apscheduler.schedulers.blocking",
        "--hidden-import", "apscheduler.triggers.cron",
        "--collect-all", "pdfplumber",
        "--collect-all", "pdfminer",
        str(ROOT / "run_gui.py"),
    ]

    icon = ROOT / "assets" / "jobhunter.ico"
    if icon.exists():
        args[args.index("--clean") + 1 : args.index("--clean") + 1] = ["--icon", str(icon)]

    print("Building… this takes a couple of minutes.\n")
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        print("\nBuild failed — see the PyInstaller output above.")
        return result.returncode

    exe = ROOT / "dist" / ("jobhunter.exe" if sys.platform == "win32" else "jobhunter")
    print(f"\nBuilt: {exe}")

    # Ship a config next to the exe so the first launch has something to load.
    target_config = ROOT / "dist" / "config"
    target_config.mkdir(exist_ok=True)
    if not (target_config / "config.yaml").exists():
        shutil.copy(ROOT / "config" / "config.example.yaml", target_config / "config.yaml")
        print(f"Starter config written to {target_config / 'config.yaml'} — edit it, or use the UI.")

    print(
        "\nBefore the first run, install the browser Playwright drives:\n"
        "    python -m playwright install chromium"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
