"""Trick card effects. Handlers mutate GameSession via duck-typing."""

from __future__ import annotations

import math
import random
from typing import Any, Callable

from game.stats import final_basic_damage, final_true_damage


STATUS_TECH_LOCK = "tech_lock"
STATUS_CRADLE = "cradle"
STATUS_HIBERNATION = "hibernation"
STATUS_FLIPPED = "flipped"
STATUS_DEADLINE_LOCK = "deadline_lock"
STATUS_PLAN_PART = "plan_part"
STATUS_BLACK_HOLE = "black_hole"
STATUS_MICRO_UNIVERSE = "micro_universe"
STATUS_DEATH_IMMORTAL = "death_immortal"

FIELD_IDS = {
    "dark_domain",
    "dark_forest_field",
    "sophon_blind",
    "crisis_field",
    "trisolaris_field",
}

STATUS_TRICK_IDS = frozenset(
    {
        STATUS_PLAN_PART,
        STATUS_BLACK_HOLE,
        STATUS_MICRO_UNIVERSE,
        STATUS_DEATH_IMMORTAL,
        STATUS_CRADLE,
    }
)


def _alive_others(session: Any, username: str) -> list[str]:
    return [n for n in session.player_order if n != username and session.players[n]["alive"]]


def _require_alive_target(
    session: Any, username: str, target: str | None, *, allow_self: bool = False
) -> tuple[bool, str]:
    if not target or target not in session.players:
        return False, "需要指定目标"
    if not allow_self and target == username:
        return False, "不能以自己为目标"
    if not session.players[target]["alive"]:
        return False, "目标已淘汰"
    if session.players[target].get("untargetable") or session._has_status(target, STATUS_HIBERNATION):
        return False, "目标不可选中"
    return True, ""


def discard_from_target(session: Any, target: str, n: int) -> int:
    hand = session.players[target]["hand"]
    taken = 0
    for _ in range(n):
        if not hand:
            break
        card = hand.pop(random.randrange(len(hand)))
        session.discard.append(card)
        if hasattr(session, "_emit_discard"):
            session._emit_discard(target, card)
        session._log(f"{target} 被弃置 {card.get('name')}")
        taken += 1
    return taken


def field_bonus_damage(session: Any) -> int:
    return 2 if any(f.get("id") == "dark_forest_field" for f in session.fields) else 0


def field_bonus_reduction(session: Any) -> int:
    return 2 if any(f.get("id") == "dark_domain" for f in session.fields) else 0


def has_field(session: Any, field_id: str) -> bool:
    return any(f.get("id") == field_id for f in session.fields)


def add_field(session: Any, field_id: str, name: str, source: str) -> None:
    session.fields = [f for f in session.fields if f.get("id") != field_id]
    session.fields.append({"id": field_id, "name": name, "source": source})
    session._log(f"场地生效：{name}（来自 {source}）")


def clear_fields(session: Any) -> int:
    n = len(session.fields)
    session.fields = []
    session.field_multiplier = 1
    session.trisolaris_era = None
    return n


def clear_negative_statuses(session: Any, username: str) -> int:
    p = session.players[username]
    before = len(p["statuses"])
    p["statuses"] = [s for s in p["statuses"] if s.get("kind") != "negative"]
    return before - len(p["statuses"])


def clear_control_effects(session: Any, username: str) -> int:
    """Clear negative/control statuses (tech lock, flip, sealed, etc.)."""
    cleared = clear_negative_statuses(session, username)
    p = session.players[username]
    for sid in (STATUS_TECH_LOCK, STATUS_FLIPPED, STATUS_DEADLINE_LOCK):
        if session._remove_status(username, sid):
            cleared += 1
    if p.pop("cold_silence", None):
        cleared += 1
    if p.pop("ravine_damage_penalty", None):
        cleared += 1
        p["damage_reduction"] = max(0, int(p.get("damage_reduction", 0)) - 1)
    return cleared


def play_sophon(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    ok, err = _require_alive_target(session, username, target, allow_self=True)
    if not ok:
        return False, err
    assert target
    session.discard.append(card)
    session._apply_status(target, STATUS_TECH_LOCK, "科技锁定", "negative")
    session.players[target]["tech_lock_clear_at_turn_end"] = True
    session._log(f"{username} 对 {target} 使用智子：下回合科技无法变化")
    return True, f"{target} 科技已锁定"


def play_curtain(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    cleared = clear_negative_statuses(session, username)
    drawn = session.draw_sys.draw_n(session.players[username]["tech_level"], 1)
    if hasattr(session, "_give_drawn"):
        session._give_drawn(username, drawn)
    else:
        session.players[username]["hand"].extend(drawn)
    session._log(f"{username} 使用帷幕：清除 {cleared} 个负面，摸 {len(drawn)} 张")
    return True, f"清除负面并摸 {len(drawn)} 张"


def play_wallfacer(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    ok, err = _require_alive_target(session, username, target)
    if not ok:
        return False, err
    assert target
    n = 2 if session.players[target].get("vision_exposed") else 1
    session.discard.append(card)
    taken = discard_from_target(session, target, n)
    exposed = "已" if session.players[target].get("vision_exposed") else "未"
    session._log(f"{username} 对 {target} 使用面壁计划：弃 {taken} 张（目标{exposed}暴露视野）")
    return True, f"{target} 弃置 {taken} 张"


def play_red_coast(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    p = session.players[username]
    if p.get("red_coast_used"):
        return False, "红岸计划每回合限一次"
    drawn = session.draw_sys.draw_n(p["tech_level"], 2)
    if hasattr(session, "_give_drawn"):
        session._give_drawn(username, drawn)
    else:
        p["hand"].extend(drawn)
    p["red_coast_used"] = True
    session.discard.append(card)
    session._log(f"{username} 使用红岸计划，摸 {len(drawn)} 张")
    return True, f"摸了 {len(drawn)} 张"


def play_broadcast(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    src = session.players[username]
    bits: list[str] = []
    for name in _alive_others(session, username):
        t = session.players[name]
        if not t.get("vision_exposed"):
            dmg = final_basic_damage(
                2,
                int(src.get("damage_bonus", 0)) + field_bonus_damage(session),
                int(t.get("damage_reduction", 0)) + field_bonus_reduction(session),
            )
            bits.append(session._deal_damage(username, name, dmg, from_trick=True))
    for name in _alive_others(session, username):
        t = session.players[name]
        t["vision_exposed"] = True
        t["vision_clear_at_turn_end"] = True
    session._log(f"{username} 使用广播：" + ("；".join(bits) if bits else "无未暴露目标") + "；暴露其余人视野")
    return True, "广播已结算"


def play_toxic_water(session: Any, username: str, card: dict[str, Any], _target: str | None, action: dict) -> tuple[bool, str]:
    others = _alive_others(session, username)
    if not others:
        return False, "没有其他存活角色"
    session.discard.append(card)
    if action.get("skip_response"):
        src = session.players[username]
        bits = []
        for name in others:
            t = session.players[name]
            dmg = final_basic_damage(
                2,
                int(src.get("damage_bonus", 0)) + field_bonus_damage(session),
                int(t.get("damage_reduction", 0)) + field_bonus_reduction(session),
            )
            bits.append(session._deal_damage(username, name, dmg, from_trick=True))
        session._log(f"{username} 使用剧毒之水：" + "；".join(bits))
        return True, "剧毒之水已结算"
    session.prompt = {
        "type": "respond_toxic",
        "from": username,
        "to": others[0],
        "queue": others[1:],
        "card_name": card.get("name"),
        "base": 2,
        "resolved": [],
        "nullified_targets": [],
    }
    session.phase = "prompt"
    session._log(f"{username} 使用剧毒之水，等待 {others[0]} 响应")
    session._start_turn_timer()
    return True, f"等待 {others[0]} 响应剧毒之水"


def play_four_dimension(session: Any, username: str, card: dict[str, Any], target: str | None, action: dict) -> tuple[bool, str]:
    targets = action.get("targets") or ([] if not target else [target])
    if isinstance(targets, str):
        targets = [targets]
    targets = [t for t in targets if t in session.players and session.players[t]["alive"]]
    if not targets:
        # default: all others
        targets = _alive_others(session, username)
    if not targets:
        return False, "没有目标"
    session.discard.append(card)
    removed = 0
    exposed: list[str] = []
    for tname in targets:
        if removed >= 4:
            break
        t = session.players[tname]
        lost = False
        for slot in list(t.get("equipment") or {}):
            if removed >= 4:
                break
            if t["equipment"].get(slot):
                session._unequip_slot(tname, slot, to_discard=True)
                removed += 1
                lost = True
        if lost:
            t["vision_exposed"] = True
            t["vision_clear_at_turn_end"] = True
            exposed.append(tname)
    session._log(f"{username} 使用四维空间：弃装备 {removed} 件，暴露 {','.join(exposed) or '无'}")
    return True, f"弃装备 {removed} 件"


def play_deadline(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    ok, err = _require_alive_target(session, username, target)
    if not ok:
        return False, err
    assert target
    session.discard.append(card)
    t = session.players[target]
    before = t["tech_level"]
    t["tech_level"] = max(1, before - 1)
    session._apply_status(target, STATUS_TECH_LOCK, "死线锁定", "negative")
    t["tech_lock_clear_at_turn_end"] = True
    session._log(f"{username} 对 {target} 使用死线：科技 {before}→{t['tech_level']} 并锁定")
    return True, f"{target} 科技降至 {t['tech_level']}"


def play_zeroing(session: Any, username: str, card: dict[str, Any], _target: str | None, action: dict) -> tuple[bool, str]:
    others = _alive_others(session, username)
    if not others:
        return False, "没有其他存活角色"
    session.discard.append(card)
    if action.get("auto_resolve"):
        # smoke/auto: each takes ceil(tech/2) damage
        src = session.players[username]
        bits = []
        for name in others:
            t = session.players[name]
            half = math.ceil(int(t["tech_level"]) / 2)
            dmg = final_basic_damage(
                half,
                int(src.get("damage_bonus", 0)) + field_bonus_damage(session),
                int(t.get("damage_reduction", 0)) + field_bonus_reduction(session),
            )
            bits.append(session._deal_damage(username, name, dmg, from_trick=True))
        session._log(f"{username} 使用归零（自动）：" + "；".join(bits))
        return True, "归零已结算"
    session.prompt = {
        "type": "choice",
        "to": others[0],
        "from": username,
        "card_id": "zeroing",
        "queue": others[1:],
        "options": [
            {"id": "half_dmg", "label": "受到科技/2点伤害（向上取整）"},
            {"id": "tech_drop", "label": "科技-1并弃1张牌"},
        ],
    }
    session.phase = "prompt"
    session._log(f"{username} 使用归零：等待 {others[0]} 选择")
    session._start_turn_timer()
    return True, "归零开始"


def play_cradle(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    if session._has_status(username, STATUS_CRADLE):
        return False, "已有摇篮，可将本牌重铸"
    session.discard.append(card)
    session._apply_status(username, STATUS_CRADLE, "摇篮", "positive")
    session._log(f"{username} 使用摇篮：获得反伤（≤3）")
    return True, "获得摇篮"


def play_hibernation(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    p = session.players[username]
    p["max_hp"] = max(1, int(p.get("max_hp", 1)) - 2)
    p["hp"] = min(p["hp"], p["max_hp"])
    session._apply_status(username, STATUS_HIBERNATION, "冬眠", "positive")
    p["hibernation_clear_at_turn_start"] = True
    session._log(f"{username} 使用冬眠：上限-2（{p['max_hp']}），不可被他人选中至下回合开始")
    return True, "进入冬眠"


def play_deterrence(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    session.players[username]["deterrence_extra_target"] = True
    session._log(f"{username} 使用威慑：下一张基本牌指定目标+1")
    return True, "威慑生效"


def play_swordholder(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    session.players[username]["swordholder_ready"] = True
    session._log(f"{username} 使用执剑：下一张伤害牌将按最终伤害回血")
    return True, "执剑生效"


def play_dual_vector(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    ok, err = _require_alive_target(session, username, target)
    if not ok:
        return False, err
    assert target
    session.discard.append(card)
    t = session.players[target]
    src = session.players[username]
    if t.get("vision_exposed"):
        dmg = max(0, int(t["hp"]))
    else:
        dmg = final_true_damage(3, int(src.get("damage_bonus", 0)) + field_bonus_damage(session))
    t["hp"] -= dmg
    if dmg > 0 and hasattr(session, "_emit"):
        session._emit("damage", source=username, target=target, value=dmg, reason="true")
    msg = f"{target} 受到 {dmg} 点真实伤害（HP {t['hp']}）"
    if t["hp"] <= 0:
        msg += "，" + session._begin_dying(target)
    if src.get("swordholder_ready"):
        session._heal(username, dmg)
        src["swordholder_ready"] = False
        session._log(f"{username} 执剑：回复 {dmg} 点")
    session._log(f"{username} 对 {target} 使用二向箔：{msg}")
    return True, msg


def play_soap(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    if session.phase == "dying" or session.dying:
        return False, "香皂不可在濒死时使用"
    x = int(session.players[username]["tech_level"])
    if x <= 0:
        return False, "科技等级无效"
    session.discard.append(card)
    alive = [n for n in session.player_order if session.players[n]["alive"]]
    session.prompt = {
        "type": "soap_heal",
        "to": username,
        "from": username,
        "remaining": x,
        "targets": alive,
    }
    session.phase = "prompt"
    session._log(f"{username} 使用香皂：需选择 {x} 次+1血目标")
    session._start_turn_timer()
    return True, f"香皂：剩余 {x} 次"


def play_guzheng_start(session: Any, username: str, card: dict[str, Any], _target: str | None, action: dict) -> tuple[bool, str]:
    discard_id = str(action.get("discard_instance_id") or action.get("extra_instance_id") or "").strip()
    hand = session.players[username]["hand"]
    if not discard_id:
        return False, "古筝计划需要再弃一张手牌"
    idx = next((i for i, c in enumerate(hand) if c["instance_id"] == discard_id), None)
    if idx is None:
        return False, "弃牌不在手牌中"
    dumped = hand.pop(idx)
    session.discard.append(dumped)
    session.discard.append(card)
    session.prompt = {
        "type": "choice",
        "to": username,
        "from": username,
        "card_id": "guzheng_plan",
        "options": [
            {"id": "draw2", "label": "摸两张牌"},
            {"id": "discard_target2", "label": "弃置一名角色两张手牌", "needs_target": True},
            {"id": "heal2", "label": "回复2点"},
        ],
    }
    session.phase = "prompt"
    session._log(f"{username} 使用古筝计划（已弃 {dumped.get('name')}），三选一")
    session._start_turn_timer()
    return True, "请选择古筝效果"


def play_star_ring_city(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    alive = [n for n in session.player_order if session.players[n]["alive"]]
    session.prompt = {
        "type": "choice",
        "to": alive[0],
        "from": username,
        "card_id": "star_ring_city",
        "queue": alive[1:],
        "options": [
            {"id": "give2", "label": "给予使用者两张牌"},
            {"id": "dmg_heal", "label": "受1点基础伤害，令使用者等量回血"},
        ],
    }
    session.phase = "prompt"
    session._log(f"{username} 使用星环城：全员二选一")
    session._start_turn_timer()
    return True, "星环城开始"


def play_killer_52(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    ok, err = _require_alive_target(session, username, target)
    if not ok:
        return False, err
    assert target
    session.discard.append(card)
    helpers = [n for n in _alive_others(session, username) if n != target][:3]
    pending_kills: list[dict[str, Any]] = []
    for helper in helpers:
        drawn = session.draw_sys.draw_one(session.players[helper]["tech_level"])
        if drawn.get("subtype") == "kill":
            pending_kills.append({"from": helper, "card": drawn, "to": target})
            session._log(f"{helper} 摸到杀，将对 {target} 立即结算")
        else:
            session.players[helper]["hand"].append(drawn)
            if hasattr(session, "_emit"):
                session._emit(
                    "draw",
                    source=helper,
                    count=1,
                    cards=[session._card_pub(drawn)] if hasattr(session, "_card_pub") else None,
                    instance_ids=[drawn.get("instance_id")],
                )
            session._log(f"{helper} 摸到 {drawn.get('name')}，加入手牌")
    self_draw = session.draw_sys.draw_one(session.players[username]["tech_level"])
    if hasattr(session, "_give_drawn"):
        session._give_drawn(username, self_draw)
    else:
        session.players[username]["hand"].append(self_draw)
    session._log(f"{username} Killer.5.2：自己摸到 {self_draw.get('name')}")
    if pending_kills:
        session.killer_queue = pending_kills
        session._start_next_killer_kill()
        return True, "Killer.5.2：杀手协议启动"
    return True, "Killer.5.2 已结算"


def play_great_ravine(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    ok, err = _require_alive_target(session, username, target)
    if not ok:
        return False, err
    assert target
    session.discard.append(card)
    session._apply_status(target, STATUS_FLIPPED, "翻面", "negative")
    session.players[target]["damage_reduction"] = int(session.players[target].get("damage_reduction", 0)) + 1
    session.players[target]["ravine_damage_penalty"] = True
    session._log(f"{username} 对 {target} 使用大低谷：翻面且受到伤害-1")
    return True, f"{target} 翻面"


def play_dx3906(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    ok, err = _require_alive_target(session, username, target)
    if not ok:
        return False, err
    assert target
    thand = session.players[target]["hand"]
    if not thand:
        return False, "目标没有手牌"
    session.discard.append(card)
    taken = thand.pop(random.randrange(len(thand)))
    me = session.players[username]
    if hasattr(session, "_give_drawn"):
        session._give_drawn(username, taken)
    else:
        me["hand"].append(taken)
    bits = [f"获得 {taken.get('name')}"]
    if session._is_basic_card(taken):
        drawn = session.draw_sys.draw_one(me["tech_level"])
        if hasattr(session, "_give_drawn"):
            session._give_drawn(username, drawn)
        else:
            me["hand"].append(drawn)
        bits.append(f"基本牌：再摸 {drawn.get('name')}")
    elif taken.get("type") == "equipment" or taken.get("slot") in {"ship", "armor", "temp_ascend"}:
        dumped = list(me["hand"])
        me["hand"] = []
        session.discard.extend(dumped)
        for c in dumped:
            if hasattr(session, "_emit_discard"):
                session._emit_discard(username, c)
        bits.append(f"装备：弃光手牌（{len(dumped)}）")
    else:
        # trick / field
        n = discard_from_target(session, target, 1)
        bits.append(f"锦囊：再弃对方 {n} 张")
    session._log(f"{username} 对 {target} 使用 DX3906：" + "；".join(bits))
    return True, "；".join(bits)


def play_field_card(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    cid = card.get("id")
    names = {
        "dark_domain": "黑域",
        "dark_forest_field": "黑暗森林",
        "sophon_blind": "智子盲区",
        "crisis_field": "危机",
        "trisolaris_field": "三体",
    }
    if has_field(session, str(cid)):
        return False, "场上已有同名场地，可将本牌重铸"
    session.discard.append(card)
    add_field(session, str(cid), names.get(str(cid), str(cid)), username)
    if cid == "trisolaris_field":
        session.trisolaris_era = "stable"
        session._log("三体纪元：恒纪元（初始）")
    return True, f"场地 {names.get(str(cid), cid)} 已布置"


def play_cosmic_safety(session: Any, username: str, card: dict[str, Any], target: str | None, action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    names = action.get("targets") or ([] if not target else [target])
    if isinstance(names, str):
        names = [names]
    names = [n for n in names if isinstance(n, str) and n.strip()]
    if not names:
        names = [n for n in session.player_order if session.players[n]["alive"]]
    cleared = 0
    n_fields = clear_fields(session)
    cleared += n_fields
    for name in names:
        if name not in session.players or not session.players[name]["alive"]:
            continue
        cleared += clear_control_effects(session, name)
    if cleared <= 0:
        session._log(f"{username} 使用宇宙安全声明：无效果可清")
        return True, "无效果可清"
    me = session.players[username]
    drawn = session.draw_sys.draw_n(int(me.get("tech_level") or 1), 2)
    me["hand"].extend(drawn)
    session._log(f"{username} 使用宇宙安全声明：清除 {cleared} 项，摸 {len(drawn)} 张")
    return True, f"清除成功，摸 {len(drawn)} 张"


def play_curse(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    if session.fields:
        session.field_multiplier = int(session.field_multiplier or 1) * 2
        session._log(f"{username} 使用咒语：场地效果翻倍（x{session.field_multiplier}）")
        return True, f"场地翻倍 x{session.field_multiplier}"
    pick = random.choice(list(FIELD_IDS))
    names = {
        "dark_domain": "黑域",
        "dark_forest_field": "黑暗森林",
        "crisis_field": "危机",
        "sophon_blind": "智子盲区",
        "trisolaris_field": "三体",
    }
    add_field(session, pick, names[pick], username)
    if pick == "trisolaris_field":
        session.trisolaris_era = "stable"
    return True, f"随机场地：{names[pick]}"


def play_thought_stamp(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    if session.phase != "prompt" or not session.prompt:
        return False, "思想钢印只能在响应时使用"
    ptype = session.prompt.get("type")
    if ptype not in {"interrupt_trick", "respond_toxic"}:
        return False, "当前无法使用思想钢印"
    session.discard.append(card)
    session.prompt["nullified"] = True
    session.prompt["nullified_by"] = username
    if ptype == "respond_toxic":
        session.prompt.setdefault("nullified_targets", []).append(session.prompt.get("to"))
    session._log(f"{username} 打出思想钢印：锦囊无效")
    session._resolve_interrupt_or_toxic()
    return True, "锦囊无效"


def play_return_motion(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    if session.phase != "prompt" or not session.prompt or session.prompt.get("type") != "interrupt_trick":
        return False, "回归运动只能在非基本牌结算前使用"
    session.discard.append(card)
    session.prompt["nullified"] = True
    session.prompt["nullified_by"] = username
    session._log(f"{username} 打出回归运动：非基本牌无效")
    session._resolve_interrupt_or_toxic()
    return True, "牌无效"


def _apply_status_trick(
    session: Any,
    username: str,
    card: dict[str, Any],
    status_id: str,
    status_name: str,
    on_apply: Callable[[Any, str], None] | None = None,
) -> tuple[bool, str]:
    session.discard.append(card)
    if session._has_status(username, status_id):
        session._log(f"{username} 使用{status_name}：已有该状态，不可叠加")
        return True, f"已有{status_name}"
    session._apply_status(username, status_id, status_name, "positive")
    if on_apply:
        on_apply(session, username)
    session._log(f"{username} 获得正面状态：{status_name}")
    return True, f"获得{status_name}"


def play_plan_part(session: Any, username: str, card: dict[str, Any], _t: str | None, _a: dict) -> tuple[bool, str]:
    def on_apply(s: Any, u: str) -> None:
        s.players[u]["plan_part_charges"] = 2

    return _apply_status_trick(session, username, card, STATUS_PLAN_PART, "计划的一部分", on_apply)


def play_black_hole(session: Any, username: str, card: dict[str, Any], _t: str | None, _a: dict) -> tuple[bool, str]:
    def on_apply(s: Any, u: str) -> None:
        s.players[u]["black_hole_enemy_basics"] = []

    return _apply_status_trick(session, username, card, STATUS_BLACK_HOLE, "黑洞", on_apply)


def play_micro_universe(session: Any, username: str, card: dict[str, Any], _t: str | None, _a: dict) -> tuple[bool, str]:
    def on_apply(s: Any, u: str) -> None:
        p = s.players[u]
        p["shield"] = int(p.get("shield", 0)) + 5
        p["micro_universe_shield"] = 5

    return _apply_status_trick(session, username, card, STATUS_MICRO_UNIVERSE, "小宇宙", on_apply)


def play_death_immortal(session: Any, username: str, card: dict[str, Any], _t: str | None, _a: dict) -> tuple[bool, str]:
    def on_apply(s: Any, u: str) -> None:
        s.players[u]["death_immortal"] = True

    return _apply_status_trick(session, username, card, STATUS_DEATH_IMMORTAL, "死神永生", on_apply)


def play_realm(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    rid = card.get("realm_id") or card.get("id")
    session.discard.append(card)
    if rid == "reckoning":
        ok, err = _require_alive_target(session, username, target)
        if not ok:
            others = _alive_others(session, username)
            if not others:
                return True, "清算无目标"
            target = others[0]
        assert target
        session.players[username]["hp"] = 1
        session.players[target]["hp"] = 1
        session._log(f"{username} 虚境·清算：与 {target} 生命均变为 1")
        return True, "清算"
    if rid == "illusion":
        for name in list(session.player_order):
            p = session.players[name]
            if not p["alive"]:
                continue
            n = len(p["hand"])
            session.discard.extend(p["hand"])
            p["hand"] = []
            if n:
                p["hp"] -= n
                session._log(f"{name} 虚境·幻梦：弃 {n} 张并受伤 {n}（HP {p['hp']}）")
                if p["hp"] <= 0:
                    session._begin_dying(name)
        return True, "幻梦"
    if rid == "cold_silence":
        for name in session.player_order:
            p = session.players[name]
            if not p["alive"]:
                continue
            p["tech_level"] = 1
            session._apply_status(name, STATUS_TECH_LOCK, "冷寂", "negative")
            p["tech_lock_clear_at_turn_end"] = False
            p["cold_silence"] = True
        session._log(f"{username} 虚境·冷寂：全员科技降至 1 并锁定")
        return True, "冷寂"
    if rid == "finale":
        ok, err = _require_alive_target(session, username, target, allow_self=True)
        if not ok:
            target = username
        assert target
        t = session.players[target]
        t["damage_bonus"] = int(t.get("damage_bonus", 0)) + 3
        drawn = session.draw_sys.draw_n(t["tech_level"], 2)
        t["hand"].extend(drawn)
        clear_negative_statuses(session, target)
        t["finale_death_pending"] = True
        t["finale_field_immune"] = True
        session._log(f"{username} 虚境·终末：{target} 强化，下回合结束后死亡")
        return True, "终末"
    drawn = session.draw_sys.draw_n(session.players[username]["tech_level"], 1)
    session.players[username]["hand"].extend(drawn)
    session._log(f"{username} 虚境·{card.get('name')}（占位）：摸 1 张")
    return True, f"虚境 {card.get('name')}"


HANDLERS: dict[str, Callable[..., tuple[bool, str]]] = {
    "sophon": play_sophon,
    "curtain": play_curtain,
    "wallfacer_plan": play_wallfacer,
    "red_coast": play_red_coast,
    "broadcast": play_broadcast,
    "toxic_water": play_toxic_water,
    "four_dimension": play_four_dimension,
    "deadline": play_deadline,
    "zeroing": play_zeroing,
    "cradle": play_cradle,
    "hibernation": play_hibernation,
    "deterrence": play_deterrence,
    "swordholder": play_swordholder,
    "dual_vector": play_dual_vector,
    "soap": play_soap,
    "guzheng_plan": play_guzheng_start,
    "star_ring_city": play_star_ring_city,
    "killer_52": play_killer_52,
    "great_ravine": play_great_ravine,
    "dx3906": play_dx3906,
    "dark_domain": play_field_card,
    "dark_forest_field": play_field_card,
    "sophon_blind": play_field_card,
    "crisis_field": play_field_card,
    "trisolaris_field": play_field_card,
    "cosmic_safety": play_cosmic_safety,
    "curse": play_curse,
    "thought_stamp": play_thought_stamp,
    "return_motion": play_return_motion,
    "realm_bucket": play_realm,
    "plan_part": play_plan_part,
    "black_hole": play_black_hole,
    "micro_universe": play_micro_universe,
    "death_immortal": play_death_immortal,
}

for _rid in ("finale", "reckoning", "illusion", "cold_silence", "myriad", "afterglow"):
    HANDLERS[_rid] = play_realm

TARGET_TRICKS = {
    "sophon",
    "wallfacer_plan",
    "deadline",
    "dual_vector",
    "great_ravine",
    "killer_52",
    "dx3906",
    "reckoning",
    "finale",
    "four_dimension",
}

SELF_OK = {
    "sophon",
    "curtain",
    "cradle",
    "hibernation",
    "deterrence",
    "swordholder",
    "red_coast",
    "plan_part",
    "black_hole",
    "micro_universe",
    "death_immortal",
    "broadcast",
    "toxic_water",
    "zeroing",
    "star_ring_city",
    "cosmic_safety",
    "curse",
    "soap",
    "four_dimension",
}


def legal_play(session: Any, username: str, card: dict[str, Any]) -> bool:
    cid = card.get("id")
    if cid not in HANDLERS and cid not in FIELD_IDS:
        return False
    if not session._card_implemented(card):
        return False
    if cid == "soap" and (session.phase == "dying" or session.dying):
        return False
    if cid in TARGET_TRICKS and cid not in SELF_OK:
        if cid == "sophon":
            return bool(session._alive_players())
        return bool(_alive_others(session, username))
    if cid == "guzheng_plan":
        return len(session.players[username]["hand"]) >= 2
    if cid in {"thought_stamp", "return_motion"}:
        return False
    if cid == "red_coast":
        return not bool(session.players[username].get("red_coast_used"))
    if cid in STATUS_TRICK_IDS:
        return not session._has_status(username, str(cid))
    if cid in FIELD_IDS:
        return not has_field(session, str(cid))
    return True
