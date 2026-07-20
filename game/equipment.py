"""Equipment slots and on-equip / passive helpers (Phase B)."""

from __future__ import annotations

from typing import Any

# Primary document slots (legacy stellar/stability kept empty)
PRIMARY_SLOTS = ("ship", "armor", "temp_ascend")
ALL_SLOTS = ("ship", "armor", "temp_ascend", "stellar_track", "stability_system")
SLOT_LABELS = {
    "ship": "船",
    "armor": "甲",
    "temp_ascend": "临时飞升",
    "stellar_track": "恒星航迹",
    "stability_system": "维稳系统",
}


def empty_equipment() -> dict[str, Any | None]:
    return {slot: None for slot in ALL_SLOTS}


def resolve_slot(card: dict[str, Any]) -> str | None:
    if card.get("slot"):
        return str(card["slot"])
    if card.get("ship_id") or card.get("type") == "ship":
        return "ship"
    if card.get("armor_id") or card.get("type") == "armor":
        return "armor"
    return None


def apply_equip_bonuses(player: dict[str, Any], card: dict[str, Any], *, equipping: bool) -> list[str]:
    """Apply or reverse static bonuses when equipment enters/leaves. Returns log bits."""
    sign = 1 if equipping else -1
    notes: list[str] = []
    cid = card.get("id") or card.get("ship_id") or card.get("armor_id")

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

    return notes
