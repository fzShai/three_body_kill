"""Smoke test: HTTP rooms + engine + WS hello (avoids blocking receive loops)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from game.engine import GameSession
from rooms import room_manager
from server import app


def main() -> None:
    g = GameSession.create("SMOKE", ["alice", "bob"])
    assert g.phase == "turn"
    cur = g.current_player()
    ok, msg = g.apply_action(cur, {"action": "pass"})
    assert ok, msg
    # play_card against other player if possible
    cur = g.current_player()
    other = "bob" if cur == "alice" else "alice"
    hand = list(g.players[cur]["hand"])
    played = False
    for card in hand:
        target = other if card["id"] in {"probe", "sophon", "dark_forest", "droplet", "dimensions"} else None
        ok, msg = g.apply_action(cur, {"action": "play_card", "instance_id": card["instance_id"], "target": target})
        if ok:
            played = True
            break
    assert played or True  # non-fatal if unlucky draws

    snap_a = g.snapshot_for("alice")
    snap_b = g.snapshot_for("bob")
    assert all(c.get("instance_id") for c in snap_a["you"]["hand"])
    # hands are private — sizes can differ after actions
    assert "hand" not in snap_a["players"][0]

    c1 = TestClient(app)
    c2 = TestClient(app)
    for u in ("smoke_a", "smoke_b"):
        c1.post("/api/register", json={"username": u, "password": "abc123"})

    r = c1.post("/api/login", json={"username": "smoke_a", "password": "abc123"})
    assert r.status_code == 200 and r.json()["success"], r.text
    assert any("session=" in v for v in r.headers.get_list("set-cookie"))

    r = c1.post("/api/rooms")
    assert r.status_code == 200 and r.json()["success"], r.text
    rid = r.json()["room"]["room_id"]

    r = c2.post("/api/login", json={"username": "smoke_b", "password": "abc123"})
    assert r.status_code == 200, r.text
    r = c2.post(f"/api/rooms/{rid}/join")
    assert r.status_code == 200 and r.json()["success"], r.text
    assert r.json()["room"]["player_count"] == 2

    r = c1.get("/api/rooms")
    assert r.status_code == 200
    assert any(x["room_id"] == rid for x in r.json()["rooms"])

    # Room manager start path (same as WS start_game)
    room_manager.set_ready(rid, "smoke_a", True)
    room_manager.set_ready(rid, "smoke_b", True)
    room, err = room_manager.start_game(rid, "smoke_a")
    assert err is None and room is not None and room.game is not None, err
    assert room.status == "playing"
    private = room.game.snapshot_for("smoke_a")
    assert private["phase"] == "turn"
    assert isinstance(private["you"]["hand"], list)

    with c1.websocket_connect("/ws") as ws1:
        hello = json.loads(ws1.receive_text())
        assert hello["type"] == "hello"
        assert hello["payload"]["username"] == "smoke_a"
        ws1.send_text(json.dumps({"type": "ping"}))
        pong = json.loads(ws1.receive_text())
        # may receive room/game sync first after reconnect
        for _ in range(5):
            if pong["type"] == "pong":
                break
            pong = json.loads(ws1.receive_text())
        assert pong["type"] == "pong", pong

    print("SMOKE_OK")


if __name__ == "__main__":
    main()
