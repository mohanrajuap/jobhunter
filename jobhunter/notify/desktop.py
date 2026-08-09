"""Desktop toast notification. Windows-first, with graceful no-ops elsewhere."""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess

log = logging.getLogger(__name__)


def send_desktop(title: str, message: str) -> None:
    system = platform.system()
    if system == "Windows":
        _windows(title, message)
    elif system == "Darwin":
        _macos(title, message)
    else:
        _linux(title, message)


def _windows(title: str, message: str) -> None:
    # BurntToast if present (proper Action Center toast), else a message box.
    script = (
        "if (Get-Module -ListAvailable -Name BurntToast) {"
        f"  Import-Module BurntToast; New-BurntToastNotification -Text {_ps(title)}, {_ps(message)}"
        "} else {"
        "  Add-Type -AssemblyName System.Windows.Forms;"
        "  $n = New-Object System.Windows.Forms.NotifyIcon;"
        "  $n.Icon = [System.Drawing.SystemIcons]::Information;"
        f" $n.BalloonTipTitle = {_ps(title)}; $n.BalloonTipText = {_ps(message)};"
        "  $n.Visible = $true; $n.ShowBalloonTip(15000); Start-Sleep -Seconds 16; $n.Dispose()"
        "}"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False, capture_output=True, timeout=45,
    )


def _macos(title: str, message: str) -> None:
    script = f'display notification {_osa(message)} with title {_osa(title)}'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True, timeout=20)


def _linux(title: str, message: str) -> None:
    if not shutil.which("notify-send"):
        log.debug("notify-send not available — skipping desktop notification")
        return
    subprocess.run(["notify-send", title, message], check=False, capture_output=True, timeout=20)


def _ps(value: str) -> str:
    """Single-quoted PowerShell literal (doubling embedded quotes)."""
    return "'" + value.replace("'", "''") + "'"


def _osa(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
