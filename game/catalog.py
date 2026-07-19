"""Load structured card catalog from data/catalog/."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CATALOG_DIR = Path(__file__).resolve().parent.parent / "data" / "catalog"


def _load(name: str) -> dict[str, Any]:
    path = CATALOG_DIR / name
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_pools() -> dict[str, Any]:
    return _load("pools.json")


def load_card_defs() -> dict[str, dict[str, Any]]:
    return dict(_load("cards.json").get("cards", {}))


def load_ships() -> list[dict[str, Any]]:
    return list(_load("ships.json").get("ships", []))


def load_armors() -> list[dict[str, Any]]:
    return list(_load("armors.json").get("armors", []))


def load_realms() -> list[dict[str, Any]]:
    return list(_load("realms.json").get("realms", []))
