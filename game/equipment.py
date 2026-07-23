"""Equipment slots and on-equip / passive helpers (Phase B)."""

from __future__ import annotations

from typing import Any

# Primary document slots (legacy stellar/stability kept empty)
# temp_ascend cards apply as statuses, not equipment slots
PRIMARY_SLOTS = ("ship", "armor")
ALL_SLOTS = ("ship", "armor", "temp_ascend", "stellar_track", "stability_system")
TEMP_ASCEND_IDS = frozenset({"nano_center", "chip_workshop", "stars_plan"})
SPECIAL_ARMOR_IDS = frozenset(
    {"plan_part", "black_hole", "micro_universe", "death_immortal", "quantum_ghost"}
)
SLOT_LABELS = {
    "ship": "船",
    "armor": "甲",
    "temp_ascend": "临时飞升",
    "stellar_track": "恒星航迹",
    "stability_system": "维稳系统",
}


def empty_equipment() -> dict[str, Any | None]:
    return {slot: None for slot in ALL_SLOTS}


def is_temp_ascend_card(card: dict[str, Any]) -> bool:
    cid = str(card.get("id") or "")
    return cid in TEMP_ASCEND_IDS or card.get("slot") == "temp_ascend"


def resolve_slot(card: dict[str, Any]) -> str | None:
    """Resolve ship/armor/legacy equipment slot. Temp ascension is not a slot."""
    if is_temp_ascend_card(card):
        return None
    if card.get("slot"):
        slot = str(card["slot"])
        if slot == "temp_ascend":
            return None
        return slot
    cid = str(card.get("id") or card.get("ship_id") or card.get("armor_id") or "")
    if cid in SPECIAL_ARMOR_IDS:
        return "armor"
    if card.get("ship_id") or card.get("type") == "ship":
        return "ship"
    if card.get("armor_id") or card.get("type") == "armor":
        return "armor"
    return None


def equip_id(card: dict[str, Any]) -> str:
    return str(card.get("id") or card.get("ship_id") or card.get("armor_id") or "")


def has_ship(player: dict[str, Any], ship_id: str) -> bool:
    ship = (player.get("equipment") or {}).get("ship")
    return bool(ship and equip_id(ship) == ship_id)


def has_armor(player: dict[str, Any], armor_id: str) -> bool:
    armor = (player.get("equipment") or {}).get("armor")
    return bool(armor and equip_id(armor) == armor_id)


def apply_equip_bonuses(player: dict[str, Any], card: dict[str, Any], *, equipping: bool) -> list[str]:
    """Apply or reverse static bonuses when equipment enters/leaves. Returns log bits."""
    sign = 1 if equipping else -1
    notes: list[str] = []
    cid = equip_id(card)

    if cid == "blue_space":
        player["damage_bonus"] = max(0, player.get("damage_bonus", 0) + sign)
        notes.append("伤害加成" + ("+1" if equipping else "-1"))
    elif cid == "stars_plan":
        player["damage_bonus"] = max(0, player.get("damage_bonus", 0) + sign)
        notes.append("伤害加成" + ("+1" if equipping else "-1"))
    elif cid == "nano_center":
        player["damage_reduction"] = max(0, player.get("damage_reduction", 0) + sign)
        notes.append("伤害减免" + ("+1" if equipping else "-1"))
    elif cid == "chip_workshop":
        player["extra_draw"] = max(0, int(player.get("extra_draw", 0)) + sign)
        notes.append("摸牌" + ("+1" if equipping else "-1"))
    elif cid == "natural_selection" and equipping:
        player["max_hp"] = int(player.get("max_hp", 0)) + 3
        player["hp"] = min(player["max_hp"], player["hp"] + 2)
        notes.append("体力上限+3并回复2")
    elif cid == "natural_selection" and not equipping:
        player["max_hp"] = max(1, int(player.get("max_hp", 1)) - 3)
        player["hp"] = min(player["hp"], player["max_hp"])
        notes.append("体力上限-3")
    elif cid == "bronze_age" and equipping:
        player["tech_level"] = min(6, int(player.get("tech_level", 1)) + 1)
        player["bronze_age_regen"] = True
        notes.append("科技+1")
    elif cid == "bronze_age" and not equipping:
        player["bronze_age_regen"] = False
    elif cid == "quantum":
        player["kill_limit_bonus"] = max(0, int(player.get("kill_limit_bonus", 0)) + sign)
        notes.append("出杀上限" + ("+1" if equipping else "-1"))
    elif cid == "deep_sea":
        player["deep_sea"] = equipping
        notes.append("深海液" + ("启用" if equipping else "卸下"))
    elif cid == "eco_bottle":
        player["eco_bottle"] = equipping
        notes.append("生态瓶" + ("启用" if equipping else "卸下"))
    elif cid == "lightspeed_2":
        if equipping:
            player["lightspeed_stacks"] = 0
            player["lightspeed_reduction"] = 0
        else:
            player.pop("lightspeed_stacks", None)
            player.pop("lightspeed_reduction", None)
        notes.append("光速2号" + ("启用" if equipping else "卸下"))
    elif cid == "gravity":
        player["gravity_ship"] = equipping
        notes.append("万有引力号" + ("启用" if equipping else "卸下"))
    elif cid == "star_ring":
        player["star_ring"] = equipping
        notes.append("星环号" + ("启用" if equipping else "卸下"))
    elif cid == "ultimate_law":
        if equipping:
            player["ultimate_law_used"] = False
        else:
            player.pop("ultimate_law_used", None)
        notes.append("终极规律号" + ("启用" if equipping else "卸下"))
    elif cid == "curvature":
        player["curvature"] = equipping
        notes.append("曲率引擎" + ("启用" if equipping else "卸下"))
    elif cid == "solar_observe":
        if equipping:
            player["cards_used_this_turn"] = 0
            player["solar_observe"] = True
        else:
            player.pop("solar_observe", None)
            player.pop("cards_used_this_turn", None)
        notes.append("太阳系观测单元" + ("启用" if equipping else "卸下"))
    elif cid == "plan_part":
        if equipping:
            player["plan_part_charges"] = 2
        else:
            player.pop("plan_part_charges", None)
        notes.append("计划的一部分" + ("启用" if equipping else "卸下"))
    elif cid == "black_hole":
        if equipping:
            player["black_hole_basics"] = 0
        else:
            player.pop("black_hole_basics", None)
        notes.append("黑洞" + ("启用" if equipping else "卸下"))
    elif cid == "micro_universe":
        if equipping:
            player["shield"] = int(player.get("shield", 0)) + 5
            notes.append("护盾+5")
        else:
            # leave remaining shield; only strip if we track source — keep simple: clamp down by leftover mark
            player["shield"] = max(0, int(player.get("shield", 0)) - int(player.get("micro_universe_shield", 5)))
            player.pop("micro_universe_shield", None)
            notes.append("卸下小宇宙")
        if equipping:
            player["micro_universe_shield"] = 5
    elif cid == "death_immortal":
        player["death_immortal"] = equipping
        notes.append("死神永生" + ("启用" if equipping else "卸下"))
    elif cid == "quantum_ghost":
        if equipping:
            player["quantum_ghost_hp"] = 1
            notes.append("嘲讽替身+1")
        else:
            player.pop("quantum_ghost_hp", None)
            notes.append("替身消散")

    return notes
