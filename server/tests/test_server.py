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


# -- phases 3 and 4: results, verification, leaderboards, challenges ----------

import sqlite3

from games_server.verifier import verify_pending


def verify_all(tmp_path, answers):
    """Run the verifier with a fake replay: answers[(game, seed)] -> the --verify JSON."""
    db = sqlite3.connect(str(tmp_path / "games.db"), isolation_level=None)
    db.row_factory = sqlite3.Row

    def runner(game, record):
        rec = json.loads(record)
        return answers.get((game, rec.get("seed"), rec.get("time")), {"ok": False, "reason": "no"})

    verify_pending(db, runner=runner)


def sudoku_record(seed, time_, difficulty="expert"):
    return {"seed": seed, "difficulty": difficulty, "won": True, "time": time_, "date": "2026-11-12T10:00:00",
            "log": ["v1=5@3"]}


def ok_answer(seed, time_, difficulty="expert", won=True):
    return {"ok": True, "game": "sudoku", "seed": seed, "settings": {"difficulty": difficulty},
            "won": won, "time": time_, "score": None, "moves": 60}


def test_results_count_only_after_the_replay(server, tmp_path, monkeypatch):
    a = player(server, tmp_path, monkeypatch, "a")
    b = player(server, tmp_path, monkeypatch, "b")
    me_a, me_b = a.register("anna"), b.register("bert")
    a.add_friend(me_b["handle"])
    b.answer(b.friends()["incoming"][0]["request_id"], True)
    r1 = a.call("POST", "/v1/results", {"game": "sudoku", "record": sudoku_record(7, 600)})
    assert r1["status"] == "pending"
    # The same record again is no second result.
    assert a.call("POST", "/v1/results", {"game": "sudoku", "record": sudoku_record(7, 600)})["id"] == r1["id"]
    b.call("POST", "/v1/results", {"game": "sudoku", "record": sudoku_record(7, 500)})
    # Claims a time the replay does not back up: rejected, not on the board.
    b.call("POST", "/v1/results", {"game": "sudoku", "record": sudoku_record(7, 5)})
    board = a.call("GET", "/v1/leaderboard?game=sudoku&settings=difficulty%3Dexpert&scope=friends")
    assert board["rows"] == []
    verify_all(tmp_path, {("sudoku", 7, 600): ok_answer(7, 600), ("sudoku", 7, 500): ok_answer(7, 480)})
    rows = a.call("GET", "/v1/leaderboard?game=sudoku&settings=difficulty%3Dexpert&scope=friends")["rows"]
    # The replay's time counts (480), not the uploaded one (500).
    assert [(r["handle"], r["value"]) for r in rows] == [(me_b["handle"], 480), (me_a["handle"], 600)]
    assert rows[1]["me"] is True
    mine = {r["time"]: r["status"] for r in b.call("GET", "/v1/results/mine")["results"]}
    assert mine == {480: "ok", None: "rejected"}
    # Per seed, and another difficulty is another board.
    assert a.call("GET", "/v1/leaderboard?game=sudoku&settings=difficulty%3Dexpert&seed=8&scope=all")["rows"] == []
    assert a.call("GET", "/v1/leaderboard?game=sudoku&settings=difficulty%3Dhard&scope=all")["rows"] == []
    assert a.friends()["friends"][0]["last_played"]["game"] == "sudoku"


def test_replay_about_another_game_is_rejected(server, tmp_path, monkeypatch):
    a = player(server, tmp_path, monkeypatch, "a")
    a.register("anna")
    a.call("POST", "/v1/results", {"game": "sudoku", "record": sudoku_record(7, 600)})
    verify_all(tmp_path, {("sudoku", 7, 600): ok_answer(8, 600)})  # replayed seed 8, not 7
    assert a.call("GET", "/v1/results/mine")["results"][0]["reason"] == "replay is about another game"


def test_bad_results_are_refused(server, tmp_path, monkeypatch):
    a = player(server, tmp_path, monkeypatch, "a")
    a.register("anna")
    for body, msg in [({"game": "chess", "record": sudoku_record(1, 9)}, "unknown game"),
                      ({"game": "sudoku", "record": {"difficulty": "expert"}}, "seed"),
                      ({"game": "sudoku", "record": sudoku_record(1, 9, "insane")}, "difficulty"),
                      ({"game": "sudoku", "record": "x"}, "record")]:
        with pytest.raises(OnlineError, match=msg):
            a.call("POST", "/v1/results", body)


def test_challenges_race_and_beat(server, tmp_path, monkeypatch):
    a = player(server, tmp_path, monkeypatch, "a")
    b = player(server, tmp_path, monkeypatch, "b")
    c = player(server, tmp_path, monkeypatch, "c")
    me_a, me_b, me_c = a.register("anna"), b.register("bert"), c.register("carl")
    a.add_friend(me_b["handle"])
    b.answer(b.friends()["incoming"][0]["request_id"], True)
    # Only friends.
    with pytest.raises(OnlineError, match="only challenge friends"):
        a.call("POST", "/v1/challenges", {"to": [me_c["id"]], "game": "sudoku", "kind": "race"})
    made = a.call("POST", "/v1/challenges", {"to": [me_b["id"]], "game": "sudoku",
                                              "settings": {"difficulty": "master"}, "kind": "race"})
    assert 1 <= made["seed"] <= 999999  # no seed given: the server picks one, the same for both
    seen = b.call("GET", "/v1/challenges/list")["challenges"][0]
    assert seen["from"] == me_a["handle"] and seen["settings"] == {"difficulty": "master"}
    assert {p["handle"]: p["status"] for p in seen["players"]} == {me_a["handle"]: "accepted", me_b["handle"]: "invited"}
    b.call("POST", f"/v1/challenges/{seen['id']}/accept")
    seed = made["seed"]
    b.call("POST", "/v1/results", {"game": "sudoku", "record": sudoku_record(seed, 900, "master")})
    verify_all(tmp_path, {("sudoku", seed, 900): {**ok_answer(seed, 900, "master")}})
    standing = {p["handle"]: p["value"] for p in a.call("GET", "/v1/challenges/list")["challenges"][0]["players"]}
    assert standing == {me_a["handle"]: None, me_b["handle"]: 900}
    # Beat my time: only on one of your own verified wins.
    with pytest.raises(OnlineError, match="verified wins"):
        a.call("POST", "/v1/challenges", {"to": [me_b["id"]], "game": "sudoku", "kind": "beat", "result_id": 999})
    a.call("POST", "/v1/results", {"game": "sudoku", "record": sudoku_record(11, 700)})
    verify_all(tmp_path, {("sudoku", 11, 700): ok_answer(11, 700)})
    rid = a.call("GET", "/v1/results/mine")["results"][0]["id"]
    beat = a.call("POST", "/v1/challenges", {"to": [me_b["id"]], "game": "sudoku", "kind": "beat", "result_id": rid})
    got = next(x for x in b.call("GET", "/v1/challenges/list")["challenges"] if x["id"] == beat["id"])
    assert got["kind"] == "beat" and got["seed"] == 11 and got["target"]["time"] == 700
    b.call("POST", f"/v1/challenges/{got['id']}/decline")
    assert all(x["id"] != got["id"] for x in b.call("GET", "/v1/challenges/list")["challenges"])


def test_backups_keep_the_last_days(tmp_path, monkeypatch):
    import os

    from games_server.app import backup_once

    create_app(str(tmp_path / "games.db"), trusted_proxies=set())
    folder = tmp_path / "backups"
    folder.mkdir()
    for day in range(20):
        (folder / f"games-202601{day + 1:02d}.db").write_text("old")
    made = backup_once(str(tmp_path / "games.db"), keep=14)
    names = sorted(os.listdir(folder))
    assert len(names) == 14 and os.path.basename(made) in names
    assert oct(os.stat(made).st_mode & 0o777) == "0o600"
    con = sqlite3.connect(made)
    assert con.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0
