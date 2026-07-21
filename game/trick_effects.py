"""Trick card effects (Waves 1–5). Handlers mutate GameSession via duck-typing."""

from __future__ import annotations

import random
from typing import Any, Callable

from game.stats import final_basic_damage, final_true_damage

STATUS_TECH_LOCK = "tech_lock"
STATUS_CRADLE = "cradle"
STATUS_HIBERNATION = "hibernation"
STATUS_FLIPPED = "flipped"
STATUS_DEADLINE_LOCK = "deadline_lock"

FIELD_IDS = {
    "dark_domain",
    "dark_forest_field",
    "sophon_blind",
    "crisis_field",
    "trisolaris_field",
}


def _alive_others(session: Any, username: str) -> list[str]:
    return [n for n in session.player_order if n != username and session.players[n]["alive"]]


def _require_alive_target(session: Any, username: str, target: str | None, *, allow_self: bool = False) -> tuple[bool, str]:
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
    return n


def clear_negative_statuses(session: Any, username: str) -> int:
    p = session.players[username]
    before = len(p["statuses"])
    p["statuses"] = [s for s in p["statuses"] if s.get("kind") != "negative"]
    return before - len(p["statuses"])


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
    # Fixed choice: draw 2 (no choice UI needed)
    drawn = session.draw_sys.draw_n(session.players[username]["tech_level"], 2)
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
    session._log(f"{username} 对 {target} 使用面壁计划：弃 {taken} 张")
    return True, f"{target} 弃置 {taken} 张"


def play_broadcast(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    ok, err = _require_alive_target(session, username, target)
    if not ok:
        return False, err
    assert target
    t = session.players[target]
    if t.get("vision_exposed"):
        return False, "目标视野已暴露"
    session.discard.append(card)
    src = session.players[username]
    dmg = final_basic_damage(1, int(src.get("damage_bonus", 0)) + field_bonus_damage(session), int(t.get("damage_reduction", 0)) + field_bonus_reduction(session))
    msg = session._deal_damage(username, target, dmg)
    t["vision_exposed"] = True
    t["vision_clear_at_turn_end"] = True
    session._log(f"{username} 对 {target} 使用广播：{msg}，并暴露视野")
    return True, msg


def play_toxic_water(session: Any, username: str, card: dict[str, Any], target: str | None, action: dict) -> tuple[bool, str]:
    """Wave1 simplified: direct 2 base damage. Wave5 may open response via action flag."""
    ok, err = _require_alive_target(session, username, target)
    if not ok:
        return False, err
    assert target
    # Wave5: if allow_response and not skipped, open toxic response window
    if card.get("allow_response") and not action.get("skip_response"):
        session.discard.append(card)
        session.prompt = {
            "type": "respond_toxic",
            "from": username,
            "to": target,
            "card_name": card.get("name"),
            "base": 2,
        }
        session.phase = "prompt"
        session._log(f"{username} 对 {target} 使用剧毒之水，等待响应")
        session._start_turn_timer()
        return True, f"等待 {target} 响应剧毒之水"
    session.discard.append(card)
    src = session.players[username]
    t = session.players[target]
    dmg = final_basic_damage(2, int(src.get("damage_bonus", 0)) + field_bonus_damage(session), int(t.get("damage_reduction", 0)) + field_bonus_reduction(session))
    msg = session._deal_damage(username, target, dmg)
    session._log(f"{username} 对 {target} 使用剧毒之水：{msg}")
    return True, msg


def play_four_dimension(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    ok, err = _require_alive_target(session, username, target)
    if not ok:
        return False, err
    assert target
    session.discard.append(card)
    t = session.players[target]
    for slot in list(t.get("equipment") or {}):
        if t["equipment"].get(slot):
            session._unequip_slot(target, slot, to_discard=True)
    t["vision_exposed"] = True
    t["vision_clear_at_turn_end"] = True
    session._log(f"{username} 对 {target} 使用四维空间：弃装备并暴露视野")
    return True, f"{target} 装备已弃、视野暴露"


def play_deadline(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    ok, err = _require_alive_target(session, username, target)
    if not ok:
        return False, err
    assert target
    session.discard.append(card)
    t = session.players[target]
    before = t["tech_level"]
    # bypass tech lock for this forced drop
    t["tech_level"] = max(1, before - 1)
    session._apply_status(target, STATUS_TECH_LOCK, "死线锁定", "negative")
    t["tech_lock_clear_at_turn_end"] = True
    session._log(f"{username} 对 {target} 使用死线：科技 {before}→{t['tech_level']} 并锁定")
    return True, f"{target} 科技降至 {t['tech_level']}"


def play_zeroing(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    src = session.players[username]
    bits = []
    for name in _alive_others(session, username):
        t = session.players[name]
        # alternate: deal 1 final or drop tech — prefer damage if hp>1 else drop tech
        if t["hp"] > 1:
            dmg = final_basic_damage(1, int(src.get("damage_bonus", 0)) + field_bonus_damage(session), int(t.get("damage_reduction", 0)) + field_bonus_reduction(session))
            bits.append(session._deal_damage(username, name, dmg))
        else:
            before = t["tech_level"]
            if not session._has_status(name, STATUS_TECH_LOCK):
                t["tech_level"] = max(1, before - 1)
            bits.append(f"{name} 科技 {before}→{t['tech_level']}")
    session._log(f"{username} 使用归零：" + "；".join(bits))
    return True, "归零已结算"


def play_cradle(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    session._apply_status(username, STATUS_CRADLE, "摇篮", "positive")
    session._log(f"{username} 使用摇篮：获得反伤")
    return True, "获得摇篮"


def play_hibernation(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    session._apply_status(username, STATUS_HIBERNATION, "冬眠", "positive")
    session.players[username]["hibernation_clear_at_turn_start"] = True
    session._log(f"{username} 使用冬眠：不可选中至下回合开始")
    return True, "进入冬眠"


def play_deterrence(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    session.players[username]["deterrence_extra"] = int(session.players[username].get("deterrence_extra", 0)) + 1
    session._log(f"{username} 使用威慑：下一张基本牌额外目标+1")
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
    # true damage bypasses normal reduction except we still apply eco_bottle via _deal_damage path —
    # use direct hp for true damage simplicity
    t["hp"] -= dmg
    msg = f"{target} 受到 {dmg} 点真实伤害（HP {t['hp']}）"
    if t["hp"] <= 0:
        msg += "，" + session._begin_dying(target)
    if src.get("swordholder_ready"):
        session._heal(username, dmg)
        src["swordholder_ready"] = False
        session._log(f"{username} 执剑：回复 {dmg} 点")
    session._log(f"{username} 对 {target} 使用二向箔：{msg}")
    return True, msg


def play_soap(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    ok, err = _require_alive_target(session, username, target)
    if not ok:
        return False, err
    assert target
    session.discard.append(card)
    amount = int(session.players[username]["tech_level"])
    session._heal(username, amount)
    session._heal(target, amount)
    session._log(f"{username} 与 {target} 使用香皂：各回复 {amount} 点")
    return True, f"各回复 {amount}"


def play_guzheng_start(session: Any, username: str, card: dict[str, Any], _target: str | None, action: dict) -> tuple[bool, str]:
    discard_id = str(action.get("discard_instance_id") or action.get("extra_instance_id") or "").strip()
    hand = session.players[username]["hand"]
    # card already popped from hand; need another card to discard
    if not discard_id:
        # auto-discard random if only this was needed — require explicit for smoke
        if not hand:
            return False, "古筝计划需要再弃一张手牌"
        discard_id = hand[0]["instance_id"]
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
            {"id": "heal2", "label": "回复2点"},
            {"id": "tech1", "label": "科技+1"},
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
            {"id": "draw1", "label": "摸一张牌"},
            {"id": "discard1", "label": "弃一张牌"},
        ],
    }
    session.phase = "prompt"
    session._log(f"{username} 使用星环城：全员二选一")
    session._start_turn_timer()
    return True, "星环城开始"


def play_killer_52(session: Any, username: str, card: dict[str, Any], target: str | None, action: dict) -> tuple[bool, str]:
    targets = action.get("targets") or ([] if not target else [target])
    if isinstance(targets, str):
        targets = [targets]
    targets = [t for t in targets if t and t in session.players and session.players[t]["alive"] and t != username]
    if not targets:
        return False, "Killer.5.2 需要至少一个目标"
    session.discard.append(card)
    src = session.players[username]
    bits = []
    for tname in targets:
        t = session.players[tname]
        dmg = final_basic_damage(1, int(src.get("damage_bonus", 0)) + field_bonus_damage(session), int(t.get("damage_reduction", 0)) + field_bonus_reduction(session))
        bits.append(session._deal_damage(username, tname, dmg))
    drawn = session.draw_sys.draw_n(src["tech_level"], 1)
    src["hand"].extend(drawn)
    session._log(f"{username} 使用 Killer.5.2：" + "；".join(bits) + f"；摸到 {drawn[0].get('name')}")
    # 摸牌即用：若摸到基本杀则自动不强制；仅记录
    return True, "Killer.5.2 已结算"


def play_great_ravine(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    ok, err = _require_alive_target(session, username, target)
    if not ok:
        return False, err
    assert target
    session.discard.append(card)
    session._apply_status(target, STATUS_FLIPPED, "翻面", "negative")
    session.players[target]["damage_bonus"] = max(0, int(session.players[target].get("damage_bonus", 0)) - 1)
    session.players[target]["ravine_damage_penalty"] = True
    session._log(f"{username} 对 {target} 使用大低谷：翻面且伤害-1")
    return True, f"{target} 翻面"


def play_dx3906(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    # take top of "virtual" — draw one then if trick/equipment leave in hand; if basic auto no
    drawn = session.draw_sys.draw_one(session.players[username]["tech_level"])
    session.players[username]["hand"].append(drawn)
    session._log(f"{username} 使用 DX3906：获得 {drawn.get('name')}")
    return True, f"获得 {drawn.get('name')}"


def play_field_card(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    cid = card.get("id")
    names = {
        "dark_domain": "黑域",
        "dark_forest_field": "黑暗森林",
        "sophon_blind": "智子盲区",
        "crisis_field": "危机",
        "trisolaris_field": "三体",
    }
    session.discard.append(card)
    add_field(session, str(cid), names.get(str(cid), str(cid)), username)
    if cid == "trisolaris_field":
        era = random.choice(["stable", "chaos"])
        session.trisolaris_era = era
        session._log(f"三体纪元：{'恒纪元' if era == 'stable' else '乱纪元'}")
    return True, f"场地 {names.get(str(cid), cid)} 已布置"


def play_cosmic_safety(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    n = clear_fields(session)
    cleared = 0
    for name in session.player_order:
        if not session.players[name]["alive"]:
            continue
        cleared += clear_negative_statuses(session, name)
        for sid in (STATUS_TECH_LOCK, STATUS_FLIPPED, STATUS_HIBERNATION, STATUS_CRADLE):
            if session._remove_status(name, sid):
                cleared += 1
    session.trisolaris_era = None
    session._log(f"{username} 使用宇宙安全声明：清除 {n} 个场地、若干控制")
    return True, "场地与控制已清除"


def play_curse(session: Any, username: str, card: dict[str, Any], _target: str | None, _action: dict) -> tuple[bool, str]:
    session.discard.append(card)
    if session.fields:
        # double: stack a marker
        session.field_multiplier = int(session.field_multiplier or 1) * 2
        session._log(f"{username} 使用咒语：场地效果翻倍（x{session.field_multiplier}）")
        return True, f"场地翻倍 x{session.field_multiplier}"
    # random field
    pick = random.choice(["dark_domain", "dark_forest_field", "crisis_field", "sophon_blind"])
    names = {"dark_domain": "黑域", "dark_forest_field": "黑暗森林", "crisis_field": "危机", "sophon_blind": "智子盲区"}
    add_field(session, pick, names[pick], username)
    return True, f"随机场地：{names[pick]}"


def play_thought_stamp(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    """Used during interrupt_trick or respond_toxic to nullify."""
    if session.phase != "prompt" or not session.prompt:
        return False, "思想钢印只能在响应时使用"
    ptype = session.prompt.get("type")
    if ptype not in {"interrupt_trick", "respond_toxic"}:
        return False, "当前无法使用思想钢印"
    session.discard.append(card)
    session.prompt["nullified"] = True
    session.prompt["nullified_by"] = username
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


def play_realm(session: Any, username: str, card: dict[str, Any], target: str | None, _action: dict) -> tuple[bool, str]:
    rid = card.get("realm_id") or card.get("id")
    session.discard.append(card)
    if rid == "reckoning":
        ok, err = _require_alive_target(session, username, target)
        if not ok:
            # still spent — pick first other
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
            p["tech_lock_clear_at_turn_end"] = False  # sticky until cosmic safety
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
        session._log(f"{username} 虚境·终末：{target} 强化，下回合结束后死亡")
        return True, "终末"
    # myriad / afterglow placeholders
    drawn = session.draw_sys.draw_n(session.players[username]["tech_level"], 1)
    session.players[username]["hand"].extend(drawn)
    session._log(f"{username} 虚境·{card.get('name')}（占位）：摸 1 张")
    return True, f"虚境 {card.get('name')}"


HANDLERS: dict[str, Callable[..., tuple[bool, str]]] = {
    "sophon": play_sophon,
    "curtain": play_curtain,
    "wallfacer_plan": play_wallfacer,
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
}

# realm materialized cards use realm ids
for _rid in ("finale", "reckoning", "illusion", "cold_silence", "myriad", "afterglow"):
    HANDLERS[_rid] = play_realm

TARGET_TRICKS = {
    "sophon",
    "wallfacer_plan",
    "broadcast",
    "toxic_water",
    "four_dimension",
    "deadline",
    "dual_vector",
    "soap",
    "great_ravine",
    "killer_52",
    "reckoning",
    "finale",
}

SELF_OK = {"sophon", "curtain", "cradle", "hibernation", "deterrence", "swordholder", "red_coast"}


def legal_play(session: Any, username: str, card: dict[str, Any]) -> bool:
    cid = card.get("id")
    if cid not in HANDLERS and cid not in FIELD_IDS:
        return False
    if not session._card_implemented(card):
        return False
    if cid in TARGET_TRICKS and cid not in SELF_OK:
        if cid == "sophon":
            return bool(session._alive_players())
        return bool(_alive_others(session, username))
    if cid == "guzheng_plan":
        return len(session.players[username]["hand"]) >= 2
    if cid in {"thought_stamp", "return_motion"}:
        return False  # only in prompt
    return True
