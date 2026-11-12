"""The games server: players, devices and friends (docs/ONLINE.md, phase 2).

One small JSON API, nothing else: no website, no cookies, no sessions. Every
request is signed by a device key (Ed25519); the server only stores public
keys, so its database holds nothing anyone can log in with.

Signature: header `Authorization: Games key=<pubkey>, ts=<unix>, nonce=<16 bytes>, sig=<sig>`
(all base64url without padding) over the message
    METHOD \\n PATH?QUERY \\n ts \\n nonce \\n sha256(body) hex
A timestamp more than 60 s off or a nonce seen before (per key, 10 min) is refused.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
from typing import Any, Optional

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

MAX_BODY = 64 * 1024
CLOCK_SKEW = 60
NONCE_TTL = 600
NAME_RE = re.compile(r"^[a-z0-9_-]{3,16}$")
HANDLE_RE = re.compile(r"^([a-z0-9_-]{3,16})#(\d{4})$")
ID_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_FRIENDS = 200
MAX_PENDING = 50
MAX_DEVICES = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, tag TEXT NOT NULL, created INTEGER NOT NULL,
    recovery_hash TEXT NOT NULL, UNIQUE (name, tag));
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY, player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    pubkey BLOB NOT NULL UNIQUE, label TEXT NOT NULL, created INTEGER NOT NULL, last_seen INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS nonces (
    pubkey BLOB NOT NULL, nonce TEXT NOT NULL, ts INTEGER NOT NULL, PRIMARY KEY (pubkey, nonce));
CREATE TABLE IF NOT EXISTS friend_requests (
    id INTEGER PRIMARY KEY, from_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    to_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE, created INTEGER NOT NULL,
    UNIQUE (from_id, to_id));
CREATE TABLE IF NOT EXISTS friends (
    a TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    b TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE, since INTEGER NOT NULL, PRIMARY KEY (a, b));
CREATE TABLE IF NOT EXISTS blocks (
    blocker TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    blocked TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE, created INTEGER NOT NULL,
    PRIMARY KEY (blocker, blocked));
CREATE TABLE IF NOT EXISTS link_codes (
    code_hash TEXT PRIMARY KEY, player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rate (bucket TEXT NOT NULL, window INTEGER NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY (bucket, window));
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY, player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    game TEXT NOT NULL, seed INTEGER NOT NULL, settings TEXT NOT NULL, record TEXT NOT NULL,
    record_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
    won INTEGER, time INTEGER, score INTEGER, moves INTEGER, played TEXT, created INTEGER NOT NULL,
    verified INTEGER, UNIQUE (player_id, record_hash));
CREATE INDEX IF NOT EXISTS results_board ON results (game, settings, seed, status);
CREATE INDEX IF NOT EXISTS results_pending ON results (status, id);
CREATE TABLE IF NOT EXISTS challenges (
    id INTEGER PRIMARY KEY, from_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    game TEXT NOT NULL, seed INTEGER NOT NULL, settings TEXT NOT NULL, kind TEXT NOT NULL,
    target_result INTEGER REFERENCES results(id) ON DELETE SET NULL, created INTEGER NOT NULL,
    expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS challenge_players (
    challenge_id INTEGER NOT NULL REFERENCES challenges(id) ON DELETE CASCADE,
    player_id TEXT NOT NULL REFERENCES players(id) ON DELETE CASCADE, status TEXT NOT NULL,
    PRIMARY KEY (challenge_id, player_id));
"""

# (bucket, max requests, window in seconds)
LIMITS = {
    "ip": (300, 60),
    "player": (120, 60),
    "register": (5, 3600),
    "link": (10, 3600),
    "recover": (5, 3600),
    "friend_request": (30, 86400),
    "bad_sig": (20, 3600),
    "results": (300, 86400),
    "challenges": (50, 86400),
}
MAX_SEED = 999999
CHALLENGE_DAYS = 7


def load_games(path: Optional[str] = None) -> dict:
    """The games and their settings from the library's registry.json (the one list of games)."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [path, os.environ.get("GAMES_REGISTRY"), os.path.join(here, "registry.json"),
                  os.path.join(here, "..", "..", "games_app", "registry.json")]
    for candidate in filter(None, candidates):
        if os.path.exists(candidate):
            with open(candidate, encoding="utf-8") as fh:
                data = json.load(fh)
            out = {}
            for g in data.get("games", []):
                play = g.get("play") or {}
                result = play.get("result") or {}
                out[g["id"]] = {
                    # Only the settings that make two games the same game for a ranking.
                    "settings": {x["id"]: [str(c) for c in x["choices"]] for x in play.get("settings", [])
                                 if x.get("ranked", True)},
                    "defaults": {x["id"]: str(x["default"]) for x in play.get("settings", [])
                                 if x.get("ranked", True)},
                    # Time games rank the fastest win, score games the highest score.
                    "metric": "time" if result.get("time") else "score",
                }
            return out
    raise RuntimeError("registry.json not found")


def settings_key(settings: dict) -> str:
    return ";".join(f"{k}={settings[k]}" for k in sorted(settings))


class Fail(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def b64d(text: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", text or ""):
        raise Fail(401, "bad signature")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def now() -> int:
    return int(time.time())


class Store:
    """SQLite, one connection per thread, foreign keys on, WAL."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.local = threading.local()
        with self.db() as db:
            db.executescript(SCHEMA)

    def db(self) -> sqlite3.Connection:
        conn = getattr(self.local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            self.local.conn = conn
        return conn


class tx:
    """BEGIN ... COMMIT, or ROLLBACK when anything inside fails."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def __enter__(self) -> sqlite3.Connection:
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        self.db.execute("ROLLBACK" if exc_type else "COMMIT")


def report(kind: str, severity: str, details: dict) -> None:
    """A security event for beheer (/api/ai/dienst/melding). Never blocks, never fails."""
    url, token = os.environ.get("BEHEER_MELDING_URL"), os.environ.get("BEHEER_MELDING_TOKEN")
    if not url or not token:
        return

    def send() -> None:
        small = details if len(json.dumps(details)) < 3500 else {"note": "details too long"}
        body = json.dumps({"soort": kind, "ernst": severity, "details": dict(small, app="games.rustema.app"),
                           "tijd": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            urllib.request.urlopen(req, timeout=2).close()
        except Exception:  # noqa: BLE001 - a report must never break the request
            pass

    threading.Thread(target=send, daemon=True).start()


class SafeHeaders:
    """The same safe headers on every answer (pure ASGI, no buffering)."""

    HEADERS = [(b"x-content-type-options", b"nosniff"), (b"cache-control", b"no-store"),
               (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
               (b"referrer-policy", b"no-referrer"),
               (b"strict-transport-security", b"max-age=31536000; includeSubDomains")]

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [h for h in message["headers"] if h[0] != b"server"] + self.HEADERS
            await send(message)

        await self.app(scope, receive, with_headers)


def create_app(db_path: Optional[str] = None, trusted_proxies: Optional[set[str]] = None,
               registry: Optional[str] = None) -> Starlette:
    store = Store(db_path or os.environ.get("GAMES_DB", "/data/games.db"))
    games = load_games(registry)
    proxies = trusted_proxies if trusted_proxies is not None else set(
        filter(None, os.environ.get("TRUSTED_PROXIES", "127.0.0.1").split(",")))

    # -- helpers -------------------------------------------------------------
    def client_ip(request: Request) -> str:
        """The real client: walk X-Forwarded-For from the right, past our own proxies
        only. A client can put anything in that header, but not to the right of
        what our proxies add."""
        peer = request.client.host if request.client else "?"
        if peer not in proxies:
            return peer
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        for hop in reversed(hops):
            if hop not in proxies:
                return hop[:64]
        return peer

    def hit(bucket: str, key: str) -> None:
        limit, window = LIMITS[bucket]
        slot = now() // window
        name = f"{bucket}:{hashlib.sha256(key.encode()).hexdigest()[:24]}"
        db = store.db()
        db.execute("INSERT INTO rate (bucket, window, count) VALUES (?, ?, 1) "
                   "ON CONFLICT (bucket, window) DO UPDATE SET count = count + 1", (name, slot))
        count = db.execute("SELECT count FROM rate WHERE bucket = ? AND window = ?", (name, slot)).fetchone()[0]
        if count > limit:
            if count == limit + 1:
                report("anders", "waarschuwing", {"rate_limit": bucket})
            raise Fail(429, "too many requests, try again later")

    def handle(row: sqlite3.Row) -> str:
        return f"{row['name']}#{row['tag']}"

    async def read_body(request: Request) -> bytes:
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
            raise Fail(413, "request too large")
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY:
                raise Fail(413, "request too large")
        return body

    def verify(request: Request, body: bytes, ip: str) -> bytes:
        """The public key that signed this request (it is not looked up here)."""
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Games "):
            raise Fail(401, "not signed")
        parts = dict(p.strip().split("=", 1) for p in auth[6:].split(",") if "=" in p)
        try:
            key, sig, nonce = b64d(parts["key"]), b64d(parts["sig"]), parts["nonce"]
            ts = int(parts["ts"])
        except (KeyError, ValueError):
            raise Fail(401, "bad signature") from None
        if len(key) != 32 or len(sig) != 64 or len(b64d(nonce)) != 16:
            raise Fail(401, "bad signature")
        if abs(now() - ts) > CLOCK_SKEW:
            raise Fail(401, "clock is off, check the time on this device")
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        message = f"{request.method}\n{path}\n{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode()
        try:
            VerifyKey(key).verify(message, sig)
        except (BadSignatureError, ValueError):
            hit("bad_sig", ip)
            report("rechten_geweigerd", "info", {"reason": "bad signature", "path": request.url.path})
            raise Fail(401, "bad signature") from None
        db = store.db()
        db.execute("DELETE FROM nonces WHERE ts < ?", (now() - NONCE_TTL,))
        try:
            db.execute("INSERT INTO nonces (pubkey, nonce, ts) VALUES (?, ?, ?)", (key, nonce, now()))
        except sqlite3.IntegrityError:
            raise Fail(401, "request was already used") from None
        return key

    def device_of(key: bytes) -> sqlite3.Row:
        row = store.db().execute(
            "SELECT d.id AS device_id, p.* FROM devices d JOIN players p ON p.id = d.player_id "
            "WHERE d.pubkey = ?", (key,)).fetchone()
        if row is None:
            raise Fail(401, "unknown device")
        store.db().execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now(), row["device_id"]))
        return row

    def as_json(body: bytes) -> dict:
        if not body:
            return {}
        try:
            data = json.loads(body)
        except ValueError:
            raise Fail(400, "invalid JSON") from None
        if not isinstance(data, dict):
            raise Fail(400, "invalid JSON")
        return data

    def text(data: dict, key: str, pattern: re.Pattern, lower: bool = True) -> str:
        value = data.get(key)
        if not isinstance(value, str):
            raise Fail(400, f"{key} is missing")
        value = value.strip().lower() if lower else value.strip()
        if not pattern.fullmatch(value):
            raise Fail(400, f"{key} is not valid")
        return value

    def label(data: dict) -> str:
        value = data.get("label", "device")
        value = value if isinstance(value, str) else "device"
        return "".join(ch for ch in value if ch.isalnum() or ch in " -_.")[:32] or "device"

    def new_recovery() -> tuple[str, str]:
        code = "-".join(secrets.token_hex(3).upper() for _ in range(4))
        return code, hashlib.sha256(code.encode()).hexdigest()

    def endpoint(kind: str):
        """kind: 'open' (no key yet, signed by the new key), 'player' (a known device)."""

        def wrap(fn):
            async def handler(request: Request) -> JSONResponse:
                ip = client_ip(request)
                try:
                    hit("ip", ip)
                    body = await read_body(request)
                    key = verify(request, body, ip)
                    data = as_json(body)
                    if kind == "open":
                        result = fn(request, data, key, ip)
                    else:
                        me = device_of(key)
                        hit("player", me["id"])
                        result = fn(request, data, me)
                    status, payload = result if isinstance(result, tuple) else (200, result)
                    return JSONResponse(payload, status_code=status)
                except Fail as fail:
                    return JSONResponse({"error": fail.message}, status_code=fail.status)
            return handler
        return wrap

    def player_view(row: sqlite3.Row) -> dict:
        devices = store.db().execute(
            "SELECT id, label, created, last_seen FROM devices WHERE player_id = ? ORDER BY created",
            (row["id"],)).fetchall()
        return {"id": row["id"], "handle": handle(row), "created": row["created"],
                "devices": [dict(d) for d in devices]}

    def are_friends(a: str, b: str) -> bool:
        x, y = sorted((a, b))
        return store.db().execute("SELECT 1 FROM friends WHERE a = ? AND b = ?", (x, y)).fetchone() is not None

    def befriend(a: str, b: str) -> None:
        x, y = sorted((a, b))
        db = store.db()
        db.execute("INSERT OR IGNORE INTO friends (a, b, since) VALUES (?, ?, ?)", (x, y, now()))
        db.execute("DELETE FROM friend_requests WHERE (from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?)",
                   (a, b, b, a))

    def blocked(blocker: str, other: str) -> bool:
        return store.db().execute("SELECT 1 FROM blocks WHERE blocker = ? AND blocked = ?",
                                  (blocker, other)).fetchone() is not None

    def friend_count(pid: str) -> int:
        return store.db().execute("SELECT COUNT(*) FROM friends WHERE a = ? OR b = ?", (pid, pid)).fetchone()[0]

    # -- routes ----------------------------------------------------------------
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    @endpoint("open")
    def register(request, data, key, ip):
        hit("register", ip)
        name = text(data, "name", NAME_RE)
        db = store.db()
        if db.execute("SELECT 1 FROM devices WHERE pubkey = ?", (key,)).fetchone():
            raise Fail(409, "this device already has an account")
        taken = {r[0] for r in db.execute("SELECT tag FROM players WHERE name = ?", (name,))}
        if len(taken) > 9000:
            raise Fail(409, "that name is too popular, pick another one")
        tag = next(t for t in (f"{secrets.randbelow(10000):04d}" for _ in range(1000)) if t not in taken)
        pid = secrets.token_hex(8)
        code, code_hash = new_recovery()
        with tx(db):
            db.execute("INSERT INTO players (id, name, tag, created, recovery_hash) VALUES (?, ?, ?, ?, ?)",
                       (pid, name, tag, now(), code_hash))
            db.execute("INSERT INTO devices (id, player_id, pubkey, label, created, last_seen) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (secrets.token_hex(8), pid, key, label(data), now(), now()))
        row = db.execute("SELECT * FROM players WHERE id = ?", (pid,)).fetchone()
        return 201, {**player_view(row), "recovery_code": code}

    @endpoint("player")
    def me(request, data, me):
        if request.method == "DELETE":
            store.db().execute("DELETE FROM players WHERE id = ?", (me["id"],))
            return {"deleted": True}
        return player_view(me)

    @endpoint("player")
    def rename(request, data, me):
        name = text(data, "name", NAME_RE)
        db = store.db()
        taken = {r[0] for r in db.execute("SELECT tag FROM players WHERE name = ?", (name,))}
        tag = me["tag"] if me["tag"] not in taken else next(
            t for t in (f"{secrets.randbelow(10000):04d}" for _ in range(1000)) if t not in taken)
        db.execute("UPDATE players SET name = ?, tag = ? WHERE id = ?", (name, tag, me["id"]))
        return player_view(db.execute("SELECT * FROM players WHERE id = ?", (me["id"],)).fetchone())

    @endpoint("player")
    def friends(request, data, me):
        db = store.db()
        pid = me["id"]
        rows = db.execute(
            "SELECT p.*, f.since FROM friends f JOIN players p ON p.id = CASE WHEN f.a = ? THEN f.b ELSE f.a END "
            "WHERE f.a = ? OR f.b = ? ORDER BY p.name, p.tag", (pid, pid, pid)).fetchall()
        incoming = db.execute(
            "SELECT r.id AS request_id, r.created, p.* FROM friend_requests r JOIN players p ON p.id = r.from_id "
            "WHERE r.to_id = ? ORDER BY r.created", (pid,)).fetchall()
        outgoing = db.execute(
            "SELECT r.id AS request_id, r.created, p.* FROM friend_requests r JOIN players p ON p.id = r.to_id "
            "WHERE r.from_id = ? ORDER BY r.created", (pid,)).fetchall()
        blocks = db.execute("SELECT p.* FROM blocks b JOIN players p ON p.id = b.blocked WHERE b.blocker = ?",
                            (pid,)).fetchall()
        def last(pid_: str) -> Optional[dict]:
            row = db.execute("SELECT game, played FROM results WHERE player_id = ? AND status = 'ok' "
                             "ORDER BY id DESC LIMIT 1", (pid_,)).fetchone()
            return {"game": row["game"], "date": row["played"]} if row else None

        return {
            "friends": [{"id": r["id"], "handle": handle(r), "since": r["since"], "last_played": last(r["id"])}
                        for r in rows],
            "incoming": [{"request_id": r["request_id"], "id": r["id"], "handle": handle(r)} for r in incoming],
            "outgoing": [{"request_id": r["request_id"], "id": r["id"], "handle": handle(r)} for r in outgoing],
            "blocked": [{"id": r["id"], "handle": handle(r)} for r in blocks],
        }

    @endpoint("player")
    def friend_request(request, data, me):
        """Always the same answer, whether the player exists or not: no enumeration."""
        value = text(data, "handle", HANDLE_RE)
        hit("friend_request", me["id"])
        m = HANDLE_RE.fullmatch(value)
        db = store.db()
        other = db.execute("SELECT * FROM players WHERE name = ? AND tag = ?", (m.group(1), m.group(2))).fetchone()
        answer = (202, {"sent": True, "note": "If that player exists, they get your request."})
        if other is None or other["id"] == me["id"] or blocked(other["id"], me["id"]) or are_friends(me["id"], other["id"]):
            return answer
        if blocked(me["id"], other["id"]):
            db.execute("DELETE FROM blocks WHERE blocker = ? AND blocked = ?", (me["id"], other["id"]))
        if db.execute("SELECT 1 FROM friend_requests WHERE from_id = ? AND to_id = ?",
                      (other["id"], me["id"])).fetchone():
            if friend_count(me["id"]) < MAX_FRIENDS and friend_count(other["id"]) < MAX_FRIENDS:
                befriend(me["id"], other["id"])
            return answer
        pending = db.execute("SELECT COUNT(*) FROM friend_requests WHERE from_id = ?", (me["id"],)).fetchone()[0]
        if pending >= MAX_PENDING:
            raise Fail(429, "too many open requests, wait for answers first")
        db.execute("INSERT OR IGNORE INTO friend_requests (from_id, to_id, created) VALUES (?, ?, ?)",
                   (me["id"], other["id"], now()))
        return answer

    @endpoint("player")
    def answer_request(request, data, me):
        rid = request.path_params["rid"]
        if request.path_params["verb"] not in ("accept", "decline"):
            raise Fail(404, "not found")
        db = store.db()
        row = db.execute("SELECT * FROM friend_requests WHERE id = ? AND to_id = ?", (rid, me["id"])).fetchone()
        if row is None:
            raise Fail(404, "no such request")
        if request.path_params["verb"] == "accept":
            if friend_count(me["id"]) >= MAX_FRIENDS or friend_count(row["from_id"]) >= MAX_FRIENDS:
                raise Fail(409, "too many friends")
            befriend(me["id"], row["from_id"])
        else:
            db.execute("DELETE FROM friend_requests WHERE id = ?", (rid,))
        return {"ok": True}

    def target(request: Request) -> str:
        pid = request.path_params["pid"]
        if not ID_RE.fullmatch(pid):
            raise Fail(400, "not a player id")
        return pid

    @endpoint("player")
    def unfriend(request, data, me):
        pid = target(request)
        x, y = sorted((me["id"], pid))
        db = store.db()
        db.execute("DELETE FROM friends WHERE a = ? AND b = ?", (x, y))
        db.execute("DELETE FROM friend_requests WHERE from_id = ? AND to_id = ?", (me["id"], pid))
        return {"ok": True}

    @endpoint("player")
    def block(request, data, me):
        pid = target(request)
        db = store.db()
        if request.method == "DELETE":
            db.execute("DELETE FROM blocks WHERE blocker = ? AND blocked = ?", (me["id"], pid))
            return {"ok": True}
        if pid == me["id"] or db.execute("SELECT 1 FROM players WHERE id = ?", (pid,)).fetchone() is None:
            return {"ok": True}
        x, y = sorted((me["id"], pid))
        db.execute("DELETE FROM friends WHERE a = ? AND b = ?", (x, y))
        db.execute("DELETE FROM friend_requests WHERE (from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?)",
                   (me["id"], pid, pid, me["id"]))
        db.execute("INSERT OR IGNORE INTO blocks (blocker, blocked, created) VALUES (?, ?, ?)",
                   (me["id"], pid, now()))
        return {"ok": True}

    @endpoint("player")
    def link_code(request, data, me):
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        db = store.db()
        db.execute("DELETE FROM link_codes WHERE expires < ? OR player_id = ?", (now(), me["id"]))
        db.execute("INSERT INTO link_codes (code_hash, player_id, expires) VALUES (?, ?, ?)",
                   (hashlib.sha256(code.encode()).hexdigest(), me["id"], now() + 600))
        return {"code": code, "expires_in": 600}

    @endpoint("open")
    def link(request, data, key, ip):
        hit("link", ip)
        code = str(data.get("code", ""))[:32].strip().upper()
        if not re.fullmatch(r"[0-9A-F]{4}(-[0-9A-F]{4}){2}", code):
            raise Fail(400, "code is not valid")
        db = store.db()
        db.execute("DELETE FROM link_codes WHERE expires < ?", (now(),))
        row = db.execute("SELECT * FROM link_codes WHERE code_hash = ?",
                         (hashlib.sha256(code.encode()).hexdigest(),)).fetchone()
        if row is None:
            raise Fail(403, "that code is wrong or expired")
        if db.execute("SELECT 1 FROM devices WHERE pubkey = ?", (key,)).fetchone():
            raise Fail(409, "this device already has an account")
        if db.execute("SELECT COUNT(*) FROM devices WHERE player_id = ?", (row["player_id"],)).fetchone()[0] >= MAX_DEVICES:
            raise Fail(409, "too many devices, remove one first")
        db.execute("DELETE FROM link_codes WHERE code_hash = ?", (row["code_hash"],))
        db.execute("INSERT INTO devices (id, player_id, pubkey, label, created, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                   (secrets.token_hex(8), row["player_id"], key, label(data), now(), now()))
        return 201, player_view(db.execute("SELECT * FROM players WHERE id = ?", (row["player_id"],)).fetchone())

    @endpoint("player")
    def remove_device(request, data, me):
        did = request.path_params["did"]
        if not ID_RE.fullmatch(did):
            raise Fail(400, "not a device id")
        store.db().execute("DELETE FROM devices WHERE id = ? AND player_id = ?", (did, me["id"]))
        return player_view(me)

    @endpoint("open")
    def recover(request, data, key, ip):
        """Lost every device: the recovery code replaces all devices by this one."""
        hit("recover", ip)
        value = text(data, "handle", HANDLE_RE)
        code = data.get("recovery_code")
        if not isinstance(code, str) or len(code) > 64:
            raise Fail(400, "recovery_code is missing")
        m = HANDLE_RE.fullmatch(value)
        db = store.db()
        row = db.execute("SELECT * FROM players WHERE name = ? AND tag = ?", (m.group(1), m.group(2))).fetchone()
        given = hashlib.sha256(code.strip().upper().encode()).hexdigest()
        if row is None or not hmac.compare_digest(row["recovery_hash"], given):
            report("inlog_mislukt", "waarschuwing", {"reason": "recovery code wrong"})
            raise Fail(403, "that handle and recovery code do not match")
        if db.execute("SELECT 1 FROM devices WHERE pubkey = ?", (key,)).fetchone():
            raise Fail(409, "this device already has an account")
        new_code, new_hash = new_recovery()
        with tx(db):
            db.execute("DELETE FROM devices WHERE player_id = ?", (row["id"],))
            db.execute("INSERT INTO devices (id, player_id, pubkey, label, created, last_seen) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (secrets.token_hex(8), row["id"], key, label(data), now(), now()))
            db.execute("UPDATE players SET recovery_hash = ? WHERE id = ?", (new_hash, row["id"]))
        report("passkey_herstel", "info", {"player": row["id"]})
        return {**player_view(row), "recovery_code": new_code}

    # -- results, leaderboards, challenges (phases 3 and 4) ---------------------
    def game_and_settings(game: Any, settings: Any) -> tuple[str, dict]:
        if not isinstance(game, str) or game not in games:
            raise Fail(400, "unknown game")
        spec = games[game]
        settings = settings if isinstance(settings, dict) else {}
        clean = {}
        for key, choices in spec["settings"].items():
            value = str(settings.get(key, spec["defaults"][key]))
            if value not in choices:
                raise Fail(400, f"{key} is not valid")
            clean[key] = value
        return game, clean

    def seed_of(value: Any, allow_none: bool = False) -> Optional[int]:
        if value is None and allow_none:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SEED:
            raise Fail(400, "seed is not valid")
        return value

    @endpoint("player")
    def upload_result(request, data, me):
        """A finished game from games.jsonl. It only counts after the verifier replayed it."""
        hit("results", me["id"])
        record = data.get("record")
        if not isinstance(record, dict):
            raise Fail(400, "record is missing")
        game, settings = game_and_settings(data.get("game"), {k: record.get(k) for k in games.get(
            data.get("game") if isinstance(data.get("game"), str) else "", {}).get("settings", {})})
        seed = seed_of(record.get("seed"))
        raw = json.dumps(record, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        played = str(record.get("date", ""))[:19]
        db = store.db()
        try:
            db.execute("INSERT INTO results (player_id, game, seed, settings, record, record_hash, played, created) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (me["id"], game, seed, settings_key(settings), raw, digest, played, now()))
        except sqlite3.IntegrityError:
            pass  # sent before: fine
        row = db.execute("SELECT id, status FROM results WHERE player_id = ? AND record_hash = ?",
                         (me["id"], digest)).fetchone()
        return 202, {"id": row["id"], "status": row["status"]}

    @endpoint("player")
    def my_results(request, data, me):
        rows = store.db().execute(
            "SELECT id, game, seed, settings, status, reason, won, time, score, played FROM results "
            "WHERE player_id = ? ORDER BY id DESC LIMIT 100", (me["id"],)).fetchall()
        return {"results": [dict(r) for r in rows]}

    def friend_ids(pid: str) -> list[str]:
        rows = store.db().execute("SELECT CASE WHEN a = ? THEN b ELSE a END FROM friends WHERE a = ? OR b = ?",
                                  (pid, pid, pid)).fetchall()
        return [r[0] for r in rows]

    def board(game: str, settings: str, seed: Optional[int], players: Optional[list[str]],
              since: int = 0, limit: int = 20, me_id: str = "") -> list[dict]:
        metric = games[game]["metric"]
        best = "MIN(r.time)" if metric == "time" else "MAX(r.score)"
        where = ["r.game = ?", "r.settings = ?", "r.status = 'ok'", "r.created >= ?"]
        args: list = [game, settings, since]
        if metric == "time":
            where.append("r.won = 1 AND r.time IS NOT NULL")
        if seed is not None:
            where.append("r.seed = ?")
            args.append(seed)
        if players is not None:
            where.append(f"r.player_id IN ({','.join('?' * len(players))})")
            args += players
        rows = store.db().execute(
            f"SELECT p.id, p.name, p.tag, {best} AS value, COUNT(*) AS games FROM results r "
            f"JOIN players p ON p.id = r.player_id WHERE {' AND '.join(where)} GROUP BY p.id "
            f"ORDER BY value {'ASC' if metric == 'time' else 'DESC'}, p.name LIMIT ?", (*args, limit)).fetchall()
        return [{"rank": i + 1, "id": r["id"], "handle": handle(r), "value": r["value"], "games": r["games"],
                 "me": r["id"] == me_id, "metric": metric} for i, r in enumerate(rows)]

    @endpoint("player")
    def leaderboard(request, data, me):
        q = request.query_params
        game, settings = game_and_settings(q.get("game"), dict(
            part.split("=", 1) for part in (q.get("settings") or "").split(";") if "=" in part))
        seed = None if q.get("seed") in (None, "") else seed_of(int(q["seed"]) if q["seed"].isdigit() else -1)
        scope = q.get("scope", "friends")
        if scope not in ("friends", "all"):
            raise Fail(400, "scope is friends or all")
        players = None if scope == "all" else friend_ids(me["id"]) + [me["id"]]
        return {"game": game, "settings": settings, "seed": seed, "scope": scope,
                "rows": board(game, settings_key(settings), seed, players, me_id=me["id"])}

    @endpoint("player")
    def create_challenge(request, data, me):
        hit("challenges", me["id"])
        game, settings = game_and_settings(data.get("game"), data.get("settings"))
        kind = data.get("kind", "race")
        if kind not in ("race", "beat"):
            raise Fail(400, "kind is race or beat")
        to = data.get("to")
        if not isinstance(to, list) or not 1 <= len(to) <= 8 or not all(isinstance(t, str) and ID_RE.fullmatch(t) for t in to):
            raise Fail(400, "pick 1 to 8 friends")
        friends_of_me = set(friend_ids(me["id"]))
        if not set(to) <= friends_of_me:
            raise Fail(403, "you can only challenge friends")
        target = None
        db = store.db()
        if kind == "beat":
            rid = data.get("result_id")
            row = db.execute("SELECT * FROM results WHERE id = ? AND player_id = ? AND status = 'ok' AND won = 1",
                             (rid if isinstance(rid, int) else -1, me["id"])).fetchone()
            if row is None:
                raise Fail(400, "pick one of your own verified wins to beat")
            game, seed, skey, target = row["game"], row["seed"], row["settings"], row["id"]
        else:
            seed = seed_of(data.get("seed"), allow_none=True) or 1 + secrets.randbelow(MAX_SEED)
            skey = settings_key(settings)
        with tx(db):
            cur = db.execute("INSERT INTO challenges (from_id, game, seed, settings, kind, target_result, created, expires) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (me["id"], game, seed, skey, kind, target, now(), now() + CHALLENGE_DAYS * 86400))
            cid = cur.lastrowid
            db.execute("INSERT INTO challenge_players (challenge_id, player_id, status) VALUES (?, ?, 'accepted')",
                       (cid, me["id"]))
            for pid in set(to):
                db.execute("INSERT INTO challenge_players (challenge_id, player_id, status) VALUES (?, ?, 'invited')",
                           (cid, pid))
        return 201, {"id": cid, "seed": seed}

    @endpoint("player")
    def list_challenges(request, data, me):
        db = store.db()
        rows = db.execute(
            "SELECT c.*, p.name, p.tag FROM challenges c JOIN challenge_players cp ON cp.challenge_id = c.id "
            "JOIN players p ON p.id = c.from_id WHERE cp.player_id = ? AND c.expires > ? AND cp.status != 'declined' "
            "ORDER BY c.id DESC LIMIT 50", (me["id"], now())).fetchall()
        out = []
        for c in rows:
            members = db.execute(
                "SELECT cp.status, p.id, p.name, p.tag FROM challenge_players cp JOIN players p ON p.id = cp.player_id "
                "WHERE cp.challenge_id = ?", (c["id"],)).fetchall()
            standings = {r["id"]: r for r in board(c["game"], c["settings"], c["seed"],
                                                   [m["id"] for m in members], since=c["created"], limit=10)}
            target = None
            if c["target_result"]:
                t = db.execute("SELECT time, score FROM results WHERE id = ?", (c["target_result"],)).fetchone()
                target = dict(t) if t else None
            out.append({
                "id": c["id"], "game": c["game"], "seed": c["seed"],
                "settings": dict(part.split("=", 1) for part in c["settings"].split(";") if "=" in part),
                "kind": c["kind"], "from": f"{c['name']}#{c['tag']}", "mine": c["from_id"] == me["id"],
                "expires": c["expires"], "target": target,
                "players": [{"handle": handle(m), "status": m["status"], "me": m["id"] == me["id"],
                             "value": standings[m["id"]]["value"] if m["id"] in standings else None}
                            for m in members],
            })
        return {"challenges": out}

    @endpoint("player")
    def answer_challenge(request, data, me):
        verb = request.path_params["verb"]
        if verb not in ("accept", "decline"):
            raise Fail(404, "not found")
        cur = store.db().execute(
            "UPDATE challenge_players SET status = ? WHERE challenge_id = ? AND player_id = ? AND status = 'invited'",
            ("accepted" if verb == "accept" else "declined", request.path_params["cid"], me["id"]))
        if cur.rowcount == 0:
            raise Fail(404, "no such challenge")
        return {"ok": True}

    async def not_found(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": "not found"}, status_code=404)

    async def not_allowed(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": "method not allowed"}, status_code=405)

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/v1/register", register, methods=["POST"]),
        Route("/v1/me", me, methods=["GET", "DELETE"]),
        Route("/v1/me/name", rename, methods=["POST"]),
        Route("/v1/friends", friends, methods=["GET"]),
        Route("/v1/friends/requests", friend_request, methods=["POST"]),
        Route("/v1/friends/requests/{rid:int}/{verb:str}", answer_request, methods=["POST"]),
        Route("/v1/friends/{pid:str}", unfriend, methods=["DELETE"]),
        Route("/v1/blocks/{pid:str}", block, methods=["POST", "DELETE"]),
        Route("/v1/devices/link-code", link_code, methods=["POST"]),
        Route("/v1/devices/link", link, methods=["POST"]),
        Route("/v1/devices/{did:str}", remove_device, methods=["DELETE"]),
        Route("/v1/recover", recover, methods=["POST"]),
        Route("/v1/results", upload_result, methods=["POST"]),
        Route("/v1/results/mine", my_results, methods=["GET"]),
        Route("/v1/leaderboard", leaderboard, methods=["GET"]),
        Route("/v1/challenges", create_challenge, methods=["POST"]),
        Route("/v1/challenges/list", list_challenges, methods=["GET"]),
        Route("/v1/challenges/{cid:int}/{verb:str}", answer_challenge, methods=["POST"]),
    ]
    app = Starlette(routes=routes, exception_handlers={404: not_found, 405: not_allowed})
    app.add_middleware(SafeHeaders)

    return app


def backup_once(db_path: str, keep: int = 14) -> str:
    """A consistent copy (SQLite's own backup, safe while the server writes) in backups/,
    one per day, the last `keep` days kept."""
    folder = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    target = os.path.join(folder, time.strftime("games-%Y%m%d.db", time.gmtime()))
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.chmod(target, 0o600)
    old = sorted(f for f in os.listdir(folder) if f.startswith("games-") and f.endswith(".db"))
    for name in old[:-keep]:
        os.remove(os.path.join(folder, name))
    return target


def start_backups(db_path: str) -> None:
    def loop() -> None:
        while True:
            try:
                backup_once(db_path)
            except Exception:  # noqa: BLE001 - a failed backup must not stop the server; retried in an hour
                time.sleep(3600)
                continue
            time.sleep(24 * 3600)

    threading.Thread(target=loop, daemon=True, name="backups").start()


app = None
if os.environ.get("GAMES_SERVER_AUTOSTART") == "1":
    app = create_app()
    start_backups(os.environ.get("GAMES_DB", "/data/games.db"))
