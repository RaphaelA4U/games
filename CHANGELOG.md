# Changelog

## v1.4.0 (2026-11-12)
- Leaderboards (`l`): the best results per game and settings, for your friends or everyone, over all seeds or one seed. Only games the server replayed count: each finished game is sent with its moves, and the server plays it again with the game's own `--verify` before it counts. A changed time or score is refused.
- Challenges (`c`): `c` on a friend in friends starts a race (the same seed and settings for both); `c` on one of your wins in history asks friends to beat it. Enter on a challenge plays it; who is ahead shows next to it.
- Your finished games go to the server in the background once you are online; "Share my games" in friends turns that off.
- In history, `l` shows the leaderboard of that one seed.

## v1.3.1 (2026-11-12)
- A long message at the bottom wraps in line with its first line.

## v1.3.0 (2026-11-12)
- Friends (`f`), optional and online: pick a name and you get a handle like `anna#1234`; add friends by their exact handle, accept or decline requests, remove or block. Link a second device with a one-time code; a recovery code (shown once) gets the account back if every device is lost.
- No password and no email: this device has its own key (`~/.config/games/identity.key`, only readable by you) and signs every request. The server only ever sees the public half.
- The server itself lives in `server/` (one small JSON API, see `docs/ONLINE.md`).

## v1.2.0 (2026-11-12)
- History (`h`): every game you played, of every game, newest first, with its seed, settings and result. Enter plays the same seed with the same settings again.
- Play a seed (`s`): pick a game, type a number (or `r` for a random one) and choose the settings. The same seed and settings are the same game for everyone.
- Every game in the registry has a `play` block (seed flag, settings, history file), the contract for friends, leaderboards and battles later (`docs/ONLINE.md`).

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
