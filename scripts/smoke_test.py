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
        p["countdown"] = None
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
    p["madonna_used"] = False
    p["flying_blade_used"] = False
    p["next_damage_true"] = False
    p["countdown_done"] = False
    if role_id == "wang_miao":
        p["countdown"] = 12
    else:
        p["countdown"] = None


KNOWN_ROLES = {
    "guan_yifan",
    "friss",
    "luo_ji",
    "ye_wenjie",
    "cheng_xin",
    "wang_miao",
}


def _clear_prompt_to_turn(session: GameSession) -> None:
    """Dismiss opening skill prompts (e.g. 仁心) so baseline tests can run."""
    while session.phase == "prompt" and session.prompt:
        ptype = session.prompt.get("type")
        who = str(session.prompt.get("to"))
        if ptype == "benevolence_heal":
            session.apply_action(who, {"action": "benevolence_pass"})
        elif ptype == "wander_draw":
            session.apply_action(who, {"action": "wander_pass"})
        elif ptype == "madonna_save":
            session.apply_action(who, {"action": "madonna_pass"})
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
    ok, msg = eg.apply_action(cur, {"action": "discard_done"})
    assert ok and eg.turn_phase == "discard", msg
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
    for i in range(4):
        ok, msg = eg.apply_action(cur, {"action": "discard_card", "instance_id": f"extra-{i}"})
        assert ok, msg
    ok, msg = eg.apply_action(cur, {"action": "discard_done"})
    assert ok, msg

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
    ok, msg = gy.apply_action("guan", {"action": "discard_done"})
    assert ok, msg
    assert gy.phase == "prompt" and gy.prompt and gy.prompt.get("type") == "wander_draw"
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

    # 叶文洁领袖：杀无法响应；红岸摸牌
    yw = GameSession.create("YE", ["ye", "victim"])
    _as_role(yw, "ye", "ye_wenjie")
    _blank_skills(yw, "victim")
    yw.turn_index = yw.player_order.index("ye")
    yw.phase = "turn"
    yw.turn_phase = "play"
    kill_ye = {
        "id": "kill_low",
        "name": "1阶杀",
        "type": "basic",
        "subtype": "kill",
        "tier": 1,
        "instance_id": "kill-ye",
    }
    _give(yw.players["ye"], kill_ye)
    _give(yw.players["victim"])  # 清空手牌，避免濒死强制桃
    yw.players["victim"]["hp"] = 1
    hand0 = len(yw.players["ye"]["hand"])
    ok, msg = yw.apply_action("ye", {"action": "play_card", "instance_id": "kill-ye", "target": "victim"})
    assert ok, msg
    # 领袖直接结算，可能进入濒死/圣母询问；强制出局看红岸
    if yw.phase == "prompt" and yw.prompt and yw.prompt.get("type") == "madonna_save":
        yw.apply_action(str(yw.prompt["to"]), {"action": "madonna_pass"})
    if yw.phase == "dying":
        yw.apply_action("victim", {"action": "dying_resolve"})
    assert not yw.players["victim"]["alive"]
    assert len(yw.players["ye"]["hand"]) >= hand0 + 1

    # 程心仁心 + 圣母
    cx = GameSession.create("CHENG", ["cheng", "hurt"])
    _as_role(cx, "cheng", "cheng_xin")
    _blank_skills(cx, "hurt")
    cx.turn_index = cx.player_order.index("cheng")
    cx.phase = "turn"
    cx.turn_phase = "play"
    cx.players["hurt"]["hp"] = 1
    cx._open_benevolence_prompt("cheng")
    ok, msg = cx.apply_action("cheng", {"action": "benevolence_heal", "target": "hurt"})
    assert ok, msg
    assert cx.players["hurt"]["hp"] == 3
    cx.players["hurt"]["hp"] = 0
    cx._begin_dying("hurt", source="cheng")
    assert cx.prompt and cx.prompt.get("type") == "madonna_save"
    ok, msg = cx.apply_action("cheng", {"action": "madonna_accept"})
    assert ok, msg
    assert cx.players["hurt"]["hp"] == cx.players["hurt"]["max_hp"]
    assert cx.players["hurt"]["tech_level"] == 1
    assert cx.players["cheng"]["madonna_used"] is True

    # 汪淼倒计时 + 飞刃
    wm = GameSession.create("WANG", ["wang", "foe"])
    _as_role(wm, "wang", "wang_miao")
    _blank_skills(wm, "foe")
    assert wm.players["wang"]["countdown"] == 12
    wm.turn_index = wm.player_order.index("wang")
    wm.phase = "turn"
    wm.turn_phase = "play"
    wm.players["wang"]["countdown"] = 1
    wm._tick_countdowns(1)
    assert wm.players["wang"]["countdown_done"] is True
    assert wm.players["wang"]["tech_level"] == 2
    ok, msg = wm.apply_action("wang", {"action": "flying_blade"})
    assert ok, msg
    assert wm.players["wang"]["next_damage_true"] is True

    # 新装备：小宇宙护盾 / 星环号不可被杀 / 太阳系观测
    eqn = GameSession.create("EQUIP_NEW", ["eq", "bot"])
    _blank_skills(eqn, "eq", "bot")
    eqn.turn_index = eqn.player_order.index("eq")
    eqn.phase = "turn"
    eqn.turn_phase = "play"
    micro = {
        "id": "micro_universe",
        "name": "小宇宙",
        "type": "equipment",
        "slot": "armor",
        "implemented": True,
        "instance_id": "micro-1",
    }
    _give(eqn.players["eq"], micro)
    ok, msg = eqn.apply_action("eq", {"action": "play_card", "instance_id": "micro-1"})
    assert ok, msg
    assert eqn.players["eq"]["shield"] == 5
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
    ok, msg = bl.apply_action("seer", {"action": "discard_done"})
    assert ok, msg
    assert bl.current_player() == "target"
    tech_before = bl.players["target"]["tech_level"]
    _give(bl.players["target"])
    bl.phase = "turn"
    bl.turn_phase = "play"
    ok, msg = bl.apply_action("target", {"action": "end_play"})
    assert ok, msg
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
    assert len(tg.players["t1"]["hand"]) >= 2

    tg.players["t2"]["hand"] = [{"id": "peach", "name": "桃", "instance_id": "px"}]
    tg.players["t2"]["vision_exposed"] = True
    ok, msg = _trick(tg, "t1", {"id": "wallfacer_plan", "name": "面壁", "type": "trick", "implemented": True, "instance_id": "wf1"}, target="t2")
    assert ok, msg
    assert len(tg.players["t2"]["hand"]) == 0

    ok, msg = _trick(tg, "t1", {"id": "broadcast", "name": "广播", "type": "trick", "implemented": True, "instance_id": "br1"}, target="t2")
    # t2 may already be exposed from above — reset
    tg.players["t2"]["vision_exposed"] = False
    ok, msg = _trick(tg, "t1", {"id": "broadcast", "name": "广播", "type": "trick", "implemented": True, "instance_id": "br2"}, target="t2")
    assert ok, msg
    assert tg.players["t2"]["vision_exposed"]

    # toxic with response window
    hp_before = tg.players["t2"]["hp"]
    ok, msg = _trick(tg, "t1", {"id": "toxic_water", "name": "剧毒", "type": "trick", "implemented": True, "instance_id": "tw1"}, target="t2")
    assert ok, msg
    assert tg.phase == "prompt" and tg.prompt and tg.prompt.get("type") == "respond_toxic"
    ok, msg = tg.apply_action("t2", {"action": "respond_pass"})
    assert ok, msg
    assert tg.players["t2"]["hp"] < hp_before

    ok, msg = _trick(tg, "t1", {"id": "cradle", "name": "摇篮", "type": "trick", "implemented": True, "instance_id": "cr1"})
    assert ok and any(s["id"] == "cradle" for s in tg.players["t1"]["statuses"]), msg

    ok, msg = _trick(tg, "t1", {"id": "hibernation", "name": "冬眠", "type": "trick", "implemented": True, "instance_id": "hi1"})
    assert ok and any(s["id"] == "hibernation" for s in tg.players["t1"]["statuses"]), msg

    ok, msg = _trick(tg, "t1", {"id": "deterrence", "name": "威慑", "type": "trick", "implemented": True, "instance_id": "de1"})
    assert ok and tg.players["t1"].get("deterrence_extra", 0) >= 1, msg

    ok, msg = _trick(tg, "t1", {"id": "swordholder", "name": "执剑", "type": "trick", "implemented": True, "instance_id": "sw1"})
    assert ok and tg.players["t1"].get("swordholder_ready"), msg

    tg.players["t2"]["vision_exposed"] = False
    tg.players["t2"]["hp"] = 5
    ok, msg = _trick(tg, "t1", {"id": "dual_vector", "name": "二向箔", "type": "trick", "implemented": True, "instance_id": "dv1"}, target="t2")
    assert ok, msg
    assert tg.players["t2"]["hp"] <= 2  # 5-3 true, maybe heal from swordholder on t1

    ok, msg = _trick(tg, "t1", {"id": "soap", "name": "香皂", "type": "trick", "implemented": True, "instance_id": "so1"}, target="t2")
    assert ok, msg

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

    ok, msg = _trick(tg, "t1", {"id": "dark_domain", "name": "黑域", "type": "trick", "implemented": True, "instance_id": "dd1"})
    assert ok and any(f["id"] == "dark_domain" for f in tg.fields), msg

    ok, msg = _trick(tg, "t1", {"id": "dark_forest_field", "name": "黑暗森林", "type": "trick", "implemented": True, "instance_id": "df1"})
    assert ok and any(f["id"] == "dark_forest_field" for f in tg.fields), msg

    ok, msg = _trick(tg, "t1", {"id": "cosmic_safety", "name": "宇宙安全声明", "type": "trick", "implemented": True, "instance_id": "cs1"})
    assert ok and tg.fields == [], msg

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
