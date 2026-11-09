"""Auto-update via git, Claude Code style: check in the background at
startup, install silently, and the new version runs on the next start
(right away when you are in the menu).

The app runs from a git checkout in ~/.games (editable install), so a
`git pull` instantly becomes the new code.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
GIT_TIMEOUT = 15

FAILED_MSG = "Update failed. It retries on the next start with an internet connection."


@dataclass
class UpdateResult:
    status: str  # "updated" | "uptodate" | "failed" | "disabled"
    message: str
    new_version: str | None = None


def is_git_checkout() -> bool:
    return (REPO_DIR / ".git").exists()


def _git(*args: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(
        ["git", "-C", str(REPO_DIR), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def current_version() -> str:
    from . import __version__

    return __version__


def _head_version() -> str | None:
    """Version from pyproject.toml at the current HEAD."""
    try:
        result = _git("show", "HEAD:pyproject.toml")
        for line in result.stdout.splitlines():
            if line.strip().startswith("version"):
                return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return None


def quick_sync_update(fetch_timeout: int = 4) -> bool:
    """Claude-style eager update at launch, BEFORE the UI starts.

    Fast fetch with a hard timeout so an offline start stays snappy; when an
    update exists it is pulled and installed right away, and the caller
    re-execs into the new code. Returns True if an update was installed.
    """
    if not is_git_checkout():
        return False
    try:
        fetch = _git("fetch", "--quiet", "origin", timeout=fetch_timeout)
        if fetch.returncode != 0:
            return False
        behind = _git("rev-list", "--count", "HEAD..@{u}")
        if behind.returncode != 0 or not behind.stdout.strip():
            return False
        if int(behind.stdout.strip()) == 0:
            return False
        new_version = "?"
        for line in _git("show", "@{u}:pyproject.toml").stdout.splitlines():
            if line.strip().startswith("version"):
                new_version = line.split("=", 1)[1].strip().strip('"')
                break
        print(
            f"\033[1;35m⚡ Updating games v{current_version()} → v{new_version}...\033[0m",
            flush=True,
        )
        deps_before = _git("show", "HEAD:pyproject.toml").stdout
        ok, reason = _pull_or_repair()
        if not ok:
            print(f"Update failed ({reason}); starting the current version.", flush=True)
            return False
        if reason:
            print(f"Repaired a blocked update ({reason}).", flush=True)
        if deps_before != _git("show", "HEAD:pyproject.toml").stdout:
            print("Refreshing dependencies...", flush=True)
            _reinstall_deps()
        print(f"\033[1;35m✓ Updated to v{new_version}, starting...\033[0m", flush=True)
        return True
    except (subprocess.TimeoutExpired, Exception):  # noqa: BLE001
        return False


def check_and_update() -> UpdateResult:
    """Fetch + fast-forward pull. Safe to run in a thread."""
    if not is_git_checkout():
        return UpdateResult("disabled", "Not a git checkout; updates disabled.")
    try:
        fetch = _git("fetch", "--quiet", "origin")
        if fetch.returncode != 0:
            return UpdateResult("failed", FAILED_MSG)
        behind = _git("rev-list", "--count", "HEAD..@{u}")
        if behind.returncode != 0 or not behind.stdout.strip():
            return UpdateResult("uptodate", "You are on the latest version.")
        if int(behind.stdout.strip()) == 0:
            return UpdateResult("uptodate", "You are on the latest version.")
        deps_before = _git("show", "HEAD:pyproject.toml").stdout
        ok, reason = _pull_or_repair()
        if not ok:
            return UpdateResult("failed", f"Update failed ({reason}). It retries on the next start.")
        deps_after = _git("show", "HEAD:pyproject.toml").stdout
        if deps_before != deps_after:
            _reinstall_deps()
        version = _head_version() or "?"
        return UpdateResult(
            "updated",
            f"Update installed: v{version}.",
            new_version=version,
        )
    except subprocess.TimeoutExpired:
        return UpdateResult("failed", FAILED_MSG)
    except Exception as exc:  # noqa: BLE001
        return UpdateResult("failed", f"Update failed ({exc}). It retries on the next start.")


def _pull_or_repair() -> tuple[bool, str]:
    """Fast-forward pull; when blocked, repair the app-owned checkout.

    ~/.games belongs to the app, so a blocked fast-forward (locally
    edited files, a stray merge state) is healed with a hard reset to
    upstream instead of failing forever. Returns (ok, reason); reason is
    empty for a clean pull.
    """
    pull = _git("pull", "--ff-only", "--quiet", timeout=60)
    if pull.returncode == 0:
        return True, ""
    lines = (pull.stderr or pull.stdout or "").strip().splitlines()
    reason = lines[0] if lines else "unknown error"
    heal = _git("reset", "--hard", "@{u}")
    return heal.returncode == 0, reason


def _reinstall_deps() -> None:
    """Refresh dependencies after an update that changed pyproject.toml."""
    target = str(REPO_DIR)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-e", target],
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        # Pip-less venv (Alpine/iSH): drive our venv with the outside pip.
        subprocess.run(
            ["python3", "-m", "pip", "--python", sys.executable,
             "install", "--no-cache-dir", "-e", target],
            capture_output=True,
            timeout=1800,
        )
