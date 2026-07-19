"""Kill / dodge combat helpers."""

from __future__ import annotations

from typing import Any

from game.stats import final_basic_damage, kill_base_and_vision


def compute_kill_damage(tier: int, source: dict[str, Any], target: dict[str, Any]) -> int:
    base, vision_extra = kill_base_and_vision(tier)
    if target.get("vision_exposed"):
        base += vision_extra
    return final_basic_damage(base, int(source.get("damage_bonus", 0)), int(target.get("damage_reduction", 0)))


def can_dodge(dodge_card: dict[str, Any], kill_tier: int) -> bool:
    return int(dodge_card.get("tier", 0)) >= int(kill_tier)
