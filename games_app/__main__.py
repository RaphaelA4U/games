"""Entry point: `games` or `python -m games_app`."""

from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="games",
        description="All terminal games in one retro library. Run without arguments for the shelf.",
    )
    parser.add_argument("game", nargs="?", help="start this game right away (for example: games sudoku)")
    parser.add_argument("-V", "--version", action="store_true", help="show the version")
    parser.add_argument("--list", action="store_true", help="list the games and what is installed")
    parser.add_argument("--json", action="store_true", help="with --list: machine-readable (for BM Beheer)")
    parser.add_argument("--install-all", action="store_true", help="install (or update) every game")
    parser.add_argument("--install-missing", action="store_true", help="install the games that are missing")
    parser.add_argument("--update-all", action="store_true",
                        help="update the library and every installed game now (a games host does this hourly)")
    parser.add_argument("--no-boot", action="store_true", help="skip the start-up screen")
    parser.add_argument("--no-update", action="store_true", help="skip the update check")
    args = parser.parse_args()

    if args.version:
        from . import __version__

        print(f"games v{__version__}")
        return

    from .library import install_all, list_games, load_registry, game_command, update_all, update_all_in_background

    if args.update_all:
        for line in update_all():
            print(line)
        return

    if args.list:
        games = list_games()
        if args.json:
            print(json.dumps({"games": games}, ensure_ascii=False))
            # BM Beheer asks this every few minutes: keep that games host current, in the background.
            update_all_in_background()
        else:
            for g in games:
                state = f"v{g['version']}" if g["version"] else ("installed" if g["installed"] else "not installed")
                print(f"{g['id']:<12} {state:<14} {g.get('tagline', '')}")
        return
    if args.install_all or args.install_missing:
        sys.exit(1 if install_all(only_missing=args.install_missing) else 0)

    # Claude-style eager update: pull before the UI starts, then re-exec.
    if not args.no_update:
        from .update import quick_sync_update

        if quick_sync_update():
            os.execv(sys.executable, [sys.executable, "-m", "games_app", *sys.argv[1:], "--no-update"])

    if args.game:
        games = {g["id"]: g for g in load_registry()}
        game = games.get(args.game.lower())
        if game is None:
            parser.error(f"unknown game: {args.game} (choose from {', '.join(games)})")
        cmd = game_command(game)
        if not cmd:
            from .library import install

            if install(game) != 0 or not (cmd := game_command(game)):
                sys.exit(f"{game['name']} could not be installed.")
        os.execv(cmd, [cmd])

    from .ui.app import run

    run(no_update=args.no_update, boot=not args.no_boot)


if __name__ == "__main__":
    main()
