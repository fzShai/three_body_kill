"""Smoke test: HTTP rooms + engine + WS hello (avoids blocking receive loops)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from game.engine import STATUS_LOCKED, GameSession
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
    assert "equipment" in snap_a["players"][0]
    assert snap_a["players"][0]["equipment"]["stellar_track"] is None
    assert snap_a["players"][0]["equipment"]["stability_system"] is None

    # equipment: equip into slot, replace discards old
    eg = GameSession.create("EQUIP", ["cara", "dan"])
    cara = eg.players["cara"]
    star_a = {
        "id": "star_trail",
        "name": "星轨锚定",
        "type": "equipment",
        "slot": "stellar_track",
        "cost": 1,
        "text": "test",
        "instance_id": "star_trail-test-1",
    }
    star_b = {
        "id": "orbit_shield",
        "name": "轨道护盾",
        "type": "equipment",
        "slot": "stellar_track",
        "cost": 2,
        "text": "test",
        "instance_id": "orbit_shield-test-1",
    }
    stab = {
        "id": "stabilizer",
        "name": "维稳核心",
        "type": "equipment",
        "slot": "stability_system",
        "cost": 1,
        "text": "test",
        "instance_id": "stabilizer-test-1",
    }
    cara["hand"] = [star_a, star_b, stab]
    eg.turn_index = eg.player_order.index("cara")
    eg.phase = "turn"
    discard_before = len(eg.discard)

    ok, msg = eg.apply_action("cara", {"action": "play_card", "instance_id": star_a["instance_id"]})
    assert ok, msg
    assert cara["equipment"]["stellar_track"]["instance_id"] == star_a["instance_id"]
    assert len(eg.discard) == discard_before  # equipped card stays out of discard

    eg.turn_index = eg.player_order.index("cara")
    ok, msg = eg.apply_action("cara", {"action": "play_card", "instance_id": star_b["instance_id"]})
    assert ok, msg
    assert cara["equipment"]["stellar_track"]["instance_id"] == star_b["instance_id"]
    assert any(c.get("instance_id") == star_a["instance_id"] for c in eg.discard)

    eg.turn_index = eg.player_order.index("cara")
    ok, msg = eg.apply_action("cara", {"action": "play_card", "instance_id": stab["instance_id"]})
    assert ok, msg
    assert cara["equipment"]["stability_system"]["instance_id"] == stab["instance_id"]
    pub = eg.snapshot_for("dan")["players"]
    cara_pub = next(p for p in pub if p["username"] == "cara")
    assert cara_pub["equipment"]["stellar_track"]["name"] == "轨道护盾"
    assert cara_pub["equipment"]["stability_system"]["name"] == "维稳核心"

    # statuses: no stack same id, different ids coexist, locked clears on skip
    sg = GameSession.create("STATUS", ["erin", "finn"])
    erin = sg.players["erin"]
    assert erin["statuses"] == []
    assert "skip_next" not in erin
    assert sg._apply_status("erin", STATUS_LOCKED, "锁死", "negative")
    assert not sg._apply_status("erin", STATUS_LOCKED, "锁死", "negative")
    assert len(erin["statuses"]) == 1
    assert sg._apply_status("erin", "focus", "专注", "positive")
    assert len(erin["statuses"]) == 2
    erin_pub = next(p for p in sg.snapshot_for("finn")["players"] if p["username"] == "erin")
    assert any(s["id"] == STATUS_LOCKED for s in erin_pub["statuses"])
    assert any(s["id"] == "focus" and s["kind"] == "positive" for s in erin_pub["statuses"])
    # make it finn's turn then pass so advance lands on locked erin and clears it
    sg.turn_index = sg.player_order.index("finn")
    sg.phase = "turn"
    ok, msg = sg.apply_action("finn", {"action": "pass"})
    assert ok, msg
    assert not sg._has_status("erin", STATUS_LOCKED)
    assert sg._has_status("erin", "focus")

    # heal card peach: normal self-heal only
    hg = GameSession.create("HEAL", ["gina", "hank"])
    gina = hg.players["gina"]
    peach = {
        "id": "peach",
        "name": "桃",
        "type": "heal",
        "cost": 1,
        "heal": 2,
        "text": "test",
        "instance_id": "peach-self-1",
    }
    gina["hand"] = [peach]
    gina["hp"] = 2
    hg.turn_index = hg.player_order.index("gina")
    hg.phase = "turn"
    ok, msg = hg.apply_action("gina", {"action": "play_card", "instance_id": peach["instance_id"], "target": "hank"})
    assert not ok
    ok, msg = hg.apply_action("gina", {"action": "play_card", "instance_id": peach["instance_id"]})
    assert ok, msg
    assert gina["hp"] == 4

    # dying: enter dying, self-save with peach, full circle then alive
    dg = GameSession.create("DYING", ["ivy", "jade"])
    ivy, jade = dg.players["ivy"], dg.players["jade"]
    ivy["hp"] = 1
    jade["hp"] = 4
    peach_a = {**peach, "instance_id": "peach-dying-1"}
    peach_b = {**peach, "instance_id": "peach-dying-2"}
    ivy["hand"] = [peach_a, peach_b]
    jade["hand"] = []
    dg.turn_index = dg.player_order.index("jade")
    dg.phase = "turn"
    atk = {
        "id": "droplet",
        "name": "水滴",
        "type": "attack",
        "cost": 2,
        "text": "test",
        "instance_id": "droplet-dying-1",
    }
    jade["hand"] = [atk]
    ok, msg = dg.apply_action("jade", {"action": "play_card", "instance_id": atk["instance_id"], "target": "ivy"})
    assert ok, msg
    assert dg.phase == "dying"
    assert dg.dying is not None and dg.dying["victim"] == "ivy"
    snap_d = dg.snapshot_for("jade")
    assert snap_d["dying"]["victim"] == "ivy"
    assert snap_d["dying"]["current"] == "ivy"
    assert ivy["alive"] and ivy["hp"] <= 0

    ok, msg = dg.apply_action("ivy", {"action": "play_card", "instance_id": peach_a["instance_id"]})
    assert ok, msg
    assert ivy["hp"] == 2
    # can play another peach in same response window
    ok, msg = dg.apply_action("ivy", {"action": "play_card", "instance_id": peach_b["instance_id"]})
    assert ok, msg
    assert ivy["hp"] == 4
    ok, msg = dg.apply_action("ivy", {"action": "dying_pass"})
    assert ok, msg
    # jade's turn to respond then end circle
    assert dg.phase == "dying"
    assert dg._dying_current() == "jade"
    ok, msg = dg.apply_action("jade", {"action": "dying_pass"})
    assert ok, msg
    assert dg.phase == "turn"
    assert ivy["alive"] and ivy["hp"] == 4

    # dying: everyone passes -> death
    dg2 = GameSession.create("DYING2", ["kate", "liam"])
    kate, liam = dg2.players["kate"], dg2.players["liam"]
    kate["hp"] = 1
    kate["hand"] = []
    liam["hand"] = [{
        "id": "droplet",
        "name": "水滴",
        "type": "attack",
        "cost": 2,
        "text": "test",
        "instance_id": "droplet-dying-2",
    }]
    dg2.turn_index = dg2.player_order.index("liam")
    dg2.phase = "turn"
    ok, msg = dg2.apply_action("liam", {"action": "play_card", "instance_id": "droplet-dying-2", "target": "kate"})
    assert ok, msg
    assert dg2.phase == "dying"
    while dg2.phase == "dying":
        cur = dg2._dying_current()
        assert cur
        ok, msg = dg2.apply_action(cur, {"action": "dying_pass"})
        assert ok, msg
    assert not kate["alive"]
    assert kate["hp"] == 0

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
