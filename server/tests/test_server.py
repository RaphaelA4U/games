"""The games server, driven by the real library client (games_app.online)."""

import json
import time

import pytest
from starlette.testclient import TestClient

import games_app.config as cfg
from games_app.online import Online, OnlineError
from games_server.app import create_app


@pytest.fixture
def server(tmp_path):
    app = create_app(str(tmp_path / "games.db"), trusted_proxies=set())
    return TestClient(app)


def player(server, tmp_path, monkeypatch, who):
    """A client with its own key file (its own device)."""
    home = tmp_path / who
    monkeypatch.setattr(cfg, "CONFIG_DIR", home)

    def transport(method, url, headers, body):
        r = server.request(method, url.replace("http://test", ""), headers=headers, content=body)
        return r.status_code, r.json()

    client = Online(transport=transport, base="http://test")
    client.signing_key()  # make the key while CONFIG_DIR points at this player's home
    return client


def test_register_friends_and_no_enumeration(server, tmp_path, monkeypatch):
    a = player(server, tmp_path, monkeypatch, "a")
    b = player(server, tmp_path, monkeypatch, "b")
    me_a = a.register("Raphael")
    assert me_a["handle"].startswith("raphael#") and len(me_a["recovery_code"]) == 27
    me_b = b.register("kyle")
    # Asking for a player that does not exist looks exactly like one that does.
    assert a.add_friend("nobody#0000") == a.add_friend(me_b["handle"])
    assert b.friends()["incoming"][0]["handle"] == me_a["handle"]
    rid = b.friends()["incoming"][0]["request_id"]
    b.answer(rid, accept=True)
    assert [f["handle"] for f in a.friends()["friends"]] == [me_b["handle"]]
    a.unfriend(me_b["id"])
    assert b.friends()["friends"] == []


def test_block_stops_requests(server, tmp_path, monkeypatch):
    a = player(server, tmp_path, monkeypatch, "a")
    b = player(server, tmp_path, monkeypatch, "b")
    me_a, me_b = a.register("anna"), b.register("bert")
    b.block(me_a["id"])
    a.add_friend(me_b["handle"])
    assert b.friends()["incoming"] == []
    assert b.friends()["blocked"][0]["handle"] == me_a["handle"]


def test_both_asking_makes_friends(server, tmp_path, monkeypatch):
    a = player(server, tmp_path, monkeypatch, "a")
    b = player(server, tmp_path, monkeypatch, "b")
    me_a, me_b = a.register("anna"), b.register("bert")
    a.add_friend(me_b["handle"])
    b.add_friend(me_a["handle"])
    assert a.friends()["friends"][0]["handle"] == me_b["handle"]


def test_signatures_are_checked(server, tmp_path, monkeypatch):
    a = player(server, tmp_path, monkeypatch, "a")
    a.register("anna")
    sent = {}

    def keep(method, url, headers, body):
        sent.update(method=method, url=url, headers=headers, body=body)
        r = server.request(method, url.replace("http://test", ""), headers=headers, content=body)
        return r.status_code, r.json()

    a.transport = keep
    a.call("POST", "/v1/me/name", {"name": "anne"})
    path = sent["url"].replace("http://test", "")
    # The same request again (a replay) is refused.
    r = server.request("POST", path, headers=sent["headers"], content=sent["body"])
    assert r.status_code == 401 and "already used" in r.json()["error"]
    # A changed body breaks the signature.
    a.transport = lambda m, u, h, b: (server.request(m, u.replace("http://test", ""), headers=h,
                                                     content=b.replace(b"anne", b"evil")).status_code, {})
    with pytest.raises(OnlineError):
        a.call("POST", "/v1/me/name", {"name": "anne"})
    # No signature at all.
    assert server.get("/v1/me").status_code == 401
    assert server.get("/v1/me", headers={"Authorization": "Games key=x, ts=1, nonce=y, sig=z"}).status_code == 401


def test_old_timestamps_are_refused(server, tmp_path, monkeypatch):
    a = player(server, tmp_path, monkeypatch, "a")
    a.register("anna")
    import types

    import games_app.online as online

    real = time.time
    monkeypatch.setattr(online, "time", types.SimpleNamespace(time=lambda: real() - 300))
    with pytest.raises(OnlineError, match="clock"):
        a.me()


def test_names_are_strict(server, tmp_path, monkeypatch):
    a = player(server, tmp_path, monkeypatch, "a")
    for bad in ("ab", "x" * 17, "evil\x1b[31m", "[b]bold[/b]", "raphaël"):
        with pytest.raises(OnlineError, match="not valid"):
            a.register(bad)


def test_link_a_second_device_and_recover(server, tmp_path, monkeypatch):
    a = player(server, tmp_path, monkeypatch, "a")
    me = a.register("anna")
    code = a.link_code()["code"]
    laptop = player(server, tmp_path, monkeypatch, "laptop")
    linked = laptop.link(code.lower())
    assert linked["id"] == me["id"] and len(linked["devices"]) == 2
    other = player(server, tmp_path, monkeypatch, "other")
    with pytest.raises(OnlineError, match="wrong or expired"):
        other.link(code)  # single use
    # Lost both devices: the recovery code moves the account to a new one.
    new = player(server, tmp_path, monkeypatch, "new")
    with pytest.raises(OnlineError, match="do not match"):
        new.recover(me["handle"], "0000-0000-0000-0000")
    back = new.recover(me["handle"], me["recovery_code"])
    assert back["id"] == me["id"] and len(back["devices"]) == 1
    assert back["recovery_code"] != me["recovery_code"]
    with pytest.raises(OnlineError, match="unknown device"):
        a.me()


def test_delete_account_removes_everything(server, tmp_path, monkeypatch):
    a = player(server, tmp_path, monkeypatch, "a")
    b = player(server, tmp_path, monkeypatch, "b")
    me_a, me_b = a.register("anna"), b.register("bert")
    a.add_friend(me_b["handle"])
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path / "a")
    a.delete_account()
    assert b.friends()["incoming"] == []


def test_rate_limit_and_size_limit(server, tmp_path, monkeypatch):
    for i in range(5):
        player(server, tmp_path, monkeypatch, f"p{i}").register(f"player{i}")
    with pytest.raises(OnlineError, match="too many"):
        player(server, tmp_path, monkeypatch, "p6").register("player6")
    big = json.dumps({"name": "x" * 70000})
    assert server.post("/v1/register", content=big).status_code == 413


def test_answers_are_json_with_safe_headers(server):
    r = server.get("/nope")
    assert r.status_code == 404 and r.json() == {"error": "not found"}
    r = server.get("/healthz")
    assert r.json() == {"ok": True}
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert server.post("/healthz").status_code == 405
