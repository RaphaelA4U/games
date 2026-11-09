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
