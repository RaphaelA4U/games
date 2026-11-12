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


# -- playing a seed and the history of every game (docs/ONLINE.md, phase 1) ------

@dataclass
class Played:
    """One game from a game's games.jsonl, in the same shape for every game."""
    game: str
    date: Optional[datetime]
    seed: Optional[int]
    settings: dict
    won: bool
    time: Optional[int] = None
    score: Optional[int] = None


def default_settings(game: dict[str, Any]) -> dict[str, str]:
    return {s["id"]: str(s["default"]) for s in (game.get("play") or {}).get("settings", [])}


def play_args(game: dict[str, Any], seed: Optional[int], settings: dict[str, Any]) -> list[str]:
    """The arguments that start exactly this game: the seed plus every setting
    (missing or unknown values fall back to the default)."""
    play = game.get("play") or {}
    args: list[str] = []
    if seed is not None:
        seed = int(seed)
        if not 1 <= seed <= int(play.get("seed_max", 999999)):
            raise ValueError(f"seed must be 1-{play.get('seed_max', 999999)}")
        args += [play.get("seed_flag", "--seed"), str(seed)]
    for s in play.get("settings", []):
        value = str(settings.get(s["id"], s["default"]))
        if value not in [str(c) for c in s["choices"]]:
            value = str(s["default"])
        args += [s["flag"], value]
    return args + [str(a) for a in play.get("extra_args", [])]


def history(games: Optional[list[dict[str, Any]]] = None, limit: int = 500) -> list[Played]:
    """Every recorded game of every game, newest first."""
    out: list[Played] = []
    for game in games if games is not None else load_registry():
        play = game.get("play") or {}
        if not play.get("history"):
            continue
        path = Path.home() / ".config" / game["id"] / play["history"]
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        result = play.get("result") or {}
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            try:
                date = datetime.fromisoformat(str(rec.get("date")))
            except ValueError:
                date = None
            seed = rec.get("seed")
            settings = {s["id"]: str(rec[s["id"]]) for s in play.get("settings", [])
                        if rec.get(s["id"]) is not None}

            def num(key: Optional[str]) -> Optional[int]:
                value = rec.get(key) if key else None
                return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

            out.append(Played(game=game["id"], date=date,
                              seed=int(seed) if isinstance(seed, int) else None,
                              settings=settings, won=bool(rec.get("won")),
                              time=num(result.get("time")), score=num(result.get("score"))))
    out.sort(key=lambda p: p.date or datetime.min, reverse=True)
    return out[:limit]


def played_result(p: Played) -> str:
    parts = ["won" if p.won else "lost"]
    if p.time is not None:
        parts.append(_fmt_time(p.time))
    if p.score is not None:
        parts.append(f"{p.score} pts")
    return ", ".join(parts)


# -- keeping a games host current -----------------------------------------------------

UPDATE_EVERY = 3600


def _update_stamp() -> Path:
    return Path.home() / ".config" / "games" / "last-update-all"


def update_all() -> list[str]:
    """Update the library and every installed game in place, each with its own updater (git pull +
    dependencies). A running game keeps the code it loaded; its next start uses the new version."""
    from .update import quick_sync_update

    out = []
    try:
        out.append(f"games: {'updated' if quick_sync_update() else 'up to date'}")
    except Exception as err:  # noqa: BLE001 - one failure must not stop the rest
        out.append(f"games: failed ({err})")
    for game in load_registry():
        py = game_home(game) / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
        if not py.exists():
            continue
        code = f"from {game['package']}.update import quick_sync_update; quick_sync_update()"
        try:
            rc = subprocess.run([str(py), "-c", code], cwd=str(Path.home()), capture_output=True,
                                timeout=600).returncode
            out.append(f"{game['id']}: {'ok' if rc == 0 else f'exit {rc}'}")
        except (OSError, subprocess.SubprocessError) as err:
            out.append(f"{game['id']}: failed ({err})")
    return out


def update_all_in_background(now: Optional[float] = None) -> bool:
    """At most once an hour, start `games --update-all` detached (used by `--list --json`, which
    BM Beheer asks every few minutes, so its games host stays on the newest versions)."""
    import time as _time

    now = now if now is not None else _time.time()
    stamp = _update_stamp()
    try:
        if now - stamp.stat().st_mtime < UPDATE_EVERY:
            return False
    except OSError:
        pass
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(int(now)))
        subprocess.Popen([sys.executable, "-m", "games_app", "--update-all"], cwd=str(Path.home()),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=not IS_WINDOWS)
        return True
    except OSError:
        return False
