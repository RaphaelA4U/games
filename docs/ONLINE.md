# Online: accounts, friends, leaderboards, battles

Status: design (2026-11-12). Phase 1 is built in games v1.2.0. Phases 2 to 4 wait for a host.

The games stay terminal games that work fully offline. Online is optional, not promoted (no website, no
listing), and built so that a stolen database or a hostile client cannot hurt anyone.

## Three layers

1. **Every game** follows a small contract (below) and knows nothing about friends or servers.
2. **The library `games`** is the client, like Steam: profile, friends, history over all games, play a
   seed, leaderboards, battles and challenges. Games never talk to the server; the library does. So a new
   game that follows the contract gets all of it for free, and every game stays installable on its own.
3. **The games server** (phase 2) stores players, friendships, results and challenges. One small public
   HTTPS API, nothing else.

## The game contract (phase 1)

Each game's entry in `games_app/registry.json` has a `play` block:

```json
"play": {
  "seed_flag": "--seed",
  "seed_max": 999999,
  "settings": [
    {"id": "difficulty", "label": "Difficulty", "flag": "--difficulty",
     "choices": ["beginner", "easy", "medium", "hard", "expert", "master"], "default": "hard"}
  ],
  "history": "games.jsonl",
  "result": {"time": "time", "score": null}
}
```

- `<game> --seed N <setting flags>` starts exactly that game. Same seed and settings = the same game for
  everyone, on every machine. A save of that same game resumes instead of starting over.
- Every game ever played is one line in `~/.config/<game>/games.jsonl` with at least `seed`, every setting
  id from `settings` as a top-level key, `won`, `date`, `time` and/or `score`, `version` and `log` (every
  move with its clock, enough to replay the game).
- Phase 3 adds `<game> --verify`: reads one record on stdin, replays the log, prints the real result.

## Identity without passwords (phase 2)

- `games` makes an **Ed25519 key pair** on first use of anything online: `~/.config/games/identity.key`,
  mode 0600. The account is the public key plus a random player id; you pick a handle (`raphael#4821`:
  a-z, 0-9, `_` and `-`, 3 to 16 characters, plus 4 random digits). No email, no password, no name.
- Every request is signed: method, path, sha256 of the body, a timestamp (max 60 s off) and a random
  nonce (remembered for 5 minutes, used once). The server only ever stores public keys, so a leaked
  database contains nothing to log in with. It is the passkey idea (a private key that never leaves the
  device, nothing to phish) for a terminal.
- A second device: the first one makes a one-time link code (valid 10 minutes, single use, signed).
  A recovery code is shown once at sign-up; the server keeps only its hash.
- Revoking a device or deleting the account (with all results) works from any linked device.

## Server rules (phase 2)

- Public HTTPS on 443 only, behind the reverse proxy with HSTS; the API answers JSON, never HTML, so
  there are no cookies, sessions, CSRF or XSS to defend. Database and admin only on localhost/tailnet.
- Closed by default: every route has an explicit rule (who, what), a test fails for a route without one.
- Strict input: JSON schema per route, max body 64 KB (a game log fits), handles and every other text
  checked against an allowlist. The client also strips control characters and Rich markup from
  everything it shows, so nobody can inject terminal escape codes through a handle.
- Rate limits per key and per IP, on everything. No search and no list of players: you add a friend by
  their exact handle or friend code, so accounts cannot be enumerated. Block and unfriend always work.
- Results only count on a leaderboard after `--verify` replayed them in a sandbox without network,
  with a CPU and time limit (phase 3). Unverified results stay private.
- Security events (blocked signatures, rate limits, odd results) go to beheer
  (`/api/ai/dienst/melding`), never blocking.
- Logs keep no IP addresses longer than 7 days. Dependencies audited (`pip-audit`), secrets never in git.

## Phases

1. **Without a server**: the contract in every game, history over all games and "play seed" in the
   library, and the beheer terminal made to look right.
2. **Accounts and friends**: the server, `games` profile and friends screens.
3. **Leaderboards**: upload, verify, per game and setting, per seed, friends and everyone.
4. **Battles and challenges**: invite a friend to a race (seed or random, same settings for everyone),
   or "beat my time" on a seed you finished. Beheer chat battles keep working as they do and gain the
   same seed and settings choices.

Open: the host and domain for the server (needs a public 443; not beheer, not the tailnet-only relay).
