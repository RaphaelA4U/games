# Changelog

## v1.1.2 (2026-11-11)
- The sudoku description says six difficulties (beginner to master), as in sudoku v1.4.0.

## v1.1.1 (2026-11-09)
- `.env` files are ignored by git, so the games host password can never end up in the repo.

## v1.1.0 (2026-11-09)
- The games host of BM Beheer lives here now (`deploy/games-host`): it installs the library and every game, and beheer reads the list of games from it.

## v1.0.0 (2026-11-09)
- One library for all terminal games: a retro shelf of cartridges with each game's version, update and your stats.
- Enter plays a game in the same terminal; quitting it brings you back to the shelf.
- The one-line installer installs the library and every game, each through its own installer.
- `games --list --json` and the registry are the single source for BM Beheer's chat battles.
