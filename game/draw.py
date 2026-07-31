"""Tech-pool draw: casio(1, x) then map entry to a card instance."""

from __future__ import annotations

import random
from typing import Any

from game.catalog import load_armors, load_card_defs, load_pools, load_realms, load_ships
from game.stats import resolve_kill_tier

BUCKET_KEYS = frozenset({"ship_bucket", "armor_bucket", "realm_bucket"})


def clamp_tier(n: Any, default: int = 1) -> int:
    try:
        return max(1, min(6, int(n)))
    except (TypeError, ValueError):
        return default


def build_entry_ceilings(pools: dict[str, Any]) -> list[tuple[int, int]]:
    caps = pools.get("tech_pool_max") or {}
    ceilings = [(int(caps.get(str(tech), 22)), tech) for tech in range(1, 7)]
    ceilings.sort(key=lambda x: x[0])
    return ceilings


def tech_for_entry_no(entry_no: int, ceilings: list[tuple[int, int]]) -> int:
    for max_entry, tech in ceilings:
        if entry_no <= max_entry:
            return tech
    return 6


def build_unlock_tech_map(pools: dict[str, Any]) -> dict[str, int]:
    """card_id / bucket_key → lowest tech level that includes its earliest pool entry."""
    ceilings = build_entry_ceilings(pools)
    entry_map = pools.get("entry_map") or {}
    min_entry_by_id: dict[str, int] = {}
    for entry_str, card_id in entry_map.items():
        try:
            entry_no = int(entry_str)
        except (TypeError, ValueError):
            continue
        cid = str(card_id)
        prev = min_entry_by_id.get(cid)
        if prev is None or entry_no < prev:
            min_entry_by_id[cid] = entry_no

    return {cid: tech_for_entry_no(entry_no, ceilings) for cid, entry_no in min_entry_by_id.items()}


class DrawSystem:
    def __init__(self) -> None:
        self.pools = load_pools()
        self.card_defs = load_card_defs()
        self.ships = load_ships()
        self.armors = load_armors()
        self.realms = load_realms()
        self._uid = 0
        self._ceilings = build_entry_ceilings(self.pools)
        self.unlock_tech = build_unlock_tech_map(self.pools)

    def casio(self, lo: int, hi: int) -> int:
        return random.randint(lo, hi)

    def _next_instance_id(self, card_id: str) -> str:
        self._uid += 1
        return f"{card_id}-{self._uid}"

    def pool_max(self, tech_level: int) -> int:
        caps = self.pools.get("tech_pool_max", {})
        return int(caps.get(str(max(1, min(6, tech_level))), 22))

    def tech_for_entry(self, entry_no: int) -> int:
        return tech_for_entry_no(int(entry_no), self._ceilings)

    def resolve_visual_tier(self, card: dict[str, Any], *, bucket_key: str | None = None) -> int:
        """Kill/dodge → 阶; others → tech required for this pool_entry (else min unlock / bucket)."""
        subtype = card.get("subtype")
        if subtype in {"kill", "dodge"}:
            return clamp_tier(card.get("tier"), 1)

        entry = card.get("pool_entry")
        if entry is not None and str(entry).strip() != "":
            try:
                return self.tech_for_entry(int(entry))
            except (TypeError, ValueError):
                pass

        cid = str(card.get("id") or card.get("ship_id") or card.get("armor_id") or card.get("realm_id") or "")
        if cid and cid in self.unlock_tech:
            return self.unlock_tech[cid]
        if bucket_key and bucket_key in self.unlock_tech:
            return self.unlock_tech[bucket_key]
        return 1

    def stamp_visual_tier(self, card: dict[str, Any], *, bucket_key: str | None = None) -> dict[str, Any]:
        card["visual_tier"] = self.resolve_visual_tier(card, bucket_key=bucket_key)
        return card

    def materialize_entry(self, entry_no: int, tech_level: int) -> dict[str, Any]:
        entry_map = self.pools.get("entry_map", {})
        key = entry_map.get(str(entry_no), "peach")
        base = dict(self.card_defs.get(key) or self.card_defs["peach"])
        bucket_key: str | None = key if key in BUCKET_KEYS else None

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
        self.stamp_visual_tier(base, bucket_key=bucket_key)
        return base

    def draw_one(self, tech_level: int) -> dict[str, Any]:
        x = self.pool_max(tech_level)
        entry = self.casio(1, x)
        return self.materialize_entry(entry, tech_level)

    def draw_n(self, tech_level: int, n: int) -> list[dict[str, Any]]:
        return [self.draw_one(tech_level) for _ in range(n)]
