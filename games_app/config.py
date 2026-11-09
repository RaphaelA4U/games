"""Library settings in ~/.config/games/ (each game keeps its own config)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".config" / "games"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DEFAULT_SETTINGS: dict[str, Any] = {"last_seen_version": None}


def load_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    try:
        settings.update(json.loads(SETTINGS_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings))
        tmp.replace(SETTINGS_FILE)
    except OSError:
        pass
