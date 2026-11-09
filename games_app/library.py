"""The game library: which games exist (registry.json), which are installed,
their versions and stats, and how to install, update and start them.

registry.json is the single source of truth for every game: this launcher,
its installer, the games host of BM Beheer and beheer itself (chat battles)
all read it. Every game stays its own repo and installer, so installing one
game on its own keeps working.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

REGISTRY_FILE = Path(__file__).with_name("registry.json")
IS_WINDOWS = os.name == "nt"


def load_registry() -> list[dict[str, Any]]:
    data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    return list(data.get("games", []))


def game_home(game: dict[str, Any]) -> Path:
    env = os.environ.get(f"{game['id'].upper()}_HOME")
    return Path(env) if env else Path.home() / f".{game['id']}"


def game_command(game: dict[str, Any]) -> Optional[str]:
    """The path of the game's command, or None when it is not installed."""
    home = game_home(game)
    candidates = [
        home / "bin" / game["id"],
        Path.home() / ".local" / "bin" / game["id"],
    ]
    if IS_WINDOWS:
        candidates = [home / "bin" / f"{game['id']}.cmd", home / f"{game['id']}.cmd"] + candidates
    for c in candidates:
        if c.exists():
            return str(c)
    found = shutil.which(game["id"])
    return found


def installed_version(game: dict[str, Any]) -> Optional[str]:
    init = game_home(game) / game["package"] / "__init__.py"
    try:
        m = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def _version_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", v.lstrip("v")))


def latest_version(game: dict[str, Any], timeout: float = 6.0) -> Optional[str]:
    """Newest vX.Y.Z tag of the game's repo (one quick git request)."""
    try:
        out = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", game["repo"] + ".git"],
            capture_output=True, text=True, timeout=timeout,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    tags = [line.rsplit("/", 1)[-1] for line in out.splitlines() if "refs/tags/v" in line]
    tags = [t for t in tags if re.fullmatch(r"v\d+(\.\d+)*", t)]
    return max(tags, key=_version_key).lstrip("v") if tags else None


def newer(latest: Optional[str], current: Optional[str]) -> bool:
    return bool(latest and current and _version_key(latest) > _version_key(current))


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


@dataclass
class GameStats:
    played: int = 0
    won: int = 0
    best: Optional[str] = None
    last_played: Optional[datetime] = None
    extra: dict = field(default_factory=dict)


def game_stats(game: dict[str, Any]) -> GameStats:
    """A short summary from the game's own stats.json (main profile)."""
    spec = game.get("stats") or {}
    path = Path.home() / ".config" / game["id"] / "stats.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return GameStats()
    games = [g for g in (data.get(spec.get("list", "games")) or []) if isinstance(g, dict)]
    stats = GameStats(played=len(games), won=sum(1 for g in games if g.get("won")))
    won = [g for g in games if g.get("won")]
    if spec.get("best") == "time" and won:
        stats.best = _fmt_time(min(int(g.get("time") or 0) for g in won))
    elif spec.get("best") and games:
        values = [g.get(spec["best"]) for g in games if isinstance(g.get(spec["best"]), (int, float))]
        if values:
            stats.best = str(max(values))
    dates = []
    for g in games:
        try:
            dates.append(datetime.fromisoformat(str(g.get("date"))))
        except ValueError:
            continue
    stats.last_played = max(dates) if dates else None
    return stats


def ago(when: Optional[datetime], now: Optional[datetime] = None) -> str:
    if when is None:
        return "never"
    now = now or datetime.now()
    days = (now.date() - when.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    return when.strftime("%Y-%m-%d")


def install_command(game: dict[str, Any]) -> list[str]:
    """The game's own one-line installer (also updates an existing install)."""
    raw = game["repo"].replace("https://github.com/", "https://raw.githubusercontent.com/") + "/main"
    if IS_WINDOWS:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                f"irm {raw}/install.ps1 | iex"]
    url = f"{raw}/install.sh"
    return ["sh", "-c", f"(curl -fsSL {url} 2>/dev/null || wget -qO- {url}) | sh"]


def install_env(game: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env[f"{game['id'].upper()}_YES"] = "1"
    return env


def install(game: dict[str, Any]) -> int:
    return subprocess.call(install_command(game), env=install_env(game))


def install_all(only_missing: bool = False) -> int:
    failed = 0
    for game in load_registry():
        if only_missing and game_command(game):
            continue
        print(f"\n== {game['name']} ==", flush=True)
        if install(game) != 0:
            failed += 1
            print(f"{game['name']} did not install (it can be retried from the library).", file=sys.stderr)
    return failed


def list_games() -> list[dict[str, Any]]:
    """The registry plus what is installed here; `games --list --json`, read
    by BM Beheer from its games host."""
    out = []
    for game in load_registry():
        entry = dict(game)
        entry["installed"] = bool(game_command(game))
        entry["version"] = installed_version(game)
        out.append(entry)
    return out
