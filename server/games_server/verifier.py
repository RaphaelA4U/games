"""The verifier: replays uploaded results with the games' own `--verify`.

It runs in its own container WITHOUT a network, next to the server, on the same
database file. Each record is the only input of one `python -m <game>_app --verify`
run with a CPU, memory and time limit, an empty home and an empty environment.
A result only counts (status 'ok') when the replay says so; the values that
count (won, time, score, moves) are the replay's, never the uploader's.
"""

from __future__ import annotations

import json
import os
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time

PACKAGES = {"yahtzee": "yahtzee_app", "solitaire": "solitaire_app", "sudoku": "sudoku_app"}
TIMEOUT = 20


def _limits() -> None:  # runs in the child before exec
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))   # may not write any file
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    os.setsid()


def run_verify(game: str, record: str, runner=None) -> dict:
    """One replay. Returns the game's JSON answer, or ok False with a reason."""
    if runner is not None:
        return runner(game, record)
    package = PACKAGES.get(game)
    if package is None:
        return {"ok": False, "reason": "unknown game"}
    with tempfile.TemporaryDirectory() as home:
        env = {"HOME": home, "PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
               "LANG": "C.UTF-8"}
        try:
            proc = subprocess.run([sys.executable, "-I", "-m", package, "--verify"], input=record.encode(),
                                  capture_output=True, timeout=TIMEOUT, env=env, cwd=home, preexec_fn=_limits)
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": "replay took too long"}
    lines = proc.stdout.decode(errors="replace").strip().splitlines()
    try:
        answer = json.loads(lines[-1]) if lines else {}
    except ValueError:
        answer = {}
    if not isinstance(answer, dict) or "ok" not in answer:
        return {"ok": False, "reason": "replay gave no answer"}
    if proc.returncode != 0:
        answer["ok"] = False
    return answer


def settings_of(answer: dict) -> dict:
    return {str(k): str(v) for k, v in (answer.get("settings") or {}).items()}


def verify_pending(db: sqlite3.Connection, runner=None, batch: int = 20) -> int:
    rows = db.execute("SELECT id, game, seed, settings, record FROM results WHERE status = 'pending' "
                      "ORDER BY id LIMIT ?", (batch,)).fetchall()
    for row in rows:
        answer = run_verify(row["game"], row["record"], runner)
        ok = bool(answer.get("ok"))
        reason = str(answer.get("reason") or "")[:200]
        # The replay must be about the same game the row is filed under.
        stored = dict(part.split("=", 1) for part in row["settings"].split(";") if "=" in part)
        replayed = settings_of(answer)
        if ok and (answer.get("game") != row["game"] or answer.get("seed") != row["seed"]
                   or any(replayed.get(k) != v for k, v in stored.items())):
            ok, reason = False, "replay is about another game"

        def num(key: str):
            value = answer.get(key)
            return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        db.execute("UPDATE results SET status = ?, reason = ?, won = ?, time = ?, score = ?, moves = ?, verified = ? "
                   "WHERE id = ?", ("ok" if ok else "rejected", reason, 1 if ok and answer.get("won") else 0,
                                    num("time") if ok else None, num("score") if ok else None,
                                    num("moves") if ok else None, int(time.time()), row["id"]))
    return len(rows)


def main() -> None:
    path = os.environ.get("GAMES_DB", "/data/games.db")
    while not os.path.exists(path):
        time.sleep(2)
    db = sqlite3.connect(path, isolation_level=None, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 30000")
    while True:
        try:
            if verify_pending(db) == 0:
                time.sleep(3)
        except sqlite3.Error:
            time.sleep(5)


if __name__ == "__main__":
    main()
