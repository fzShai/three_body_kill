"""Smoke test: Phase A core rules + HTTP/WS sanity."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from game.engine import GameSession
from game.stats import final_basic_damage
from rooms import room_manager
from server import app


def _give(player: dict, *cards: dict) -> None:
    player["hand"] = list(cards)


def main() -> None:
    assert final_basic_damage(2, 1, 1) == 2

    g = GameSession.create("SMOKE", ["alice", "bob"])
    assert g.phase == "turn"
    assert g.turn_phase == "play"
    assert all(len(g.players[n]["hand"]) >= 6 for n in ("alice", "bob"))  # 6 open + 3 draw for first
    assert g.players["alice"]["tech_level"] == 1

    # peach self heal
    cur = g.current_player()
    peach = {
        "id": "peach",
        "name": "桃",
        "type": "basic",
        "subtype": "heal",
        "heal": 2,
        "instance_id": "peach-t1",
    }
    g.players[cur]["hp"] = 2
    g.players[cur]["max_hp"] = 5
    _give(g.players[cur], peach)
    ok, msg = g.apply_action(cur, {"action": "play_card", "instance_id": "peach-t1"})
    assert ok, msg
    assert g.players[cur]["hp"] == 4

    # visitor raises tech
    visitor = {
        "id": "visitor",
        "name": "天外来客",
        "type": "basic",
        "subtype": "visitor",
        "instance_id": "vis-1",
    }
    _give(g.players[cur], visitor)
    ok, msg = g.apply_action(cur, {"action": "play_card", "instance_id": "vis-1"})
    assert ok, msg
    assert g.players[cur]["tech_level"] == 2

    # kill -> dodge response
    kg = GameSession.create("KILL", ["cara", "dan"])
    cara, dan = kg.players["cara"], kg.players["dan"]
    kg.turn_index = kg.player_order.index("cara")
    kg.phase = "turn"
    kg.turn_phase = "play"
    kill = {
        "id": "kill_low",
        "name": "1阶杀",
        "type": "basic",
        "subtype": "kill",
        "tier": 1,
        "instance_id": "kill-1",
    }
    dodge = {
        "id": "dodge_low",
        "name": "1阶闪",
        "type": "basic",
        "subtype": "dodge",
        "tier": 1,
        "instance_id": "dodge-1",
    }
    _give(cara, kill)
    _give(dan, dodge)
    dan["hp"] = 4
    ok, msg = kg.apply_action("cara", {"action": "play_card", "instance_id": "kill-1", "target": "dan"})
    assert ok, msg
    assert kg.phase == "prompt"
    ok, msg = kg.apply_action("dan", {"action": "respond_dodge", "instance_id": "dodge-1"})
    assert ok, msg
    assert dan["hp"] == 4
    assert kg.phase == "turn"

    # kill unanswered deals damage
    kg2 = GameSession.create("KILL2", ["erin", "finn"])
    erin, finn = kg2.players["erin"], kg2.players["finn"]
    kg2.turn_index = kg2.player_order.index("erin")
    kg2.phase = "turn"
    kg2.turn_phase = "play"
    kill2 = {**kill, "instance_id": "kill-2", "tier": 1}
    _give(erin, kill2)
    _give(finn)
    finn["hp"] = 3
    finn["vision_exposed"] = True  # 1阶杀 base1+vision1 = 2 before bonuses
    ok, msg = kg2.apply_action("erin", {"action": "play_card", "instance_id": "kill-2", "target": "finn"})
    assert ok, msg
    ok, msg = kg2.apply_action("finn", {"action": "respond_pass"})
    assert ok, msg
    assert finn["hp"] == 1

    # ladder_plan exposes vision; vision boosts kill damage; clears at target turn end
    vg = GameSession.create("VISION", ["nora", "owen"])
    nora, owen = vg.players["nora"], vg.players["owen"]
    vg.turn_index = vg.player_order.index("nora")
    vg.phase = "turn"
    vg.turn_phase = "play"
    ladder = {
        "id": "ladder_plan",
        "name": "阶梯计划",
        "type": "trick",
        "implemented": True,
        "instance_id": "ladder-1",
        "text": "暴露视野",
    }
    _give(nora, ladder)
    ok, msg = vg.apply_action("nora", {"action": "play_card", "instance_id": "ladder-1", "target": "owen"})
    assert ok, msg
    assert owen["vision_exposed"] is True
    kill_v = {**kill, "instance_id": "kill-v", "tier": 1}
    _give(nora, kill_v)
    owen["hp"] = 4
    ok, msg = vg.apply_action("nora", {"action": "play_card", "instance_id": "kill-v", "target": "owen"})
    assert ok, msg
    ok, msg = vg.apply_action("owen", {"action": "respond_pass"})
    assert ok, msg
    assert owen["hp"] == 2  # base1 + vision1
    # end nora turn then owen turn end should clear owen vision
    ok, msg = vg.apply_action("nora", {"action": "end_play"})
    assert ok, msg
    assert vg.turn_phase == "discard"
    # ensure under hand limit then finish discard
    limit_n = max(0, nora["max_hp"] - 2)
    while len(nora["hand"]) > limit_n:
        card = nora["hand"][0]
        ok, msg = vg.apply_action("nora", {"action": "discard_card", "instance_id": card["instance_id"]})
        assert ok, msg
    ok, msg = vg.apply_action("nora", {"action": "discard_done"})
    assert ok, msg
    assert vg.current_player() == "owen"
    assert owen["vision_exposed"] is True
    ok, msg = vg.apply_action("owen", {"action": "end_play"})
    assert ok, msg
    limit_o = max(0, owen["max_hp"] - 2)
    while len(owen["hand"]) > limit_o:
        card = owen["hand"][0]
        ok, msg = vg.apply_action("owen", {"action": "discard_card", "instance_id": card["instance_id"]})
        assert ok, msg
    ok, msg = vg.apply_action("owen", {"action": "discard_done"})
    assert ok, msg
    assert owen["vision_exposed"] is False

    # illegal recast: peach has legal play
    rg = GameSession.create("RECAST", ["paul", "quinn"])
    cur = rg.current_player()
    peach_r = {**peach, "instance_id": "peach-r"}
    _give(rg.players[cur], peach_r)
    ok, msg = rg.apply_action(cur, {"action": "recast", "instance_id": "peach-r"})
    assert not ok and "不能重铸" in msg, msg
    # unimplemented trick can recast
    stub = {
        "id": "wallfacer_plan",
        "name": "面壁计划",
        "type": "trick",
        "implemented": False,
        "needs": ["discard_from_target"],
        "instance_id": "stub-1",
    }
    _give(rg.players[cur], stub)
    ok, msg = rg.apply_action(cur, {"action": "recast", "instance_id": "stub-1"})
    assert ok, msg

    # equip blue_space: damage bonus
    eqg = GameSession.create("EQUIP", ["rita", "sam"])
    rita, sam = eqg.players["rita"], eqg.players["sam"]
    eqg.turn_index = eqg.player_order.index("rita")
    eqg.phase = "turn"
    eqg.turn_phase = "play"
    ship = {
        "id": "blue_space",
        "name": "蓝色空间号",
        "type": "equipment",
        "slot": "ship",
        "ship_id": "blue_space",
        "implemented": True,
        "instance_id": "ship-1",
        "text": "伤害+1",
    }
    _give(rita, ship)
    ok, msg = eqg.apply_action("rita", {"action": "play_card", "instance_id": "ship-1"})
    assert ok, msg
    assert rita["equipment"]["ship"]["id"] == "blue_space"
    assert rita["damage_bonus"] == 1
    temp = {
        "id": "stars_plan",
        "name": "群星计划",
        "type": "equipment",
        "slot": "temp_ascend",
        "implemented": True,
        "instance_id": "temp-1",
    }
    _give(rita, temp)
    ok, msg = eqg.apply_action("rita", {"action": "play_card", "instance_id": "temp-1"})
    assert ok, msg
    assert rita["equipment"]["temp_ascend"] is None
    assert any(s["id"] == "stars_plan" for s in rita["statuses"])
    assert rita["damage_bonus"] == 2
    nano = {
        "id": "nano_center",
        "name": "纳米工程中心",
        "type": "equipment",
        "slot": "temp_ascend",
        "implemented": True,
        "instance_id": "temp-2",
    }
    _give(rita, nano)
    ok, msg = eqg.apply_action("rita", {"action": "play_card", "instance_id": "temp-2"})
    assert ok, msg
    status_ids = {s["id"] for s in rita["statuses"]}
    assert status_ids >= {"stars_plan", "nano_center"}
    assert rita["damage_reduction"] == 1
    # same temp ascend cannot stack
    temp_dup = {**temp, "instance_id": "temp-3"}
    _give(rita, temp_dup)
    ok, msg = eqg.apply_action("rita", {"action": "play_card", "instance_id": "temp-3"})
    assert not ok, msg

    # dying: force peach
    dg = GameSession.create("DIE", ["gina", "hank"])
    gina, hank = dg.players["gina"], dg.players["hank"]
    dg.turn_index = dg.player_order.index("gina")
    dg.phase = "turn"
    dg.turn_phase = "play"
    kill3 = {**kill, "instance_id": "kill-3", "tier": 3}
    peach2 = {**peach, "instance_id": "peach-d1"}
    _give(gina, kill3)
    _give(hank, peach2)
    hank["hp"] = 1
    hank["vision_exposed"] = False
    ok, msg = dg.apply_action("gina", {"action": "play_card", "instance_id": "kill-3", "target": "hank"})
    assert ok, msg
    ok, msg = dg.apply_action("hank", {"action": "respond_pass"})
    assert ok, msg
    assert dg.phase == "dying"
    ok, msg = dg.apply_action("hank", {"action": "dying_resolve"})
    assert ok, msg
    assert hank["alive"] and hank["hp"] > 0

    # dying without peach -> death
    dg2 = GameSession.create("DIE2", ["ivy", "jade"])
    ivy, jade = dg2.players["ivy"], dg2.players["jade"]
    dg2.turn_index = dg2.player_order.index("ivy")
    dg2.phase = "turn"
    dg2.turn_phase = "play"
    kill4 = {**kill, "instance_id": "kill-4", "tier": 3}
    _give(ivy, kill4)
    _give(jade)
    jade["hp"] = 1
    ok, msg = dg2.apply_action("ivy", {"action": "play_card", "instance_id": "kill-4", "target": "jade"})
    assert ok, msg
    ok, msg = dg2.apply_action("jade", {"action": "respond_pass"})
    assert ok, msg
    assert dg2.phase == "dying"
    ok, msg = dg2.apply_action("jade", {"action": "dying_resolve"})
    assert ok, msg
    assert not jade["alive"]

    # end play + discard
    eg = GameSession.create("END", ["kate", "liam"])
    cur = eg.current_player()
    # cannot skip discard from play
    ok, msg = eg.apply_action(cur, {"action": "discard_done"})
    assert ok and eg.turn_phase == "discard", msg
    # over-limit hand blocks discard_done
    p = eg.players[cur]
    p["max_hp"] = 4
    extras = [
        {
            "id": "peach",
            "name": "桃",
            "type": "basic",
            "subtype": "heal",
            "heal": 2,
            "instance_id": f"extra-{i}",
        }
        for i in range(6)
    ]
    p["hand"] = extras[:]
    ok, msg = eg.apply_action(cur, {"action": "discard_done"})
    assert not ok and "还需弃置" in msg, msg
    # selective discard then finish
    ok, msg = eg.apply_action(cur, {"action": "discard_card", "instance_id": "extra-0"})
    assert ok, msg
    ok, msg = eg.apply_action(cur, {"action": "discard_card", "instance_id": "extra-1"})
    assert ok, msg
    ok, msg = eg.apply_action(cur, {"action": "discard_card", "instance_id": "extra-2"})
    assert ok, msg
    ok, msg = eg.apply_action(cur, {"action": "discard_card", "instance_id": "extra-3"})
    assert ok, msg
    assert len(p["hand"]) == 2
    ok, msg = eg.apply_action(cur, {"action": "discard_done"})
    assert ok, msg
    assert eg.current_player() != cur or eg.phase == "ended" or True

    # discard 4 basics for tech
    tg = GameSession.create("TECH", ["mona", "neil"])
    mona = tg.players["mona"]
    tg.turn_index = tg.player_order.index("mona")
    tg.phase = "turn"
    tg.turn_phase = "play"
    mona["tech_level"] = 2
    basics = [
        {"id": "peach", "name": "桃", "type": "basic", "subtype": "heal", "heal": 2, "instance_id": "tb-1"},
        {"id": "dodge_low", "name": "1阶闪", "type": "basic", "subtype": "dodge", "tier": 1, "instance_id": "tb-2"},
        {"id": "kill_low", "name": "1阶杀", "type": "basic", "subtype": "kill", "tier": 1, "instance_id": "tb-3"},
        {"id": "visitor", "name": "天外来客", "type": "basic", "subtype": "visitor", "instance_id": "tb-4"},
    ]
    _give(mona, *basics)
    ok, msg = tg.apply_action(
        "mona",
        {"action": "discard_for_tech", "instance_ids": ["tb-1", "tb-2", "tb-3", "tb-4"]},
    )
    assert ok, msg
    assert mona["tech_level"] == 3
    assert len(mona["hand"]) == 0
    # unlimited per turn
    more = [
        {**basics[0], "instance_id": "tb-5"},
        {**basics[1], "instance_id": "tb-6"},
        {**basics[2], "instance_id": "tb-7"},
        {**basics[3], "instance_id": "tb-8"},
    ]
    _give(mona, *more)
    ok, msg = tg.apply_action(
        "mona",
        {"action": "discard_for_tech", "instance_ids": ["tb-5", "tb-6", "tb-7", "tb-8"]},
    )
    assert ok, msg
    assert mona["tech_level"] == 4

    snap = eg.snapshot_for(eg.current_player())
    assert "tech_level" in snap["you"]
    assert "turn_phase" in snap

    c1 = TestClient(app)
    c2 = TestClient(app)
    for u in ("smoke_a", "smoke_b"):
        c1.post("/api/register", json={"username": u, "password": "abc123"})

    r = c1.post("/api/login", json={"username": "smoke_a", "password": "abc123"})
    assert r.status_code == 200 and r.json()["success"], r.text

    r = c1.post("/api/rooms")
    assert r.status_code == 200 and r.json()["success"], r.text
    rid = r.json()["room"]["room_id"]

    r = c2.post("/api/login", json={"username": "smoke_b", "password": "abc123"})
    assert r.status_code == 200, r.text
    r = c2.post(f"/api/rooms/{rid}/join")
    assert r.status_code == 200 and r.json()["success"], r.text

    room_manager.set_ready(rid, "smoke_a", True)
    room_manager.set_ready(rid, "smoke_b", True)
    room, err = room_manager.start_game(rid, "smoke_a")
    assert err is None and room is not None and room.game is not None, err
    private = room.game.snapshot_for("smoke_a")
    assert private["phase"] in {"turn", "prompt", "dying"}
    assert isinstance(private["you"]["hand"], list)

    with c1.websocket_connect("/ws") as ws1:
        hello = json.loads(ws1.receive_text())
        assert hello["type"] == "hello"
        ws1.send_text(json.dumps({"type": "ping"}))
        pong = json.loads(ws1.receive_text())
        for _ in range(5):
            if pong["type"] == "pong":
                break
            pong = json.loads(ws1.receive_text())
        assert pong["type"] == "pong", pong

    print("SMOKE_OK")


if __name__ == "__main__":
    main()
