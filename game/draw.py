"""Tech-pool draw: casio(1, x) then map entry to a card instance."""

from __future__ import annotations

import random
from typing import Any

from game.catalog import load_armors, load_card_defs, load_pools, load_realms, load_ships
from game.stats import resolve_kill_tier


class DrawSystem:
    def __init__(self) -> None:
        self.pools = load_pools()
        self.card_defs = load_card_defs()
        self.ships = load_ships()
        self.armors = load_armors()
        self.realms = load_realms()
        self._uid = 0

    def casio(self, lo: int, hi: int) -> int:
        return random.randint(lo, hi)

    def _next_instance_id(self, card_id: str) -> str:
        self._uid += 1
        return f"{card_id}-{self._uid}"

    def pool_max(self, tech_level: int) -> int:
        caps = self.pools.get("tech_pool_max", {})
        return int(caps.get(str(max(1, min(6, tech_level))), 22))

    def materialize_entry(self, entry_no: int, tech_level: int) -> dict[str, Any]:
        entry_map = self.pools.get("entry_map", {})
        key = entry_map.get(str(entry_no), "peach")
        base = dict(self.card_defs.get(key) or self.card_defs["peach"])

        if key == "ship_bucket" and self.ships:
            ship = random.choice(self.ships)
            base = {
                **base,
                "id": ship["id"],
                "name": ship["name"],
                "text": ship.get("text", ""),
                "ship_id": ship["id"],
                "type": "equipment",
                "slot": "ship",
                "implemented": bool(ship.get("implemented", False)),
                "needs": list(ship.get("needs") or []),
            }
        elif key == "armor_bucket" and self.armors:
            armor = random.choice(self.armors)
            base = {
                **base,
                "id": armor["id"],
                "name": armor["name"],
                "text": armor.get("text", ""),
                "armor_id": armor["id"],
                "type": "equipment",
                "slot": "armor",
                "implemented": bool(armor.get("implemented", False)),
                "needs": list(armor.get("needs") or []),
            }
        elif key == "realm_bucket" and self.realms:
            realm = random.choice(self.realms)
            base = {
                **base,
                "id": realm["id"],
                "name": realm["name"],
                "text": realm.get("text", ""),
                "realm_id": realm["id"],
                "implemented": bool(realm.get("implemented", False)),
                "needs": list(realm.get("needs") or []),
            }

        subtype = base.get("subtype")
        if subtype in {"kill", "dodge"}:
            tier = resolve_kill_tier(
                tech_level,
                base.get("tier_mode"),
                base.get("tier"),
                self.pools,
            )
            base["tier"] = tier
            if subtype == "kill":
                base["name"] = f"{tier}阶杀"
            else:
                base["name"] = f"{tier}阶闪"

        base["instance_id"] = self._next_instance_id(str(base["id"]))
        base["pool_entry"] = entry_no
        return base

    def draw_one(self, tech_level: int) -> dict[str, Any]:
        x = self.pool_max(tech_level)
        entry = self.casio(1, x)
        return self.materialize_entry(entry, tech_level)

    def draw_n(self, tech_level: int, n: int) -> list[dict[str, Any]]:
        return [self.draw_one(tech_level) for _ in range(n)]
