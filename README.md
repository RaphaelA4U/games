# Games

All terminal games in one retro library. Type `games` and every game sits on
a shelf as a cartridge: pick one with the arrows or the mouse, press enter,
and it starts right there in your terminal. Quit the game and you are back
at the shelf. Same style as the games themselves: your terminal's own
colors, everything in ASCII, no buttons and no dialogs.

| Game | |
|---|---|
| [Yahtzee](https://github.com/RaphaelA4U/yahtzee) | Dice against bots or friends, online, with a coach and a review. |
| [Solitaire](https://github.com/RaphaelA4U/solitaire) | Klondike with only winnable deals, hints, undo and a review. |
| [Sudoku](https://github.com/RaphaelA4U/sudoku) | Four levels, pencil notes and hints that explain the next step. |

## Install

One line installs the library and every game (macOS, Linux, WSL, iSH on iOS,
web/SSH terminals, Git Bash on Windows):

```sh
U=https://raw.githubusercontent.com/RaphaelA4U/games/main/install.sh; (curl -fsSL $U 2>/dev/null || wget -qO- $U) | sh
```

Native Windows (PowerShell):

```powershell
iwr -useb https://raw.githubusercontent.com/RaphaelA4U/games/main/install.ps1 | iex
```

Then open the library with `games`. `GAMES_ONLY=1` installs just the library;
missing games are then installed from the shelf the first time you play them.

Every game is still its own program with its own installer: `sudoku` starts
sudoku directly, and installing one game on its own keeps working. The library
uses each game's own installer, so both routes end up in the same place
(`~/.<game>`, command in `~/.local/bin`).

## Use

| Keys | |
|---|---|
| arrows / 1-9 | pick a game |
| enter (or double-click) | play; installs it first if needed |
| u | install or update the game |
| h | history: every game you played, of every game; enter plays the same seed again |
| s | play a seed: a game, a number and its settings (the same for everyone) |
| ? | help |
| q | quit |

```
games                     the library
games sudoku              start one game right away
games --list [--json]     the games and what is installed (JSON for BM Beheer)
games --install-all       install or update every game
```

The library and every game update themselves when they start, like Claude Code.

## Online (optional)

`f` in the library: pick a name and add friends by their handle. There is no
password and no email: the device keeps its own key and signs every request.
`l` shows leaderboards and `c` challenges (a race on the same seed, or "beat my
time"); only games the server replayed with the game's own `--verify` count.
How it works and what the server may and may not do: `docs/ONLINE.md`.

## The registry

`games_app/registry.json` is the one list of games. The library, its
installer, the games host of BM Beheer and beheer itself (chat battles: the
slash commands, the start commands, the race status) all read it. A game
there says how a chat battle runs:

- `match`: real multiplayer (`--host CODE` / `--join CODE`), like yahtzee.
  The host writes `saved_online.json` for beheer to read.
- `race`: everyone plays the same deal or puzzle (`--battle ID`), like
  solitaire and sudoku. The game tags its save and stats with the battle id and
  writes a `progress` count; beheer compares the players.

Every game also has a `play` block: its `--seed` flag, the settings that make
two games the same game, and its history file (`games.jsonl`). The library
uses it for the history and "play a seed"; see `docs/ONLINE.md` for the full
contract and the plans for friends, leaderboards and battles.

Adding a game = a new repo made the same way, its entry in the registry (with
`play`), and (for chat battles) the `--battle` or `--host/--join` flags in the game.

## The games host (BM Beheer)

`deploy/games-host` is the SSH container where beheer runs every player's
chat battle in tmux. Its init installs this library and every game; beheer
reads `games --list --json` there for the slash commands and start commands.
A new game in the registry shows up in beheer after the games host restarts
(the init then installs it).

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```
