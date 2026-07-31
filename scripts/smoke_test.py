"""Smoke test: Phase A core rules + HTTP/WS sanity."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
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


def _blank_skills(session: GameSession, *names: str) -> None:
    """Disable role skills for baseline rule tests."""
    for n in names:
        p = session.players[n]
        p["skills"] = []
        p["tech_level"] = 1
        p["statuses"] = [s for s in p["statuses"] if s.get("id") != "skills_sealed"]
    if session.phase == "prompt":
        session.prompt = None
        session.phase = "turn"
        session.turn_phase = "play"


def _as_role(session: GameSession, username: str, role_id: str) -> None:
    role = next(r for r in session.roles_catalog if r["id"] == role_id)
    p = session.players[username]
    p["role_id"] = role["id"]
    p["role_name"] = role["name"]
    p["skills"] = deepcopy(role.get("skills") or [])
    p["max_hp"] = role["hp"]
    p["hp"] = min(p["hp"], role["hp"])
    p["tech_level"] = 4 if role_id == "guan_yifan" else 1


KNOWN_ROLES = {
    "guan_yifan",
    "friss",
    "luo_ji",
}


def _clear_prompt_to_turn(session: GameSession) -> None:
    """Dismiss opening skill prompts so baseline tests can run."""
    while session.phase == "prompt" and session.prompt:
        ptype = session.prompt.get("type")
        who = str(session.prompt.get("to"))
        if ptype == "wander_draw":
            session.apply_action(who, {"action": "wander_pass"})
        else:
            session.prompt = None
            session.phase = "turn"
            break


def main() -> None:
    assert final_basic_damage(2, 1, 1) == 2

    g = GameSession.create("SMOKE", ["alice", "bob"])
    _clear_prompt_to_turn(g)
    assert g.phase == "turn"
    assert g.turn_phase == "play"
    assert all(len(g.players[n]["hand"]) >= 6 for n in ("alice", "bob"))
    assert {g.players[n]["role_id"] for n in ("alice", "bob")} <= KNOWN_ROLES
    snap_alice = g.snapshot_for("alice")
    bob_pub = next(p for p in snap_alice["players"] if p["username"] == "bob")
    assert bob_pub.get("role_name"), "对局中应公开他人角色名"
    _blank_skills(g, "alice", "bob")
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
    snap = g.snapshot_for(cur)
    assert snap.get("stage", {}).get("kind") == "heal"
    assert snap["stage"].get("card", {}).get("name") == "桃"

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

    # 科技满级：天外来客不可打出，可重铸
    g.players[cur]["tech_level"] = 6
    g.phase = "turn"
    g.turn_phase = "play"
    g.prompt = None
    vis_full = {**visitor, "instance_id": "vis-full"}
    _give(g.players[cur], vis_full)
    ok, msg = g.apply_action(cur, {"action": "play_card", "instance_id": "vis-full"})
    assert not ok and "满级" in msg, msg
    ok, msg = g.apply_action(cur, {"action": "recast", "instance_id": "vis-full"})
    assert ok, msg
    g.players[cur]["tech_level"] = 2

    # kill -> dodge response
    kg = GameSession.create("KILL", ["cara", "dan"])
    _clear_prompt_to_turn(kg)
    _blank_skills(kg, "cara", "dan")
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
    _blank_skills(kg2, "erin", "finn")
    erin, finn = kg2.players["erin"], kg2.players["finn"]
    kg2.turn_index = kg2.player_order.index("erin")
    kg2.phase = "turn"
    kg2.turn_phase = "play"
    kill2 = {**kill, "instance_id": "kill-2", "tier": 1}
    _give(erin, kill2)
    _give(finn)
    finn["hp"] = 3
    finn["vision_exposed"] = True
    ok, msg = kg2.apply_action("erin", {"action": "play_card", "instance_id": "kill-2", "target": "finn"})
    assert ok, msg
    ok, msg = kg2.apply_action("finn", {"action": "respond_pass"})
    assert ok, msg
    assert finn["hp"] == 1

    # ladder_plan
    vg = GameSession.create("VISION", ["nora", "owen"])
    _blank_skills(vg, "nora", "owen")
    nora, owen = vg.players["nora"], vg.players["owen"]
    vg.turn_index = vg.player_order.index("nora")
    vg.phase = "turn"
    vg.turn_phase = "play"
    nora["hand"] = []
    owen["hand"] = []
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
    assert owen["hp"] == 2
    ok, msg = vg.apply_action("nora", {"action": "end_play"})
    assert ok, msg
    if vg.current_player() == "nora" and vg.turn_phase == "discard":
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
    if vg.current_player() == "owen" and vg.turn_phase == "discard":
        limit_o = max(0, owen["max_hp"] - 2)
        while len(owen["hand"]) > limit_o:
            card = owen["hand"][0]
            ok, msg = vg.apply_action("owen", {"action": "discard_card", "instance_id": card["instance_id"]})
            assert ok, msg
        ok, msg = vg.apply_action("owen", {"action": "discard_done"})
        assert ok, msg
    assert owen["vision_exposed"] is False

    # illegal recast / privacy
    rg = GameSession.create("RECAST", ["paul", "quinn"])
    _blank_skills(rg, "paul", "quinn")
    cur = rg.current_player()
    peach_r = {**peach, "instance_id": "peach-r"}
    _give(rg.players[cur], peach_r)
    ok, msg = rg.apply_action(cur, {"action": "recast", "instance_id": "peach-r"})
    assert not ok and "不能重铸" in msg, msg
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
    drawn_name = rg.players[cur]["hand"][0]["name"]
    assert "重铸为" in msg and drawn_name in msg, msg
    assert all("摸到" not in line for line in rg.log)
    assert not any(drawn_name in line and "重铸" in line for line in rg.log)

    lg = GameSession.create("LADDER", ["uma", "vic"])
    _blank_skills(lg, "uma", "vic")
    uma, vic = lg.players["uma"], lg.players["vic"]
    lg.turn_index = lg.player_order.index("uma")
    lg.phase = "turn"
    lg.turn_phase = "play"
    uma["hand"] = []
    vic["hand"] = []
    ladder_a = {
        "id": "ladder_plan",
        "name": "阶梯计划",
        "type": "trick",
        "implemented": True,
        "instance_id": "ladder-a",
    }
    ladder_b = {**ladder_a, "instance_id": "ladder-b"}
    _give(uma, ladder_a, ladder_b)
    ok, msg = lg.apply_action("uma", {"action": "play_card", "instance_id": "ladder-a", "target": "vic"})
    assert ok, msg
    assert vic["vision_exposed"] is True
    ok, msg = lg.apply_action("uma", {"action": "play_card", "instance_id": "ladder-b", "target": "vic"})
    assert not ok and "已暴露" in msg, msg
    ok, msg = lg.apply_action("uma", {"action": "recast", "instance_id": "ladder-b"})
    assert ok, msg

    eqg = GameSession.create("EQUIP", ["rita", "sam"])
    _blank_skills(eqg, "rita", "sam")
    rita = eqg.players["rita"]
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
    }
    _give(rita, ship)
    ok, msg = eqg.apply_action("rita", {"action": "play_card", "instance_id": "ship-1"})
    assert ok, msg
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
    assert any(s["id"] == "stars_plan" for s in rita["statuses"])

    dg = GameSession.create("DIE", ["gina", "hank"])
    _blank_skills(dg, "gina", "hank")
    gina, hank = dg.players["gina"], dg.players["hank"]
    dg.turn_index = dg.player_order.index("gina")
    dg.phase = "turn"
    dg.turn_phase = "play"
    kill3 = {**kill, "instance_id": "kill-3", "tier": 3}
    peach2 = {**peach, "instance_id": "peach-d1"}
    _give(gina, kill3)
    _give(hank, peach2)
    hank["hp"] = 1
    ok, msg = dg.apply_action("gina", {"action": "play_card", "instance_id": "kill-3", "target": "hank"})
    assert ok, msg
    ok, msg = dg.apply_action("hank", {"action": "respond_pass"})
    assert ok, msg
    assert dg.phase == "dying"
    ok, msg = dg.apply_action("hank", {"action": "dying_resolve"})
    assert ok, msg
    assert hank["alive"] and hank["hp"] > 0

    dg3 = GameSession.create("DIE3", ["kyle", "lena"])
    _blank_skills(dg3, "kyle", "lena")
    kyle, lena = dg3.players["kyle"], dg3.players["lena"]
    dg3.turn_index = dg3.player_order.index("kyle")
    dg3.phase = "turn"
    dg3.turn_phase = "play"
    kill5 = {**kill, "instance_id": "kill-5", "tier": 3}
    peach_save = {**peach, "instance_id": "peach-save"}
    _give(kyle, kill5, peach_save)
    _give(lena)
    lena["hp"] = 1
    ok, msg = dg3.apply_action("kyle", {"action": "play_card", "instance_id": "kill-5", "target": "lena"})
    assert ok, msg
    ok, msg = dg3.apply_action("lena", {"action": "respond_pass"})
    assert ok, msg
    ok, msg = dg3.apply_action("kyle", {"action": "play_card", "instance_id": "peach-save"})
    assert ok, msg
    assert lena["alive"] and lena["hp"] > 0

    dg2 = GameSession.create("DIE2", ["ivy", "jade"])
    _blank_skills(dg2, "ivy", "jade")
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
    ok, msg = dg2.apply_action("jade", {"action": "dying_resolve"})
    assert ok, msg
    assert not jade["alive"]

    eg = GameSession.create("END", ["kate", "liam"])
    _blank_skills(eg, "kate", "liam")
    cur = eg.current_player()
    p = eg.players[cur]
    p["max_hp"] = 4
    p["hand"] = [
        {
            "id": "peach",
            "name": "桃",
            "type": "basic",
            "subtype": "heal",
            "heal": 2,
            "instance_id": f"pad-{i}",
        }
        for i in range(5)
    ]
    ok, msg = eg.apply_action(cur, {"action": "discard_done"})
    assert ok and eg.turn_phase == "discard", msg
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
    for i in range(4):
        ok, msg = eg.apply_action(cur, {"action": "discard_card", "instance_id": f"extra-{i}"})
        assert ok, msg
    ok, msg = eg.apply_action(cur, {"action": "discard_done"})
    assert ok, msg

    # 无需弃牌时跳过弃牌阶段
    skip = GameSession.create("SKIP_DISC", ["sk1", "sk2"])
    _blank_skills(skip, "sk1", "sk2")
    skip.turn_index = skip.player_order.index("sk1")
    skip.phase = "turn"
    skip.turn_phase = "play"
    skip.players["sk1"]["max_hp"] = 5
    skip.players["sk1"]["hand"] = [
        {"id": "peach", "name": "桃", "type": "basic", "subtype": "heal", "instance_id": "sk-p"}
    ]
    ok, msg = skip.apply_action("sk1", {"action": "end_play"})
    assert ok and "无需弃牌" in msg, msg
    assert skip.current_player() == "sk2"
    assert skip.turn_phase != "discard" or skip.current_player() != "sk1"

    tg = GameSession.create("TECH", ["mona", "neil"])
    _blank_skills(tg, "mona", "neil")
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

    # 关一帆星舰 + 流浪
    gy = GameSession.create("GUAN", ["guan", "foe"])
    _as_role(gy, "guan", "guan_yifan")
    _blank_skills(gy, "foe")
    assert gy.players["guan"]["tech_level"] == 4
    gy.turn_index = gy.player_order.index("guan")
    gy.phase = "turn"
    gy.turn_phase = "play"
    _give(gy.players["guan"])
    ok, msg = gy.apply_action("guan", {"action": "end_play"})
    assert ok, msg
    if gy.turn_phase == "discard" and gy.current_player() == "guan":
        ok, msg = gy.apply_action("guan", {"action": "discard_done"})
        assert ok, msg
    assert gy.phase == "prompt" and gy.prompt and gy.prompt.get("type") == "wander_draw"
    conf = gy.prompt.get("confirm") or {}
    assert conf.get("accept_action") == "wander_accept"
    assert conf.get("pass_action") == "wander_pass"
    assert gy.players["guan"]["tech_level"] == 3
    ok, msg = gy.apply_action("guan", {"action": "wander_pass"})
    assert ok, msg
    assert gy.current_player() == "foe"

    # 弗雷斯土著 + 凝聚
    fr = GameSession.create("FRISS", ["friss", "prey"])
    _as_role(fr, "friss", "friss")
    _blank_skills(fr, "prey")
    fr.turn_index = fr.player_order.index("friss")
    fr.phase = "turn"
    fr.turn_phase = "play"
    prey = fr.players["prey"]
    prey["hp"] = 6
    kill_n = {**kill, "instance_id": "kill-native", "tier": 1}
    _give(fr.players["friss"], kill_n)
    ok, msg = fr.apply_action("friss", {"action": "play_card", "instance_id": "kill-native", "target": "prey"})
    assert ok, msg
    ok, msg = fr.apply_action("prey", {"action": "respond_pass"})
    assert ok, msg
    assert fr.phase == "prompt"
    assert fr.prompt and fr.prompt.get("is_native_repeat")
    assert prey["hp"] == 5
    ok, msg = fr.apply_action("prey", {"action": "respond_pass"})
    assert ok, msg
    assert prey["hp"] == 4
    assert fr.phase == "turn"
    # 土著桃：双疗
    fr.players["friss"]["hp"] = 2
    peach_n = {**peach, "instance_id": "peach-native"}
    _give(fr.players["friss"], peach_n)
    ok, msg = fr.apply_action("friss", {"action": "play_card", "instance_id": "peach-native"})
    assert ok, msg
    assert fr.players["friss"]["hp"] == 6  # 2 + 2 + 土著再 2
    assert any("土著" in line and "桃" in line for line in fr.log)
    vis_c = {**visitor, "instance_id": "vis-cohesion"}
    _give(fr.players["friss"], vis_c)
    ok, msg = fr.apply_action("friss", {"action": "recast", "instance_id": "vis-cohesion"})
    assert ok, msg
    # 土著红岸：摸 2+2
    fr.phase = "turn"
    fr.turn_phase = "play"
    fr.prompt = None
    fr._pending_trick = None
    fr.players["friss"]["red_coast_used"] = False
    for n in fr.player_order:
        if n != "friss":
            fr.players[n]["hand"] = []
    hand_before = len(fr.players["friss"]["hand"])
    red_n = {
        "id": "red_coast",
        "name": "红岸计划",
        "type": "trick",
        "implemented": True,
        "instance_id": "red-native",
        "pool_entry": 19,
    }
    fr.players["friss"]["hand"].append(red_n)
    ok, msg = fr.apply_action("friss", {"action": "play_card", "instance_id": "red-native"})
    assert ok, msg
    assert len(fr.players["friss"]["hand"]) == hand_before + 4
    assert any("土著" in line and "红岸" in line for line in fr.log)
    # 土著面壁：弃两次
    fr.phase = "turn"
    fr.turn_phase = "play"
    _give(prey)  # 清空以免思想钢印打断
    for i in range(4):
        prey["hand"].append({
            "id": "peach",
            "name": "桃",
            "type": "basic",
            "subtype": "heal",
            "instance_id": f"prey-pad-{i}",
            "implemented": True,
        })
    prey_hand0 = len(prey["hand"])
    prey["vision_exposed"] = False
    wf_n = {
        "id": "wallfacer_plan",
        "name": "面壁计划",
        "type": "trick",
        "implemented": True,
        "instance_id": "wf-native",
        "pool_entry": 16,
    }
    _give(fr.players["friss"], wf_n)
    ok, msg = fr.apply_action("friss", {"action": "play_card", "instance_id": "wf-native", "target": "prey"})
    assert ok, msg
    assert len(prey["hand"]) == prey_hand0 - 2  # 未暴露各弃 1，土著再弃 1
    assert any("土著" in line and "面壁" in line for line in fr.log)
    # 土著古筝：选摸牌 → 摸 2+2
    fr.phase = "turn"
    fr.turn_phase = "play"
    _give(prey)  # 避免打断
    cost = {
        "id": "peach",
        "name": "桃",
        "type": "basic",
        "subtype": "heal",
        "instance_id": "gz-cost",
        "implemented": True,
    }
    gz_n = {
        "id": "guzheng_plan",
        "name": "古筝计划",
        "type": "trick",
        "implemented": True,
        "instance_id": "gz-native",
        "pool_entry": 18,
    }
    _give(fr.players["friss"], gz_n, cost)
    hand_before = len(fr.players["friss"]["hand"])
    ok, msg = fr.apply_action(
        "friss",
        {"action": "play_card", "instance_id": "gz-native", "discard_instance_id": "gz-cost"},
    )
    assert ok, msg
    assert fr.prompt and fr.prompt.get("will_native")
    ok, msg = fr.apply_action("friss", {"action": "choose", "choice": "draw2"})
    assert ok, msg
    # 打出古筝+弃 cost 共 -2，摸 2+2 = +4，净 +2
    assert len(fr.players["friss"]["hand"]) == hand_before - 2 + 4
    assert any("土著" in line and "古筝" in line for line in fr.log)

    # 罗辑执剑人：视野暴露不可被杀；对暴露目标杀不可闪
    lj = GameSession.create("LUOJI", ["luo", "foe"])
    _as_role(lj, "luo", "luo_ji")
    _blank_skills(lj, "foe")
    lj.turn_index = lj.player_order.index("foe")
    lj.phase = "turn"
    lj.turn_phase = "play"
    lj.players["luo"]["vision_exposed"] = True
    kill_lj = {
        "id": "kill_low",
        "name": "1阶杀",
        "type": "basic",
        "subtype": "kill",
        "tier": 1,
        "instance_id": "kill-lj",
    }
    _give(lj.players["foe"], kill_lj)
    ok, msg = lj.apply_action("foe", {"action": "play_card", "instance_id": "kill-lj", "target": "luo"})
    assert not ok and "执剑人" in msg
    lj.players["luo"]["vision_exposed"] = False
    lj.players["foe"]["vision_exposed"] = True
    lj.turn_index = lj.player_order.index("luo")
    kill_lj2 = {**kill_lj, "instance_id": "kill-lj2"}
    _give(lj.players["luo"], kill_lj2)
    dodge_lj = {
        "id": "dodge_low",
        "name": "1阶闪",
        "type": "basic",
        "subtype": "dodge",
        "tier": 1,
        "instance_id": "dodge-lj",
    }
    _give(lj.players["foe"], dodge_lj)
    hp_before = lj.players["foe"]["hp"]
    ok, msg = lj.apply_action("luo", {"action": "play_card", "instance_id": "kill-lj2", "target": "foe"})
    assert ok, msg
    assert lj.players["foe"]["hp"] < hp_before

    # you.actions：终极规律号主动技由 snapshot 下发
    law = GameSession.create("LAW", ["law", "bot"])
    _blank_skills(law, "law", "bot")
    law.turn_index = law.player_order.index("law")
    law.phase = "turn"
    law.turn_phase = "play"
    law_ship = {
        "id": "ultimate_law",
        "name": "终极规律号",
        "type": "equipment",
        "slot": "ship",
        "ship_id": "ultimate_law",
        "implemented": True,
        "instance_id": "law-ship",
    }
    _give(law.players["law"], law_ship)
    ok, msg = law.apply_action("law", {"action": "play_card", "instance_id": "law-ship"})
    assert ok, msg
    snap_law = law.snapshot_for("law")
    assert any(a.get("id") == "ultimate_law" for a in (snap_law["you"].get("actions") or []))
    _give(law.players["bot"], {"id": "peach", "name": "桃", "subtype": "heal", "instance_id": "bot-p", "implemented": True})
    ok, msg = law.apply_action("law", {"action": "ultimate_law", "target": "bot"})
    assert ok, msg
    snap_law2 = law.snapshot_for("law")
    assert not any(a.get("id") == "ultimate_law" for a in (snap_law2["you"].get("actions") or []))

    # 新装备/状态：小宇宙护盾 / 星环号不可被杀 / 太阳系观测
    eqn = GameSession.create("EQUIP_NEW", ["eq", "bot"])
    _blank_skills(eqn, "eq", "bot")
    eqn.turn_index = eqn.player_order.index("eq")
    eqn.phase = "turn"
    eqn.turn_phase = "play"
    eqn.prompt = None
    eqn._pending_trick = None
    for n in eqn.player_order:
        eqn.players[n]["hand"] = []
    micro = {
        "id": "micro_universe",
        "name": "小宇宙",
        "type": "trick",
        "implemented": True,
        "instance_id": "micro-1",
    }
    _give(eqn.players["eq"], micro)
    ok, msg = eqn.apply_action("eq", {"action": "play_card", "instance_id": "micro-1"})
    assert ok, msg
    assert eqn.players["eq"]["shield"] == 5
    assert any(s["id"] == "micro_universe" for s in eqn.players["eq"]["statuses"])
    eqn._deal_damage("bot", "eq", 3)
    assert eqn.players["eq"]["shield"] == 2
    assert eqn.players["eq"]["hp"] == eqn.players["eq"]["max_hp"]

    star = {
        "id": "star_ring",
        "name": "星环号",
        "type": "equipment",
        "slot": "ship",
        "ship_id": "star_ring",
        "implemented": True,
        "instance_id": "star-1",
    }
    _give(eqn.players["eq"], star)
    ok, msg = eqn.apply_action("eq", {"action": "play_card", "instance_id": "star-1"})
    assert ok, msg
    eqn.turn_index = eqn.player_order.index("bot")
    kill_sr = {
        "id": "kill_low",
        "name": "1阶杀",
        "type": "basic",
        "subtype": "kill",
        "tier": 1,
        "instance_id": "kill-sr",
    }
    _give(eqn.players["bot"], kill_sr)
    ok, msg = eqn.apply_action("bot", {"action": "play_card", "instance_id": "kill-sr", "target": "eq"})
    assert not ok and "星环" in msg

    # 甲栏已满时深海液可重铸；空槽时不可重铸
    eqr = GameSession.create("ARMOR_RECAST", ["arm", "bot"])
    _blank_skills(eqr, "arm", "bot")
    eqr.turn_index = eqr.player_order.index("arm")
    eqr.phase = "turn"
    eqr.turn_phase = "play"
    arm = eqr.players["arm"]
    deep = {
        "id": "deep_sea",
        "name": "深海液",
        "type": "equipment",
        "slot": "armor",
        "armor_id": "deep_sea",
        "implemented": True,
        "instance_id": "deep-1",
    }
    _give(arm, deep)
    ok, msg = eqr.apply_action("arm", {"action": "recast", "instance_id": "deep-1"})
    assert not ok and "不能重铸" in msg, msg
    arm["equipment"]["armor"] = {
        "id": "eco_bottle",
        "name": "生态瓶",
        "slot": "armor",
        "implemented": True,
    }
    _give(arm, {**deep, "instance_id": "deep-2"})
    ok, msg = eqr.apply_action("arm", {"action": "recast", "instance_id": "deep-2"})
    assert ok, msg

    # 球状闪电封印流浪；星舰仍生效；封印在其回合结束清除
    bl = GameSession.create("BALL", ["seer", "target"])
    _as_role(bl, "target", "guan_yifan")
    _blank_skills(bl, "seer")
    bl.turn_index = bl.player_order.index("seer")
    bl.phase = "turn"
    bl.turn_phase = "play"
    bl.players["seer"]["hand"] = []
    bl.players["target"]["hand"] = []
    ball = {
        "id": "ball_lightning",
        "name": "球状闪电",
        "type": "trick",
        "implemented": True,
        "instance_id": "ball-1",
    }
    _give(bl.players["seer"], ball)
    ok, msg = bl.apply_action("seer", {"action": "play_card", "instance_id": "ball-1", "target": "target"})
    assert ok, msg
    assert any(s["id"] == "skills_sealed" for s in bl.players["target"]["statuses"])
    _give(bl.players["seer"])
    ok, msg = bl.apply_action("seer", {"action": "end_play"})
    assert ok, msg
    if bl.turn_phase == "discard" and bl.current_player() == "seer":
        ok, msg = bl.apply_action("seer", {"action": "discard_done"})
        assert ok, msg
    assert bl.current_player() == "target"
    tech_before = bl.players["target"]["tech_level"]
    _give(bl.players["target"])
    bl.phase = "turn"
    bl.turn_phase = "play"
    ok, msg = bl.apply_action("target", {"action": "end_play"})
    assert ok, msg
    if bl.turn_phase == "discard" and bl.current_player() == "target":
        ok, msg = bl.apply_action("target", {"action": "discard_done"})
        assert ok, msg
    assert bl.players["target"]["tech_level"] == max(1, tech_before - 1)
    assert not (bl.prompt and bl.prompt.get("type") == "wander_draw")
    assert not any(s["id"] == "skills_sealed" for s in bl.players["target"]["statuses"])

    # --- Wave 1–5 trick smoke (blank hands to avoid interrupt pollution) ---
    def _trick(session, actor, card, **action_extra):
        session.phase = "turn"
        session.turn_phase = "play"
        session.prompt = None
        session._pending_trick = None
        for n in session.player_order:
            session.players[n]["hand"] = []
        session.players[actor]["hand"] = [card]
        payload = {"action": "play_card", "instance_id": card["instance_id"], **action_extra}
        return session.apply_action(actor, payload)

    tg = GameSession.create("TRICKS", ["t1", "t2"])
    _blank_skills(tg, "t1", "t2")
    tg.turn_index = tg.player_order.index("t1")
    for n in ("t1", "t2"):
        tg.players[n]["max_hp"] = 5
        tg.players[n]["hp"] = 4

    ok, msg = _trick(tg, "t1", {"id": "sophon", "name": "智子", "type": "trick", "implemented": True, "instance_id": "sp1"}, target="t2")
    assert ok, msg
    assert any(s["id"] == "tech_lock" for s in tg.players["t2"]["statuses"]), msg

    ok, msg = _trick(tg, "t1", {"id": "curtain", "name": "帷幕", "type": "trick", "implemented": True, "instance_id": "cu1"})
    assert ok, msg
    assert len(tg.players["t1"]["hand"]) >= 1

    tg.players["t2"]["hand"] = [{"id": "peach", "name": "桃", "instance_id": "px"}]
    tg.players["t2"]["vision_exposed"] = True
    ok, msg = _trick(tg, "t1", {"id": "wallfacer_plan", "name": "面壁", "type": "trick", "implemented": True, "instance_id": "wf1"}, target="t2")
    assert ok, msg
    assert len(tg.players["t2"]["hand"]) == 0

    tg.players["t2"]["vision_exposed"] = False
    ok, msg = _trick(tg, "t1", {"id": "broadcast", "name": "广播", "type": "trick", "implemented": True, "instance_id": "br2"})
    assert ok, msg
    assert tg.players["t2"]["vision_exposed"]

    # toxic with response window
    hp_before = tg.players["t2"]["hp"]
    ok, msg = _trick(tg, "t1", {"id": "toxic_water", "name": "剧毒", "type": "trick", "implemented": True, "instance_id": "tw1"})
    assert ok, msg
    assert tg.phase == "prompt" and tg.prompt and tg.prompt.get("type") == "respond_toxic"
    ok, msg = tg.apply_action("t2", {"action": "respond_pass"})
    assert ok, msg
    assert tg.players["t2"]["hp"] < hp_before

    ok, msg = _trick(tg, "t1", {"id": "cradle", "name": "摇篮", "type": "trick", "implemented": True, "instance_id": "cr1"})
    assert ok and any(s["id"] == "cradle" for s in tg.players["t1"]["statuses"]), msg

    # 已有摇篮：不可再打，可重铸
    ok, msg = _trick(tg, "t1", {"id": "cradle", "name": "摇篮", "type": "trick", "implemented": True, "instance_id": "cr2"})
    assert not ok and "重铸" in msg, msg
    tg.phase = "turn"
    tg.turn_phase = "play"
    tg.prompt = None
    tg._pending_trick = None
    for n in tg.player_order:
        tg.players[n]["hand"] = []
    tg.players["t1"]["hand"] = [
        {"id": "cradle", "name": "摇篮", "type": "trick", "implemented": True, "instance_id": "cr-recast"}
    ]
    ok, msg = tg.apply_action("t1", {"action": "recast", "instance_id": "cr-recast"})
    assert ok, msg
    assert any(s["id"] == "cradle" for s in tg.players["t1"]["statuses"])

    max_before = tg.players["t1"]["max_hp"]
    ok, msg = _trick(tg, "t1", {"id": "hibernation", "name": "冬眠", "type": "trick", "implemented": True, "instance_id": "hi1"})
    assert ok and any(s["id"] == "hibernation" for s in tg.players["t1"]["statuses"]), msg
    assert tg.players["t1"]["max_hp"] == max_before - 2

    # clear hibernation so later targeted tricks on t1 still work if needed
    tg._remove_status("t1", "hibernation")
    tg.players["t1"]["hibernation_clear_at_turn_start"] = False

    ok, msg = _trick(tg, "t1", {"id": "deterrence", "name": "威慑", "type": "trick", "implemented": True, "instance_id": "de1"})
    assert ok and tg.players["t1"].get("deterrence_extra_target"), msg

    ok, msg = _trick(tg, "t1", {"id": "swordholder", "name": "执剑", "type": "trick", "implemented": True, "instance_id": "sw1"})
    assert ok and tg.players["t1"].get("swordholder_ready"), msg

    tg.players["t2"]["vision_exposed"] = False
    tg.players["t2"]["hp"] = 5
    tg.dying = None
    tg.phase = "turn"
    ok, msg = _trick(tg, "t1", {"id": "dual_vector", "name": "二向箔", "type": "trick", "implemented": True, "instance_id": "dv1"}, target="t2")
    assert ok, msg
    assert tg.players["t2"]["hp"] <= 2  # 5-3 true, maybe heal from swordholder on t1

    # reset dying / phase for subsequent tricks
    tg.dying = None
    tg.phase = "turn"
    tg.turn_phase = "play"
    tg.prompt = None
    for n in ("t1", "t2"):
        if tg.players[n]["hp"] <= 0:
            tg.players[n]["hp"] = 4
            tg.players[n]["alive"] = True
    tg.players["t1"]["tech_level"] = 2
    ok, msg = _trick(tg, "t1", {"id": "soap", "name": "香皂", "type": "trick", "implemented": True, "instance_id": "so1"})
    assert ok, msg
    assert tg.prompt and tg.prompt.get("type") == "soap_heal"
    ok, msg = tg.apply_action("t1", {"action": "choose", "target": "t2"})
    assert ok, msg
    ok, msg = tg.apply_action("t1", {"action": "choose", "target": "t1"})
    assert ok, msg
    assert tg.phase == "turn"

    # guzheng choice
    tg.players["t1"]["hand"] = [
        {"id": "guzheng_plan", "name": "古筝", "type": "trick", "implemented": True, "instance_id": "gz1"},
        {"id": "peach", "name": "桃", "instance_id": "gzp"},
    ]
    tg.phase = "turn"
    tg.turn_phase = "play"
    ok, msg = tg.apply_action("t1", {"action": "play_card", "instance_id": "gz1", "discard_instance_id": "gzp"})
    assert ok, msg
    assert tg.prompt and tg.prompt.get("type") == "choice"
    ok, msg = tg.apply_action("t1", {"action": "choose", "choice": "draw2"})
    assert ok, msg

    # guzheng discard_target2 requires explicit target
    tg.players["t1"]["hand"] = [
        {"id": "guzheng_plan", "name": "古筝", "type": "trick", "implemented": True, "instance_id": "gz2"},
        {"id": "peach", "name": "桃", "instance_id": "gzp2"},
    ]
    tg.players["t2"]["hand"] = [
        {"id": "dodge_low", "name": "闪", "instance_id": "d1"},
        {"id": "dodge_low", "name": "闪", "instance_id": "d2"},
        {"id": "peach", "name": "桃", "instance_id": "d3"},
    ]
    tg.phase = "turn"
    tg.turn_phase = "play"
    ok, msg = tg.apply_action("t1", {"action": "play_card", "instance_id": "gz2", "discard_instance_id": "gzp2"})
    assert ok, msg
    ok, msg = tg.apply_action("t1", {"action": "choose", "choice": "discard_target2"})
    assert not ok, msg
    ok, msg = tg.apply_action("t1", {"action": "choose", "choice": "discard_target2", "target": "t2"})
    assert ok, msg
    assert len(tg.players["t2"]["hand"]) == 1

    ok, msg = _trick(tg, "t1", {"id": "dark_domain", "name": "黑域", "type": "trick", "implemented": True, "instance_id": "dd1"})
    assert ok and any(f["id"] == "dark_domain" for f in tg.fields), msg

    # 同名场地：不可再打出，可重铸
    ok, msg = _trick(tg, "t1", {"id": "dark_domain", "name": "黑域", "type": "trick", "implemented": True, "instance_id": "dd2"})
    assert not ok, msg
    tg.phase = "turn"
    tg.turn_phase = "play"
    tg.prompt = None
    tg._pending_trick = None
    for n in tg.player_order:
        tg.players[n]["hand"] = []
    tg.players["t1"]["hand"] = [
        {"id": "dark_domain", "name": "黑域", "type": "trick", "implemented": True, "instance_id": "dd-recast"}
    ]
    ok, msg = tg.apply_action("t1", {"action": "recast", "instance_id": "dd-recast"})
    assert ok, msg
    assert any(f["id"] == "dark_domain" for f in tg.fields)

    ok, msg = _trick(tg, "t1", {"id": "dark_forest_field", "name": "黑暗森林", "type": "trick", "implemented": True, "instance_id": "df1"})
    assert ok and any(f["id"] == "dark_forest_field" for f in tg.fields), msg

    hand_before_cs = len(tg.players["t1"]["hand"])
    ok, msg = _trick(tg, "t1", {"id": "cosmic_safety", "name": "宇宙安全声明", "type": "trick", "implemented": True, "instance_id": "cs1"})
    assert ok and tg.fields == [], msg
    assert len(tg.players["t1"]["hand"]) == hand_before_cs + 2, "宇宙安全声明清除成功后应摸2张"

    # interrupt: give t2 thought_stamp, play curtain from t1
    tg.phase = "turn"
    tg.turn_phase = "play"
    tg.players["t1"]["hand"] = [{"id": "curtain", "name": "帷幕", "type": "trick", "implemented": True, "instance_id": "cu2"}]
    tg.players["t2"]["hand"] = [{"id": "thought_stamp", "name": "思想钢印", "type": "trick", "implemented": True, "instance_id": "ts1"}]
    hand_before = len(tg.players["t1"]["hand"])
    ok, msg = tg.apply_action("t1", {"action": "play_card", "instance_id": "cu2"})
    assert ok and tg.prompt and tg.prompt.get("type") == "interrupt_trick", msg
    ok, msg = tg.apply_action("t2", {"action": "play_card", "instance_id": "ts1"})
    assert ok, msg
    # curtain nullified — t1 should not have drawn from it
    assert tg.phase == "turn"

    # 红岸可被思想钢印响应
    tg.players["t1"]["red_coast_used"] = False
    tg.players["t1"]["hand"] = [
        {"id": "red_coast", "name": "红岸计划", "type": "trick", "implemented": True, "instance_id": "rc1"}
    ]
    tg.players["t2"]["hand"] = [
        {"id": "thought_stamp", "name": "思想钢印", "type": "trick", "implemented": True, "instance_id": "ts-rc"}
    ]
    hand_before_rc = len(tg.players["t1"]["hand"])
    ok, msg = tg.apply_action("t1", {"action": "play_card", "instance_id": "rc1"})
    assert ok and tg.prompt and tg.prompt.get("type") == "interrupt_trick", msg
    ok, msg = tg.apply_action("t2", {"action": "play_card", "instance_id": "ts-rc"})
    assert ok, msg
    assert not tg.players["t1"].get("red_coast_used"), "钢印无效后不应记已用红岸"
    assert len(tg.players["t1"]["hand"]) == hand_before_rc - 1

    # 阶梯 / 球状闪电也可进打断窗
    tg.players["t1"]["hand"] = [
        {"id": "ladder_plan", "name": "阶梯计划", "type": "trick", "implemented": True, "instance_id": "lp1"}
    ]
    tg.players["t2"]["hand"] = [
        {"id": "thought_stamp", "name": "思想钢印", "type": "trick", "implemented": True, "instance_id": "ts-lp"}
    ]
    tg.players["t2"]["vision_exposed"] = False
    ok, msg = tg.apply_action("t1", {"action": "play_card", "instance_id": "lp1", "target": "t2"})
    assert ok and tg.prompt and tg.prompt.get("type") == "interrupt_trick", msg
    ok, msg = tg.apply_action("t2", {"action": "respond_pass"})
    assert ok, msg
    assert tg.players["t2"].get("vision_exposed")

    tg.players["t1"]["hand"] = [
        {
            "id": "nano_center",
            "name": "纳米工程中心",
            "type": "equipment",
            "slot": "temp_ascend",
            "implemented": True,
            "instance_id": "nano1",
        }
    ]
    tg.players["t2"]["hand"] = [
        {"id": "thought_stamp", "name": "思想钢印", "type": "trick", "implemented": True, "instance_id": "ts-nano"}
    ]
    ok, msg = tg.apply_action("t1", {"action": "play_card", "instance_id": "nano1"})
    assert ok and tg.prompt and tg.prompt.get("type") == "interrupt_trick", msg
    ok, msg = tg.apply_action("t2", {"action": "play_card", "instance_id": "ts-nano"})
    assert ok, msg
    assert not any(s["id"] == "nano_center" for s in tg.players["t1"]["statuses"])

    # 四维：拆装备 → 暴露 → 面壁弃 2；无装备 → 不暴露 → 面壁弃 1
    tg.phase = "turn"
    tg.turn_phase = "play"
    tg.prompt = None
    tg._pending_trick = None
    for n in tg.player_order:
        tg.players[n]["hand"] = []
    tg.players["t2"]["equipment"] = {
        "ship": {"id": "blue_space", "name": "蓝色空间", "type": "equipment", "slot": "ship", "implemented": True}
    }
    tg.players["t2"]["vision_exposed"] = False
    tg.players["t2"]["hand"] = [
        {"id": "peach", "name": "桃", "instance_id": "fd-p1"},
        {"id": "peach", "name": "桃", "instance_id": "fd-p2"},
    ]
    tg.players["t1"]["hand"] = [
        {"id": "four_dimension", "name": "四维空间", "type": "trick", "implemented": True, "instance_id": "fd1"}
    ]
    ok, msg = tg.apply_action("t1", {"action": "play_card", "instance_id": "fd1", "target": "t2"})
    assert ok, msg
    assert not tg.players["t2"]["equipment"].get("ship")
    assert tg.players["t2"].get("vision_exposed")
    tg.players["t1"]["hand"] = [
        {"id": "wallfacer_plan", "name": "面壁", "type": "trick", "implemented": True, "instance_id": "wf-fd"}
    ]
    ok, msg = tg.apply_action("t1", {"action": "play_card", "instance_id": "wf-fd", "target": "t2"})
    assert ok, msg
    assert len(tg.players["t2"]["hand"]) == 0

    tg.players["t2"]["equipment"] = {}
    tg.players["t2"]["vision_exposed"] = False
    tg.players["t2"]["hand"] = [
        {"id": "peach", "name": "桃", "instance_id": "fd-q1"},
        {"id": "peach", "name": "桃", "instance_id": "fd-q2"},
    ]
    tg.players["t1"]["hand"] = [
        {"id": "four_dimension", "name": "四维空间", "type": "trick", "implemented": True, "instance_id": "fd2"}
    ]
    ok, msg = tg.apply_action("t1", {"action": "play_card", "instance_id": "fd2", "target": "t2"})
    assert ok, msg
    assert not tg.players["t2"].get("vision_exposed")
    tg.players["t1"]["hand"] = [
        {"id": "wallfacer_plan", "name": "面壁", "type": "trick", "implemented": True, "instance_id": "wf-fd2"}
    ]
    ok, msg = tg.apply_action("t1", {"action": "play_card", "instance_id": "wf-fd2", "target": "t2"})
    assert ok, msg
    assert len(tg.players["t2"]["hand"]) == 1

    ok, msg = _trick(
        tg,
        "t1",
        {"id": "reckoning", "name": "清算", "type": "trick", "implemented": True, "realm_id": "reckoning", "instance_id": "rk1"},
        target="t2",
    )
    assert ok, msg
    assert tg.players["t1"]["hp"] == 1 and tg.players["t2"]["hp"] == 1

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
    assert private["you"]["role"]["role_id"] in KNOWN_ROLES

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
