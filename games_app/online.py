"""The online part of the library: this device's key and the games server
(docs/ONLINE.md). Nothing here runs until you go online yourself.

The key is an Ed25519 key pair in ~/.config/games/identity.key (mode 0600);
the server only ever sees the public half. Every request is signed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from . import config

DEFAULT_SERVER = "https://games.rustema.app"
Transport = Callable[[str, str, dict, bytes], "tuple[int, dict]"]


class OnlineError(Exception):
    pass


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def key_file() -> Path:
    return config.CONFIG_DIR / "identity.key"


def server_url() -> str:
    return (os.environ.get("GAMES_SERVER") or config.load_settings().get("server") or DEFAULT_SERVER).rstrip("/")


def clean(value: Any, limit: int = 40) -> str:
    """Text from the server, safe for the terminal: printable characters only, no markup."""
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return text.replace("[", "(").replace("]", ")")[:limit]


def _urllib_transport(method: str, url: str, headers: dict, body: bytes) -> "tuple[int, dict]":
    req = urllib.request.Request(url, data=body or None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read(1_000_000) or b"{}")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read(100_000) or b"{}")
        except ValueError:
            return err.code, {}
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise OnlineError("The games server cannot be reached. Check your internet.") from err


class Online:
    def __init__(self, transport: Optional[Transport] = None, base: Optional[str] = None) -> None:
        self.transport = transport or _urllib_transport
        self.base = base if base is not None else server_url()
        self._key = None

    # -- the device key ----------------------------------------------------
    def has_key(self) -> bool:
        return key_file().exists()

    def signing_key(self):
        try:
            from nacl.signing import SigningKey
        except ImportError as err:  # 32-bit iSH has no PyNaCl wheel: everything else still works
            raise OnlineError("Online needs PyNaCl, which this device does not have.") from err

        if self._key is None:
            path = key_file()
            if path.exists():
                seed = bytes.fromhex(path.read_text().strip())
                self._key = SigningKey(seed)
            else:
                self._key = SigningKey.generate()
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(bytes(self._key).hex())
        return self._key

    def forget_key(self) -> None:
        try:
            key_file().unlink()
        except OSError:
            pass
        self._key = None

    # -- requests ------------------------------------------------------------
    def call(self, method: str, path: str, data: Optional[dict] = None) -> dict:
        body = json.dumps(data).encode() if data is not None else b""
        key = self.signing_key()
        ts = str(int(time.time()))
        nonce = b64e(secrets.token_bytes(16))
        message = f"{method}\n{path}\n{ts}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode()
        sig = key.sign(message).signature
        headers = {
            "Authorization": f"Games key={b64e(bytes(key.verify_key))}, ts={ts}, nonce={nonce}, sig={b64e(sig)}",
            "Content-Type": "application/json",
            "User-Agent": "games-library",
        }
        status, answer = self.transport(method, self.base + path, headers, body)
        if status >= 400:
            raise OnlineError(clean(answer.get("error") or f"the server said {status}", 120))
        return answer

    @staticmethod
    def label() -> str:
        # Only the kind of device, never its host name (that often holds a person's name).
        system = {"Darwin": "macOS"}.get(platform.system(), platform.system() or "device")
        return clean(f"{system} {platform.machine()}".strip(), 32)

    # -- the API -------------------------------------------------------------
    def register(self, name: str) -> dict:
        return self.call("POST", "/v1/register", {"name": name, "label": self.label()})

    def me(self) -> dict:
        return self.call("GET", "/v1/me")

    def rename(self, name: str) -> dict:
        return self.call("POST", "/v1/me/name", {"name": name})

    def delete_account(self) -> None:
        self.call("DELETE", "/v1/me")
        self.forget_key()

    def friends(self) -> dict:
        return self.call("GET", "/v1/friends")

    def add_friend(self, handle: str) -> dict:
        return self.call("POST", "/v1/friends/requests", {"handle": handle})

    def answer(self, request_id: int, accept: bool) -> dict:
        return self.call("POST", f"/v1/friends/requests/{int(request_id)}/{'accept' if accept else 'decline'}")

    def unfriend(self, player_id: str) -> dict:
        return self.call("DELETE", f"/v1/friends/{player_id}")

    def block(self, player_id: str, on: bool = True) -> dict:
        return self.call("POST" if on else "DELETE", f"/v1/blocks/{player_id}")

    def link_code(self) -> dict:
        return self.call("POST", "/v1/devices/link-code")

    def link(self, code: str) -> dict:
        return self.call("POST", "/v1/devices/link", {"code": code, "label": self.label()})

    def recover(self, handle: str, code: str) -> dict:
        return self.call("POST", "/v1/recover", {"handle": handle, "recovery_code": code, "label": self.label()})

    # -- results, leaderboards, challenges (phases 3 and 4) ------------------
    def upload(self, game: str, record: dict) -> dict:
        return self.call("POST", "/v1/results", {"game": game, "record": record})

    def my_results(self) -> list:
        return self.call("GET", "/v1/results/mine").get("results", [])

    def leaderboard(self, game: str, settings: dict, seed: Optional[int] = None, scope: str = "friends") -> dict:
        from urllib.parse import quote

        key = ";".join(f"{k}={settings[k]}" for k in sorted(settings))
        path = f"/v1/leaderboard?game={quote(game)}&settings={quote(key)}&scope={scope}"
        if seed is not None:
            path += f"&seed={int(seed)}"
        return self.call("GET", path)

    def challenge(self, to: list, game: str, settings: dict, seed: Optional[int] = None, kind: str = "race",
                  result_id: Optional[int] = None) -> dict:
        body: dict = {"to": list(to), "game": game, "settings": settings, "kind": kind}
        if seed is not None:
            body["seed"] = int(seed)
        if result_id is not None:
            body["result_id"] = int(result_id)
        return self.call("POST", "/v1/challenges", body)

    def challenges(self) -> list:
        return self.call("GET", "/v1/challenges/list").get("challenges", [])

    def answer_challenge(self, cid: int, accept: bool) -> dict:
        return self.call("POST", f"/v1/challenges/{int(cid)}/{'accept' if accept else 'decline'}")


# -- sending finished games -------------------------------------------------------

def _sent_file() -> Path:
    return config.CONFIG_DIR / "sent.json"


def sharing() -> bool:
    """Send finished games to the server (for leaderboards and challenges)? On once online, unless turned off."""
    return bool(config.load_settings().get("share_results", True))


def pending_records(games: list) -> "list[tuple[str, dict, str]]":
    """Finished games with a seed that were not sent yet: (game id, record, its hash)."""
    try:
        sent = set(json.loads(_sent_file().read_text()))
    except (OSError, ValueError):
        sent = set()
    out = []
    for game in games:
        play = game.get("play") or {}
        path = Path.home() / ".config" / game["id"] / play.get("history", "games.jsonl")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-300:]
        except OSError:
            continue
        for line in lines:
            if len(line) > 60_000:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get("seed"), int):
                continue
            digest = hashlib.sha256(line.encode()).hexdigest()
            if digest not in sent:
                out.append((game["id"], rec, digest))
    return out


def send_results(online: "Online", games: list, limit: int = 50) -> int:
    """Send what is new (oldest first); remembers what went, so nothing goes twice."""
    if not sharing() or not online.has_key():
        return 0
    try:
        sent = set(json.loads(_sent_file().read_text()))
    except (OSError, ValueError):
        sent = set()
    count = 0
    for game_id, rec, digest in pending_records(games)[:limit]:
        try:
            online.upload(game_id, rec)
        except OnlineError as err:
            if "unknown device" in str(err) or "reached" in str(err):
                break
        sent.add(digest)  # also a refused one: sending it again would not change the answer
        count += 1
    if count:
        _sent_file().parent.mkdir(parents=True, exist_ok=True)
        _sent_file().write_text(json.dumps(sorted(sent)))
    return count
