import pytest

from games_app.ui.app import BootScreen, GamesApp, LibraryScreen, Shelf, TextPage
from tests.test_library import fake_install

SIZE = (110, 40)


async def library(pilot, app):
    for _ in range(60):
        await pilot.pause(0.05)
        if isinstance(app.screen, LibraryScreen):
            return app.screen
    raise AssertionError(app.screen)


@pytest.mark.asyncio
async def test_boot_then_shelf_and_details(fake_home):
    fake_install(fake_home, "sudoku", "1.1.1")
    app = GamesApp(no_update=True, runner=lambda c, e, p: 0)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, BootScreen)
        await pilot.press("x")
        screen = await library(pilot, app)
        shelf = screen.query_one(Shelf)
        plain = shelf.render().plain
        assert "YAHTZEE" in plain and "SOLITAIRE" in plain and "SUDOKU" in plain
        assert "not installed" in plain and "v1.1.1" in plain
        await pilot.press("right", "right")
        assert shelf.selected == 2
        assert "Sudoku" in str(screen.query_one("#details").render())
        await pilot.press("question_mark")
        assert isinstance(app.screen, TextPage)
        await pilot.press("escape")
        assert app.screen is screen


@pytest.mark.asyncio
async def test_play_installs_first_then_runs_the_game(fake_home):
    calls = []

    def runner(cmd, env, pause):
        calls.append((cmd, pause))
        if "install.sh" in " ".join(cmd):
            fake_install(fake_home, "solitaire", "1.5.0")
        return 0

    app = GamesApp(no_update=True, boot=False, runner=runner)
    async with app.run_test(size=SIZE) as pilot:
        screen = await library(pilot, app)
        await pilot.press("2", "enter")
        await pilot.pause()
        assert "install.sh" in " ".join(calls[0][0]) and calls[0][1] is True
        assert calls[1][0][0].endswith("/.solitaire/bin/solitaire") and calls[1][1] is False
        assert "v1.5.0" in screen.query_one(Shelf).render().plain


@pytest.mark.asyncio
async def test_mouse_double_click_plays(fake_home):
    fake_install(fake_home, "yahtzee", "1.14.0")
    calls = []
    app = GamesApp(no_update=True, boot=False, runner=lambda c, e, p: calls.append(c) or 0)
    async with app.run_test(size=SIZE) as pilot:
        screen = await library(pilot, app)
        shelf = screen.query_one(Shelf)
        x0, y0, *_ = shelf._rects[0]
        await pilot.click(Shelf, offset=(x0 + 5, y0 + 3))
        await pilot.click(Shelf, offset=(x0 + 5, y0 + 3))
        await pilot.pause()
        assert calls and calls[0][0].endswith("/.yahtzee/bin/yahtzee")


@pytest.mark.asyncio
async def test_small_terminal_wraps_the_shelf(fake_home):
    app = GamesApp(no_update=True, boot=False, runner=lambda c, e, p: 0)
    async with app.run_test(size=(44, 30)) as pilot:
        screen = await library(pilot, app)
        shelf = screen.query_one(Shelf)
        assert shelf.per_row() == 2
        await pilot.press("down")
        assert shelf.selected == 2


def test_cartridge_lines_are_all_the_same_width():
    from games_app.library import load_registry
    from games_app.ui.app import CART_H, CART_W, Canvas, draw_cart

    cv = Canvas(CART_W + 2, CART_H)
    draw_cart(cv, 0, 0, load_registry()[0], "v1.0.0", selected=True, hovered=False)
    rows = cv.to_text().plain.splitlines()
    assert all(len(r.rstrip()) == CART_W for r in rows), rows


def _write_history(home):
    import json

    d = home / ".config" / "sudoku"
    d.mkdir(parents=True, exist_ok=True)
    (d / "games.jsonl").write_text(
        json.dumps({"won": True, "difficulty": "expert", "time": 2545, "seed": 651828,
                    "date": "2026-11-11T14:38:42", "log": []}) + "\n"
        + json.dumps({"won": False, "difficulty": "hard", "time": 60, "seed": 48024,
                      "date": "2026-11-10T10:00:00", "log": []}) + "\n")
    s = home / ".config" / "solitaire"
    s.mkdir(parents=True, exist_ok=True)
    (s / "games.jsonl").write_text(json.dumps(
        {"won": True, "draw": 3, "scoring": "vegas", "score": 52, "time": 300, "seed": 7,
         "date": "2026-11-12T09:00:00", "log": []}) + "\n" + "not json\n")


@pytest.mark.asyncio
async def test_history_lists_every_game_and_plays_the_same_seed_again(fake_home):
    from games_app.ui.app import HistoryScreen, RowList

    fake_install(fake_home, "sudoku", "1.6.0")
    fake_install(fake_home, "solitaire", "1.6.0")
    _write_history(fake_home)
    ran = []
    app = GamesApp(no_update=True, boot=False, runner=lambda c, e, p: ran.append(c) or 0)
    async with app.run_test(size=SIZE) as pilot:
        await library(pilot, app)
        await pilot.press("h")
        await pilot.pause()
        assert isinstance(app.screen, HistoryScreen)
        rows = [r.plain for r in app.screen.query_one(RowList).rows]
        assert len(rows) == 3 and "Solitaire" in rows[0] and "#7" in rows[0]  # newest first
        assert "#651828" in rows[1] and "expert" in rows[1] and "won, 42:25" in rows[1]
        await pilot.press("down", "enter")
        assert ran[-1][1:] == ["--seed", "651828", "--difficulty", "expert"]
        await pilot.press("up", "enter")
        assert ran[-1][1:] == ["--seed", "7", "--draw", "3", "--scoring", "vegas"]
        await pilot.press("escape")
        assert isinstance(app.screen, LibraryScreen)


@pytest.mark.asyncio
async def test_play_a_seed_with_settings(fake_home):
    from games_app.ui.app import SeedScreen

    fake_install(fake_home, "sudoku", "1.6.0")
    ran = []
    app = GamesApp(no_update=True, boot=False, runner=lambda c, e, p: ran.append(c) or 0)
    async with app.run_test(size=SIZE) as pilot:
        screen = await library(pilot, app)
        await pilot.press("3", "s")  # sudoku selected on the shelf
        await pilot.pause()
        assert isinstance(app.screen, SeedScreen) and app.screen.game["id"] == "sudoku"
        await pilot.press("down", "4", "2")  # the seed row: typing starts a new number
        assert app.screen.seed == 42
        await pilot.press("backspace", "7")
        assert app.screen.seed == 47
        await pilot.press("down", "right", "right")  # difficulty hard -> expert -> master
        await pilot.press("enter")
        assert ran[-1][1:] == ["--seed", "47", "--difficulty", "master"]
        await pilot.press("up", "up", "right")  # the game row: next game, its own defaults
        assert app.screen.game["id"] == "yahtzee" and app.screen.settings["rules"] == "official"


def test_history_survives_odd_lines_and_unseeded_games(fake_home):
    import json

    from games_app.library import history, play_args, load_registry

    _write_history(fake_home)
    y = fake_home / ".config" / "yahtzee"
    y.mkdir(parents=True)
    (y / "games.jsonl").write_text(json.dumps({"won": True, "your_score": 250, "seed": None,
                                               "rules": "official", "date": "2026-11-01T10:00:00"}) + "\n")
    played = history()
    assert [p.game for p in played] == ["solitaire", "sudoku", "sudoku", "yahtzee"]
    assert played[-1].seed is None and played[-1].score == 250
    games = {g["id"]: g for g in load_registry()}
    with pytest.raises(ValueError):
        play_args(games["sudoku"], 0, {})
    # Unknown setting values fall back to the default instead of reaching the game.
    assert play_args(games["sudoku"], 5, {"difficulty": "; rm -rf /"}) == ["--seed", "5", "--difficulty", "hard"]
