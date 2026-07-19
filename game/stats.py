"""Player combat stats: tech, vision, damage bonuses."""

from __future__ import annotations

import math
from typing import Any


def initial_combat_fields() -> dict[str, Any]:
    return {
        "tech_level": 1,
        "vision_exposed": False,
        "damage_bonus": 0,
        "damage_reduction": 0,
        "basic_cards_used": 0,
        "kills_used_this_turn": 0,
        "ascended": False,
        "ascension": None,
    }


def final_basic_damage(base: int, source_bonus: int, target_reduction: int) -> int:
    return max(0, math.floor(base + source_bonus - target_reduction))


def final_true_damage(base: int, source_bonus: int) -> int:
    return max(0, math.floor(base + source_bonus))


def kill_base_and_vision(tier: int) -> tuple[int, int]:
    """Return (base_damage, vision_extra) for a kill tier."""
    table = {
        1: (1, 1),
        2: (2, 1),
        3: (3, 1),
    }
    return table.get(tier, (1, 0))


def resolve_kill_tier(tech_level: int, tier_mode: str | None, fixed_tier: int | None, pools: dict) -> int:
    if fixed_tier:
        return int(fixed_tier)
    mapping = pools.get("kill_tier_by_tech", {}).get(str(tech_level)) or pools.get("kill_tier_by_tech", {}).get("1")
    mode = tier_mode or "low"
    return int(mapping.get(mode, 1))
