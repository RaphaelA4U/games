"""Test isolation: a fake home, so nothing touches the real games or config."""

import pytest

import games_app.config as cfg


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path / ".config" / "games")
    monkeypatch.setattr(cfg, "SETTINGS_FILE", tmp_path / ".config" / "games" / "settings.json")
    for game in ("yahtzee", "solitaire", "sudoku"):
        monkeypatch.delenv(f"{game.upper()}_HOME", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    # Never start a real background update (it would git pull this checkout).
    monkeypatch.setattr("games_app.library.update_all_in_background", lambda now=None: False)
    return tmp_path
