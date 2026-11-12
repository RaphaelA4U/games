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
}


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
        body = json.dumps({"soort": kind, "ernst": severity, "details": json.dumps(details)[:4000],
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


def create_app(db_path: Optional[str] = None, trusted_proxies: Optional[set[str]] = None) -> Starlette:
    store = Store(db_path or os.environ.get("GAMES_DB", "/data/games.db"))
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
        return {
            "friends": [{"id": r["id"], "handle": handle(r), "since": r["since"]} for r in rows],
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
    ]
    app = Starlette(routes=routes, exception_handlers={404: not_found, 405: not_allowed})
    app.add_middleware(SafeHeaders)

    return app


app = None
if os.environ.get("GAMES_SERVER_AUTOSTART") == "1":
    app = create_app()
