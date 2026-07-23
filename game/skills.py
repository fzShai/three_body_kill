"""Role skill helpers: locked vs sealable skills."""

from __future__ import annotations

from typing import Any

STATUS_SKILLS_SEALED = "skills_sealed"

SKILL_STARSHIP = "starship"
SKILL_WANDER = "wander"
SKILL_NATIVE = "native"
SKILL_COHESION = "cohesion"
SKILL_SWORD_HOLDER = "sword_holder"
SKILL_WALLFACER = "wallfacer"
SKILL_RED_SHORE = "red_shore"
SKILL_LEADER = "leader"
SKILL_BENEVOLENCE = "benevolence"
SKILL_MADONNA = "madonna"
SKILL_COUNTDOWN = "countdown"
SKILL_FLYING_BLADE = "flying_blade"

LOCKED_SKILLS = frozenset(
    {
        SKILL_STARSHIP,
        SKILL_NATIVE,
        SKILL_SWORD_HOLDER,
        SKILL_MADONNA,
        SKILL_COUNTDOWN,
    }
)


def role_skills(player: dict[str, Any]) -> list[dict[str, Any]]:
    return list(player.get("skills") or [])


def role_has_skill(player: dict[str, Any], skill_id: str) -> bool:
    return any(s.get("id") == skill_id for s in role_skills(player))


def skill_def(player: dict[str, Any], skill_id: str) -> dict[str, Any] | None:
    for s in role_skills(player):
        if s.get("id") == skill_id:
            return s
    return None


def is_locked_skill(skill_id: str, player: dict[str, Any] | None = None) -> bool:
    if skill_id in LOCKED_SKILLS:
        return True
    if player:
        s = skill_def(player, skill_id)
        if s and s.get("locked"):
            return True
    return False


def has_skills_sealed(player: dict[str, Any]) -> bool:
    return any(s.get("id") == STATUS_SKILLS_SEALED for s in player.get("statuses") or [])


def skill_active(player: dict[str, Any], skill_id: str) -> bool:
    """Locked skills always active if owned; non-locked blocked by skills_sealed."""
    if not role_has_skill(player, skill_id):
        return False
    if is_locked_skill(skill_id, player):
        return True
    return not has_skills_sealed(player)
