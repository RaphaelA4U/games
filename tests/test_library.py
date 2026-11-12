import json
import subprocess
import sys

from games_app import library


def fake_install(home, game_id, version="1.2.3", package=None):
    package = package or f"{game_id}_app"
    (home / f".{game_id}" / package).mkdir(parents=True)
    (home / f".{game_id}" / package / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (home / f".{game_id}" / "bin").mkdir()
    cmd = home / f".{game_id}" / "bin" / game_id
    cmd.write_text("#!/bin/sh\necho played\n")
    cmd.chmod(0o755)


def test_registry_is_complete_and_battle_ready():
    games = library.load_registry()
    assert [g["id"] for g in games] == ["yahtzee", "solitaire", "sudoku"]
    for g in games:
        for key in ("id", "name", "package", "repo", "tagline", "description", "cover", "stats", "battle"):
            assert key in g, (g["id"], key)
        assert g["battle"]["type"] in ("match", "race")
        assert all(len(line) <= 12 for line in g["cover"])
        if g["battle"]["type"] == "race":
            assert "--battle {gid}" in g["battle"]["command"] and g["battle"]["progress_total"]


def test_installed_and_versions(fake_home):
    fake_install(fake_home, "sudoku", "1.1.1")
    games = {g["id"]: g for g in library.list_games()}
    assert games["sudoku"]["installed"] and games["sudoku"]["version"] == "1.1.1"
    assert not games["yahtzee"]["installed"] and games["yahtzee"]["version"] is None
    assert library.newer("1.2.0", "1.1.9") and not library.newer("1.1.1", "1.1.1")
    assert not library.newer(None, "1.0.0")


def test_stats_summary(fake_home):
    cfg = fake_home / ".config" / "sudoku"
    cfg.mkdir(parents=True)
    (cfg / "stats.json").write_text(json.dumps({"games": [
        {"won": True, "time": 400, "date": "2026-11-08T10:00:00"},
        {"won": True, "time": 372, "date": "2026-11-09T10:00:00"},
        {"won": False, "time": 50, "date": "2026-11-07T10:00:00"},
    ]}))
    sudoku = next(g for g in library.load_registry() if g["id"] == "sudoku")
    st = library.game_stats(sudoku)
    assert (st.played, st.won, st.best) == (3, 2, "6:12")
    cfg = fake_home / ".config" / "yahtzee"
    cfg.mkdir(parents=True)
    (cfg / "stats.json").write_text(json.dumps({"games_v2": [{"won": True, "your_score": 250}, {"your_score": 301}]}))
    yahtzee = next(g for g in library.load_registry() if g["id"] == "yahtzee")
    assert library.game_stats(yahtzee).best == "301"


def test_install_command_uses_the_games_own_installer():
    for g in library.load_registry():
        cmd = " ".join(library.install_command(g))
        assert f"raw.githubusercontent.com/RaphaelA4U/{g['id']}/main/install" in cmd
        assert library.install_env(g)[f"{g['id'].upper()}_YES"] == "1"


def test_cli_list_json(fake_home):
    out = subprocess.run([sys.executable, "-m", "games_app", "--list", "--json"],
                         capture_output=True, text=True, env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin"})
    data = json.loads(out.stdout)
    assert [g["id"] for g in data["games"]] == ["yahtzee", "solitaire", "sudoku"]
    assert all("battle" in g and "installed" in g for g in data["games"])


def test_list_json_keeps_a_games_host_current_once_an_hour(fake_home, monkeypatch):
    import importlib

    importlib.reload(library)  # the real function (conftest stubs it out for every other test)
    started = []
    monkeypatch.setattr(library.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    assert library.update_all_in_background(now=10_000) is True
    import os

    stamp = library._update_stamp()
    os.utime(stamp, (10_000, 10_000))
    assert library.update_all_in_background(now=10_000 + 600) is False   # 10 minutes later: not again
    assert library.update_all_in_background(now=10_000 + 3700) is True   # an hour later: again
    assert len(started) == 2 and started[0][-1] == "--update-all"


def test_update_all_runs_each_installed_games_own_updater(fake_home, monkeypatch):
    calls = []
    fake_install(fake_home, "sudoku", "1.7.0")
    py = fake_home / ".sudoku" / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr("games_app.update.quick_sync_update", lambda: False)

    class Done:
        returncode = 0

    monkeypatch.setattr(library.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Done())
    out = library.update_all()
    assert out[0] == "games: up to date" and "sudoku: ok" in out
    assert calls == [[str(py), "-c", "from sudoku_app.update import quick_sync_update; quick_sync_update()"]]
