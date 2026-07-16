"""Verify disconnect policy: offline not kicked, host transfer, all-offline destroy."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import rooms
import server
from server import app, room_manager, ws_hub

LOG = ROOT / "debug-2b39ab.log"


def _register_login(client: TestClient, username: str) -> None:
    client.post("/api/register", json={"username": username, "password": "x"})
    client.post("/api/login", json={"username": username, "password": "x"})


def test_host_transfer() -> None:
    rooms.HOST_TRANSFER_SECONDS = 0.2
    server.HOST_TRANSFER_SECONDS = 0.2
    with TestClient(app) as c1, TestClient(app) as c2:
        _register_login(c1, "h1")
        _register_login(c2, "h2")
        r = c1.post("/api/rooms")
        rid = r.json()["room"]["room_id"]
        c2.post(f"/api/rooms/{rid}/join")
        ws_hub.set_user_room("h1", rid)
        ws_hub.set_user_room("h2", rid)
        with c1.websocket_connect("/ws") as ws:
            pass
        room = room_manager.get(rid)
        assert room and not room.find_player("h1").connected
        assert room.find_player("h1") is not None
        time.sleep(0.5)
        room = room_manager.get(rid)
        assert room and room.host == "h2", f"expected h2 host got {room.host if room else None}"


def test_all_offline_destroy() -> None:
    with TestClient(app) as c1, TestClient(app) as c2:
        _register_login(c1, "g1")
        _register_login(c2, "g2")
        r = c1.post("/api/rooms")
        rid = r.json()["room"]["room_id"]
        c2.post(f"/api/rooms/{rid}/join")
        room_manager.start_game(rid, "g1")
        ws_hub.set_user_room("g1", rid)
        ws_hub.set_user_room("g2", rid)
        with c1.websocket_connect("/ws") as ws:
            pass
        with c2.websocket_connect("/ws") as ws:
            pass
        assert room_manager.get(rid) is None


def test_skip_turn_when_current_offline() -> None:
    with TestClient(app) as c1, TestClient(app) as c2:
        _register_login(c1, "t1")
        _register_login(c2, "t2")
        r = c1.post("/api/rooms")
        rid = r.json()["room"]["room_id"]
        c2.post(f"/api/rooms/{rid}/join")
        room_manager.set_ready(rid, "t2", True)
        room_manager.start_game(rid, "t1")
        room = room_manager.get(rid)
        assert room and room.game
        first = room.game.current_player()
        ws_hub.set_user_room(first, rid)
        with (c1 if first == "t1" else c2).websocket_connect("/ws") as ws:
            pass
        room = room_manager.get(rid)
        assert room and room.game
        assert room.game.current_player() != first, f"turn should skip offline {first}"


def main() -> None:
    if LOG.exists():
        LOG.unlink()
    test_host_transfer()
    test_all_offline_destroy()
    test_skip_turn_when_current_offline()
    print("DISCONNECT_POLICY_OK")
    if LOG.exists():
        print(LOG.read_text(encoding="utf-8")[-800:])


if __name__ == "__main__":
    main()
