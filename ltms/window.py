"""Open a separate terminal window running the live dashboard.

When a coding agent invokes ltms, stdout is a pipe -- nothing would ever be
visible. So the worker spawns a detached watcher window. The watcher is
read-only: if it fails to open, or the user closes it, the research is
unaffected.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

TITLE = "LessTokenMoreSearch"

LINUX_TERMINALS = [
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-e"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("wezterm", ["start", "--"]),
    ("xterm", ["-e"]),
]


def _watch_command(run_dir: Path) -> list[str]:
    return [sys.executable, "-m", "ltms", "gui", str(run_dir)]


def _spawn_windows(run_dir: Path) -> bool:
    # Quoting through `start` is fragile, so put the real command in a small
    # script and let cmd read it verbatim. On Windows 11 `start` already opens
    # Windows Terminal when it is the default console host.
    script = run_dir / "watch.cmd"
    script.write_text(
        "\r\n".join(
            [
                "@echo off",
                f"title {TITLE}",
                f'"{sys.executable}" -m ltms gui "{run_dir}"',
            ]
        )
        + "\r\n",
        encoding="utf-8",
    )
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", TITLE, "cmd", "/k", str(script)],
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            close_fds=True,
        )
        return True
    except OSError:
        return False


def _spawn_macos(run_dir: Path) -> bool:
    command = " ".join(f"'{part}'" for part in _watch_command(run_dir))
    script = f'tell application "Terminal" to do script "{command}"'
    try:
        subprocess.Popen(["osascript", "-e", script], close_fds=True)
        return True
    except OSError:
        return False


def _spawn_linux(run_dir: Path) -> bool:
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return False
    for name, prefix in LINUX_TERMINALS:
        binary = shutil.which(name)
        if not binary:
            continue
        try:
            subprocess.Popen([binary, *prefix, *_watch_command(run_dir)], close_fds=True, start_new_session=True)
            return True
        except OSError:
            continue
    return False


def open_dashboard(run_dir: Path) -> bool:
    """Best effort. Returns True if a window was launched."""
    if os.environ.get("LTMS_NO_WINDOW"):
        return False
    try:
        if sys.platform == "win32":
            return _spawn_windows(run_dir)
        if sys.platform == "darwin":
            return _spawn_macos(run_dir)
        return _spawn_linux(run_dir)
    except Exception:  # noqa: BLE001 - a cosmetic feature must never break a run
        return False
