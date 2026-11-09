"""The games library: a retro console for all terminal games, Claude-terminal
style (the same rules as the games themselves).

- The terminal's own colors: default background and foreground, purple as
  the focus accent, cyan for good news (an update), never yellow.
- Everything is text and ASCII. No buttons, no dialogs, no popups: games sit
  on a shelf as cartridges; arrows or the mouse pick one, enter plays it.
- Playing suspends the library and runs the game in the same terminal;
  quitting the game brings you back to the shelf.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Callable, Optional

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static

from .. import __version__
from ..library import (
    ago,
    game_command,
    game_stats,
    install_command,
    install_env,
    installed_version,
    latest_version,
    load_registry,
    newer,
)
from ..update import REPO_DIR, check_and_update

ACCENT = "#875fff"   # Claude-style purple, leaning slightly blue
WINC = "#4dd0e1"     # cyan: good news (an update is ready)

# figlet "DOS Rebel"
LOGO = """\
  ███████  ██████   █████████████    ██████   █████
 ███░░███ ░░░░░███ ░░███░░███░░███  ███░░███ ███░░
░███ ░███  ███████  ░███ ░███ ░███ ░███████ ░░█████
░███ ░███ ███░░███  ░███ ░███ ░███ ░███░░░   ░░░░███
░░███████░░████████ █████░███ █████░░██████  ██████
 ░░░░░███ ░░░░░░░░ ░░░░░ ░░░ ░░░░░  ░░░░░░  ░░░░░░
 ███ ░███
░░██████
 ░░░░░░"""

CART_W = 18
CART_H = 9
CART_GAP = 3
BOOT_SECONDS = 1.6


class Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.w = max(1, width)
        self.h = max(1, height)
        self.chars = [[" "] * self.w for _ in range(self.h)]
        self.styles = [[""] * self.w for _ in range(self.h)]

    def put(self, x: int, y: int, text: str, style: str = "") -> None:
        if not 0 <= y < self.h:
            return
        row_c, row_s = self.chars[y], self.styles[y]
        for i, ch in enumerate(text):
            xx = x + i
            if 0 <= xx < self.w:
                row_c[xx] = ch
                row_s[xx] = style

    def to_text(self) -> Text:
        out = Text(no_wrap=True, overflow="crop")
        for y in range(self.h):
            buf, run = "", None
            for ch, st in zip(self.chars[y], self.styles[y]):
                if st != run:
                    if buf:
                        out.append(buf, style=run or "")
                    buf, run = "", st
                buf += ch
            if buf:
                out.append(buf.rstrip() if not run else buf, style=run or "")
            if y < self.h - 1:
                out.append("\n")
        return out


def draw_cart(cv: Canvas, x: int, y: int, game: dict, state: str, selected: bool, hovered: bool) -> None:
    """A cartridge: a label window with the cover art, the name, a status line."""
    installed = state != "not installed"
    frame = f"bold {ACCENT}" if selected else ("bold" if hovered else ("" if installed else "dim"))
    inner = CART_W - 2
    cv.put(x, y, "." + "-" * inner + ".", frame)
    cv.put(x, y + 1, "| .------------. |", frame)
    cover = list(game.get("cover") or [])[:3]
    while len(cover) < 3:
        cover.append("")
    for r, line in enumerate(cover):
        cv.put(x, y + 2 + r, "| |", frame)
        cv.put(x + 3, y + 2 + r, f"{line[:12]:^12}", "bold" if installed else "dim")
        cv.put(x + 15, y + 2 + r, "| |", frame)
    cv.put(x, y + 5, "| '------------' |", frame)
    cv.put(x, y + 6, "|", frame)
    cv.put(x + 1, y + 6, f"{game['name'].upper()[:inner]:^{inner}}",
           f"bold {ACCENT}" if selected else ("bold" if installed else "dim"))
    cv.put(x + CART_W - 1, y + 6, "|", frame)
    cv.put(x, y + 7, "|", frame)
    style = {"update": f"bold {WINC}", "not installed": "dim"}.get(state, "dim")
    cv.put(x + 1, y + 7, f"{state:^{inner}}", style)
    cv.put(x + CART_W - 1, y + 7, "|", frame)
    cv.put(x, y + 8, "'--==" + "-" * (inner - 8) + "==--'", frame)


class ActionBar(Static):
    """Footer key hints; the ones bound to an action are clickable."""

    hovered_idx: reactive[Optional[int]] = reactive(None)

    class Invoked(Message):
        def __init__(self, action: str) -> None:
            self.action = action
            super().__init__()

    def __init__(self, segments: list, id: Optional[str] = None) -> None:
        super().__init__(id=id)
        self.segments = segments
        self._ranges: list[tuple[int, int, int]] = []

    def render(self) -> Text:
        out = Text(" ", no_wrap=True)
        self._ranges = []
        for idx, (label, action) in enumerate(self.segments):
            start = len(out.plain)
            if action is None:
                out.append(label, style="dim")
            elif idx == self.hovered_idx:
                out.append(label, style=f"bold underline {ACCENT}")
            else:
                out.append(label)
            self._ranges.append((start, len(out.plain), idx))
            out.append("   ")
        return out

    def _idx_at(self, x: int) -> Optional[int]:
        for start, end, idx in self._ranges:
            if start <= x < end and self.segments[idx][1] is not None:
                return idx
        return None

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self.hovered_idx = self._idx_at(event.x)

    def on_leave(self) -> None:
        self.hovered_idx = None

    def on_click(self, event: events.Click) -> None:
        idx = self._idx_at(event.x)
        if idx is not None:
            self.post_message(self.Invoked(self.segments[idx][1]))

    def watch_hovered_idx(self, _) -> None:
        self.refresh()


class TextPage(Screen):
    BINDINGS = [Binding("escape,q,backspace", "go_back", "Back")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        yield Static(f" {self.title_text}", id="page-title", markup=True)
        with VerticalScroll(id="page-scroll"):
            yield Static(self.body, id="page-body", markup=True)
        yield ActionBar([("up/down scroll", None), ("esc back", "back")], id="page-footer")

    def on_mount(self) -> None:
        self.query_one("#page-scroll", VerticalScroll).focus()

    def on_action_bar_invoked(self, event: ActionBar.Invoked) -> None:
        if event.action == "back":
            self.app.pop_screen()

    def action_go_back(self) -> None:
        self.app.pop_screen()


HELP_TEXT = f"""[b]GAMES v{__version__}[/b]  [dim]all terminal games in one place[/dim]

[b u]The shelf[/b u]
  Every game is a cartridge. Pick one with the arrows or the mouse and press
  [b]enter[/b] (or double-click) to play. Quit the game to come back here.
  A game that is not installed yet is installed first.

[b u]Keys[/b u]
  [b]arrows[/b]     pick a game          [b]1-9[/b]  pick game 1-9
  [b]enter[/b]      play (installs it first if needed)
  [b]u[/b]          install or update the game
  [b]?[/b]          this help            [b]q[/b]    quit

[b u]Each game on its own[/b u]
  Every game is also its own program: type its name (for example `sudoku`)
  to play it without the library, or `games sudoku`. Installing one game on
  its own keeps working; the library just uses each game's own installer.

[b u]Updates[/b u]
  The library and every game update themselves when they start. A cyan
  "update" on a cartridge means a newer version is out; [b]u[/b] installs it now.
"""


def changelog_summary(version: str, max_lines: int = 6) -> Optional[str]:
    path = Path(REPO_DIR) / "CHANGELOG.md"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    out: list[str] = []
    in_section = False
    for line in lines:
        if line.startswith("## "):
            if in_section:
                break
            in_section = version in line
            continue
        if in_section and line.strip():
            out.append(line.strip().lstrip("- "))
            if len(out) >= max_lines:
                break
    return "\n".join(out) if out else None


class BootScreen(Screen):
    """A short power-on screen. Any key or click skips it."""

    blink: reactive[bool] = reactive(True)

    def compose(self) -> ComposeResult:
        yield Static("", id="boot")

    def on_mount(self) -> None:
        self._started = time.monotonic()
        self.set_interval(0.4, self._tick)
        self._draw()

    def _draw(self) -> None:
        w, h = self.size.width or 80, self.size.height or 24
        cv = Canvas(w, h)
        logo = LOGO.splitlines()
        lw = max(len(l) for l in logo)
        big = w >= lw + 4 and h >= len(logo) + 8
        lines = logo if big else ["G A M E S"]
        top = max(0, (h - len(lines) - 6) // 2)
        for i, line in enumerate(lines):
            cv.put(max(0, (w - (lw if big else len(line))) // 2), top + i, line, "bold")
        n = len(load_registry())
        sub = f"{n} games  .  v{__version__}"
        cv.put(max(0, (w - len(sub)) // 2), top + len(lines) + 2, sub, "dim")
        if self.blink:
            msg = "PRESS ANY KEY"
            cv.put(max(0, (w - len(msg)) // 2), top + len(lines) + 4, msg, f"bold {ACCENT}")
        self.query_one("#boot", Static).update(cv.to_text())

    def _tick(self) -> None:
        self.blink = not self.blink
        self._draw()
        if time.monotonic() - self._started >= BOOT_SECONDS:
            self._done()

    def on_resize(self) -> None:
        self._draw()

    def on_key(self, event: events.Key) -> None:
        event.stop()
        self._done()

    def on_click(self) -> None:
        self._done()

    def _done(self) -> None:
        if self.app.screen is self:
            self.app.switch_screen(LibraryScreen())


class Shelf(Widget, can_focus=True):
    """The cartridges, with mouse hit-testing."""

    class Picked(Message):
        def __init__(self, index: int, play: bool) -> None:
            self.index = index
            self.play = play
            super().__init__()

    def __init__(self, games: list[dict], id: Optional[str] = None) -> None:
        super().__init__(id=id)
        self.games = games
        self.selected = 0
        self.hover: Optional[int] = None
        self.states: dict[str, str] = {}
        self._rects: list[tuple[int, int, int, int, int]] = []
        self._last_click = (0.0, -1)

    def per_row(self) -> int:
        return max(1, (self.size.width + CART_GAP) // (CART_W + CART_GAP))

    def get_content_height(self, container, viewport, width) -> int:
        per = max(1, (width + CART_GAP) // (CART_W + CART_GAP))
        rows = (len(self.games) + per - 1) // per
        return rows * (CART_H + 1)

    def render(self) -> Text:
        w = self.size.width or 80
        per = self.per_row()
        rows = (len(self.games) + per - 1) // per
        cv = Canvas(w, rows * (CART_H + 1))
        self._rects = []
        for i, game in enumerate(self.games):
            r, c = divmod(i, per)
            in_row = min(per, len(self.games) - r * per)
            row_w = in_row * CART_W + (in_row - 1) * CART_GAP
            x = max(0, (w - row_w) // 2) + c * (CART_W + CART_GAP)
            y = r * (CART_H + 1)
            draw_cart(cv, x, y, game, self.states.get(game["id"], "..."),
                      selected=i == self.selected and self.has_focus, hovered=i == self.hover)
            self._rects.append((x, y, x + CART_W, y + CART_H, i))
        return cv.to_text()

    def _hit(self, x: int, y: int) -> Optional[int]:
        for x0, y0, x1, y1, i in self._rects:
            if x0 <= x < x1 and y0 <= y < y1:
                return i
        return None

    def on_mouse_move(self, event: events.MouseMove) -> None:
        hit = self._hit(event.x, event.y)
        if hit != self.hover:
            self.hover = hit
            self.refresh()

    def on_leave(self) -> None:
        self.hover = None
        self.refresh()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.focus()
        hit = self._hit(event.x, event.y)
        if hit is None:
            return
        now = time.monotonic()
        double = self._last_click[1] == hit and now - self._last_click[0] < 0.4
        self._last_click = (now, hit)
        self.selected = hit
        self.refresh()
        self.post_message(self.Picked(hit, play=double))

    def move(self, dx: int, dy: int) -> None:
        per = self.per_row()
        i = self.selected + dx + dy * per
        if 0 <= i < len(self.games):
            self.selected = i
        elif dx:
            self.selected = i % len(self.games)
        self.refresh()
        self.post_message(self.Picked(self.selected, play=False))

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()


FOOTER = [
    ("arrows pick", None),
    ("enter play", "play"),
    ("u install/update", "update"),
    ("? help", "help"),
    ("q quit", "quit"),
]


class LibraryScreen(Screen):
    BINDINGS = [
        Binding("left", "move(-1, 0)", show=False),
        Binding("right", "move(1, 0)", show=False),
        Binding("up", "move(0, -1)", show=False),
        Binding("down", "move(0, 1)", show=False),
        Binding("enter,space", "play", show=False),
        Binding("u", "update", show=False),
        Binding("question_mark,f1", "help", show=False),
        Binding("q,escape,ctrl+q", "quit_app", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.games = load_registry()
        self.latest: dict[str, Optional[str]] = {}

    def compose(self) -> ComposeResult:
        yield Static("", id="lib-title")
        yield Shelf(self.games, id="shelf")
        yield Static("", id="details", markup=True)
        yield Static("", id="lib-note", markup=True)
        yield ActionBar(FOOTER, id="lib-footer")

    @property
    def shelf(self) -> Shelf:
        return self.query_one(Shelf)

    def on_mount(self) -> None:
        self.shelf.focus()
        self.refresh_states()
        self._check_versions()

    def on_screen_resume(self) -> None:
        self.refresh_states()

    def on_resize(self) -> None:
        self._draw_title()

    def _draw_title(self) -> None:
        w = self.size.width or 80
        left = " GAMES "
        right = f" v{__version__} "
        bar = Text()
        bar.append("+-", style="dim")
        bar.append(left, style=f"bold {ACCENT}")
        bar.append("-" * max(1, w - len(left) - len(right) - 4), style="dim")
        bar.append(right, style="dim")
        bar.append("-+", style="dim")
        self.query_one("#lib-title", Static).update(bar)

    def state_of(self, game: dict) -> str:
        if not game_command(game):
            return "not installed"
        if newer(self.latest.get(game["id"]), installed_version(game)):
            return "update"
        v = installed_version(game)
        return f"v{v}" if v else "installed"

    def refresh_states(self) -> None:
        shelf = self.shelf
        shelf.states = {g["id"]: self.state_of(g) for g in self.games}
        shelf.refresh()
        self._draw_title()
        self.show_details()

    @work(thread=True, exclusive=True, group="versions")
    def _check_versions(self) -> None:
        for game in self.games:
            v = latest_version(game)
            self.app.call_from_thread(self._got_latest, game["id"], v)

    def _got_latest(self, game_id: str, version: Optional[str]) -> None:
        self.latest[game_id] = version
        self.refresh_states()

    def show_details(self) -> None:
        game = self.games[self.shelf.selected]
        state = self.state_of(game)
        st = game_stats(game)
        width = max(40, min(100, (self.size.width or 80) - 8))
        lines = [f"  [bold {ACCENT}]{game['name']}[/]  [dim]{game.get('tagline', '')}[/dim]"]
        lines += ["  " + l for l in textwrap.wrap(game.get("description", ""), width)]
        info = f"Players {game.get('players', '1')}"
        if state == "not installed":
            info += "   [dim]not installed yet: enter installs and plays it[/dim]"
        else:
            info += f"   {state}"
            if state == "update":
                info += f" [bold {WINC}]v{self.latest.get(game['id'])} is out[/] [dim](u installs it)[/dim]"
        lines.append("  " + info)
        if st.played:
            best_label = (game.get("stats") or {}).get("best_label", "best")
            extra = f"   {best_label} {st.best}" if st.best else ""
            lines.append(f"  Played {st.played}   won {st.won}{extra}   last played {ago(st.last_played)}")
        self.query_one("#details", Static).update("\n".join(lines))

    def note(self, text: str) -> None:
        self.query_one("#lib-note", Static).update(f" {text}")

    # -- actions ---------------------------------------------------------
    def action_move(self, dx: int, dy: int) -> None:
        self.shelf.move(dx, dy)

    async def on_key(self, event: events.Key) -> None:
        ch = event.character
        if ch and len(ch) == 1 and ch.isdigit() and ch != "0":
            i = int(ch) - 1
            if i < len(self.games):
                event.stop()
                self.shelf.selected = i
                self.shelf.refresh()
                self.show_details()

    @on(Shelf.Picked)
    def _picked(self, event: Shelf.Picked) -> None:
        self.show_details()
        if event.play:
            self.action_play()

    def on_action_bar_invoked(self, event: ActionBar.Invoked) -> None:
        {"play": self.action_play, "update": self.action_update, "help": self.action_help,
         "quit": self.action_quit_app}.get(event.action, lambda: None)()

    def action_play(self) -> None:
        game = self.games[self.shelf.selected]
        app = self.app
        assert isinstance(app, GamesApp)
        if not game_command(game):
            if app.run_external(install_command(game), install_env(game), pause=True) != 0:
                self.note(f"[red]{game['name']} did not install.[/] Check your internet and try again.")
                self.refresh_states()
                return
        cmd = game_command(game)
        if not cmd:
            self.note(f"[red]{game['name']} is not available.[/]")
            return
        app.run_external([cmd], dict(os.environ), pause=False)
        self.note(f"Back from {game['name']}. Pick the next one.")
        self.refresh_states()

    def action_update(self) -> None:
        game = self.games[self.shelf.selected]
        app = self.app
        assert isinstance(app, GamesApp)
        ok = app.run_external(install_command(game), install_env(game), pause=True) == 0
        self.note(f"{game['name']} is up to date." if ok else f"[red]{game['name']} did not update.[/]")
        self.latest.pop(game["id"], None)
        self.refresh_states()
        self._check_versions()

    def action_help(self) -> None:
        self.app.push_screen(TextPage("Help", HELP_TEXT))

    def action_quit_app(self) -> None:
        self.app.exit()


class GamesApp(App):
    TITLE = f"Games v{__version__}"

    CSS = """
    Screen { background: ansi_default; color: ansi_default; }
    #boot { width: 1fr; height: 1fr; }
    #lib-title { height: 1; }
    Shelf { width: 1fr; height: auto; margin-top: 2; }
    #details { height: auto; margin-top: 1; }
    #lib-note { height: 1fr; content-align: left bottom; }
    #lib-footer { height: 1; color: ansi_bright_black; }
    #page-title { height: 1; text-style: bold; }
    #page-scroll {
        height: 1fr; scrollbar-size-vertical: 1;
        scrollbar-background: ansi_default; scrollbar-color: ansi_bright_black;
    }
    #page-body { padding: 1 2; }
    #page-footer { height: 1; color: ansi_bright_black; }
    """

    def __init__(self, no_update: bool = False, boot: bool = True,
                 runner: Optional[Callable[[list[str], dict, bool], int]] = None) -> None:
        super().__init__(ansi_color=True)
        self.no_update = no_update
        self.boot = boot
        self._runner = runner
        self._restart_args: Optional[list[str]] = None

    def on_mount(self) -> None:
        self.push_screen(BootScreen() if self.boot else LibraryScreen())
        self._whats_new()
        if not self.no_update:
            self._update_check()

    def run_external(self, cmd: list[str], env: dict, pause: bool) -> int:
        """Run a game or an installer in this terminal while the library waits."""
        if self._runner is not None:
            return self._runner(cmd, env, pause)
        with self.suspend():
            rc = subprocess.call(cmd, env=env)
            if pause:
                try:
                    input("\n  Press enter to go back to the library... ")
                except EOFError:
                    pass
        return rc

    def _whats_new(self) -> None:
        from ..config import load_settings, save_settings, SETTINGS_FILE

        first = not SETTINGS_FILE.exists()
        settings = load_settings()
        if settings.get("last_seen_version") == __version__:
            return
        settings["last_seen_version"] = __version__
        save_settings(settings)
        if not first:
            summary = changelog_summary(__version__)
            if summary:
                self.notify(summary, title=f"What's new in v{__version__}", timeout=12)

    @work(thread=True, exclusive=True, group="update")
    def _update_check(self) -> None:
        result = check_and_update()
        if result.status == "updated":
            self.call_from_thread(
                self.notify, f"The library was updated to v{result.new_version or '?'}; "
                "it applies next time you start it.", title="Update", timeout=8)


def run(no_update: bool = False, boot: bool = True) -> None:
    app = GamesApp(no_update=no_update, boot=boot)
    app.run()
    if app._restart_args is not None:
        os.execv(sys.executable, [sys.executable, "-m", "games_app", *app._restart_args])
