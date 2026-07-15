"""Authoritative game session and placeholder rules engine."""

from __future__ import annotations

import json
import random
from copy import deepcopy
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_cards() -> list[dict[str, Any]]:
    path = DATA_DIR / "cards.json"
    with path.open("r", encoding="utf-8") as f:
        return list(json.load(f).get("cards", []))


def load_roles() -> list[dict[str, Any]]:
    path = DATA_DIR / "roles.json"
    with path.open("r", encoding="utf-8") as f:
        return list(json.load(f).get("roles", []))


def _deck_from_catalog(catalog: list[dict[str, Any]], copies: int = 3) -> list[dict[str, Any]]:
    deck: list[dict[str, Any]] = []
    uid = 0
    for _ in range(copies):
        for c in catalog:
            uid += 1
            deck.append({**c, "instance_id": f"{c['id']}-{uid}"})
    random.shuffle(deck)
    return deck


def _assign_roles(player_names: list[str], roles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    pool = roles[:]
    random.shuffle(pool)
    assigned: dict[str, dict[str, Any]] = {}
    for i, name in enumerate(player_names):
        role = pool[i % len(pool)]
        assigned[name] = {
            "role_id": role["id"],
            "role_name": role["name"],
            "faction": role["faction"],
            "hp": role["hp"],
            "max_hp": role["hp"],
            "alive": True,
            "skip_next": False,
            "hand": [],
        }
    return assigned


class GameSession:
    """In-memory authoritative game state."""

    def __init__(self, room_id: str, player_names: list[str]) -> None:
        self.room_id = room_id
        self.player_order = list(player_names)
        self.phase = "dealing"  # dealing | turn | ended
        self.turn_index = 0
        self.seq = 0
        self.log: list[str] = []
        self.catalog = load_cards()
        self.roles_catalog = load_roles()
        self.deck = _deck_from_catalog(self.catalog)
        self.discard: list[dict[str, Any]] = []
        self.players = _assign_roles(player_names, self.roles_catalog)
        self.winner: str | None = None
        self.winner_faction: str | None = None
        self._deal_initial()
        self.phase = "turn"
        self._log(f"对局开始，先手：{self.current_player()}")

    @classmethod
    def create(cls, room_id: str, player_names: list[str]) -> GameSession:
        return cls(room_id=room_id, player_names=player_names)

    def _log(self, text: str) -> None:
        self.log.append(text)
        if len(self.log) > 80:
            self.log = self.log[-80:]

    def current_player(self) -> str:
        return self.player_order[self.turn_index % len(self.player_order)]

    def _draw(self, username: str, n: int = 1) -> list[dict[str, Any]]:
        drawn: list[dict[str, Any]] = []
        player = self.players[username]
        for _ in range(n):
            if not self.deck:
                if not self.discard:
                    break
                self.deck = self.discard[:]
                self.discard.clear()
                random.shuffle(self.deck)
            if not self.deck:
                break
            card = self.deck.pop()
            player["hand"].append(card)
            drawn.append(card)
        return drawn

    def _deal_initial(self) -> None:
        for name in self.player_order:
            self._draw(name, 4)

    def mark_disconnected(self, username: str) -> None:
        if username in self.players and self.players[username]["alive"]:
            self._log(f"{username} 断开连接（仍保留座位）")

    def _alive_players(self) -> list[str]:
        return [n for n in self.player_order if self.players[n]["alive"]]

    def _check_win(self) -> bool:
        alive = self._alive_players()
        if len(alive) <= 1:
            self.phase = "ended"
            if alive:
                self.winner = alive[0]
                self.winner_faction = self.players[alive[0]]["faction"]
                self._log(f"{self.winner} 获胜")
            else:
                self.winner = None
                self._log("无人存活，平局")
            return True

        factions = {self.players[n]["faction"] for n in alive}
        earthish = {"earth"}
        alien = {"eto", "trisolaris"}
        if factions <= earthish:
            self.phase = "ended"
            self.winner = alive[0]
            self.winner_faction = "earth"
            self._log("地球阵营胜利")
            return True
        if factions <= alien:
            self.phase = "ended"
            self.winner = alive[0]
            self.winner_faction = "trisolaris"
            self._log("三体相关阵营胜利")
            return True
        if "neutral" in factions and len(alive) == 2:
            observer = next((n for n in alive if self.players[n]["faction"] == "neutral"), None)
            if observer:
                self.phase = "ended"
                self.winner = observer
                self.winner_faction = "neutral"
                self._log(f"观察者 {observer} 获胜")
                return True
        return False

    def _advance_turn(self) -> None:
        if self.phase == "ended":
            return
        n = len(self.player_order)
        for _ in range(n):
            self.turn_index = (self.turn_index + 1) % n
            name = self.current_player()
            if not self.players[name]["alive"]:
                continue
            if self.players[name]["skip_next"]:
                self.players[name]["skip_next"] = False
                self._log(f"{name} 被锁死，跳过回合")
                continue
            self._draw(name, 1)
            self._log(f"轮到 {name}")
            return
        self.phase = "ended"

    def apply_action(self, username: str, action: dict[str, Any]) -> tuple[bool, str]:
        """Validate and apply a player action. Returns (ok, message)."""
        if self.phase == "ended":
            return False, "对局已结束"
        if username not in self.players:
            return False, "你不在对局中"
        if not self.players[username]["alive"]:
            return False, "你已被淘汰"

        act = str(action.get("action", "")).strip()
        if act == "ping":
            return True, "pong"

        if act == "pass":
            if self.current_player() != username:
                return False, "还没轮到你"
            self._log(f"{username} 选择过牌")
            self._advance_turn()
            self._check_win()
            self.seq += 1
            return True, "已过牌"

        if act == "play_placeholder":
            if self.current_player() != username:
                return False, "还没轮到你"
            hand = self.players[username]["hand"]
            if not hand:
                return False, "没有手牌可打出"
            card = hand.pop(0)
            self.discard.append(card)
            self._log(f"{username} 打出占位牌 {card['name']}")
            self._advance_turn()
            self._check_win()
            self.seq += 1
            return True, f"打出 {card['name']}"

        if act == "play_card":
            if self.current_player() != username:
                return False, "还没轮到你"
            instance_id = str(action.get("instance_id", "")).strip()
            target = str(action.get("target", "")).strip() or None
            hand = self.players[username]["hand"]
            idx = next((i for i, c in enumerate(hand) if c["instance_id"] == instance_id), None)
            if idx is None:
                return False, "手牌中没有这张牌"
            card = hand.pop(idx)
            ok, msg = self._resolve_card(username, card, target)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self.discard.append(card)
            self._log(f"{username} 打出 {card['name']}：{msg}")
            if self._check_win():
                self.seq += 1
                return True, msg
            self._advance_turn()
            self.seq += 1
            return True, msg

        return False, f"未知行动: {act}"

    def _resolve_card(self, username: str, card: dict[str, Any], target: str | None) -> tuple[bool, str]:
        cid = card["id"]
        p = self.players[username]

        def need_alive_target() -> tuple[bool, str]:
            if not target or target not in self.players:
                return False, "需要指定目标玩家"
            if not self.players[target]["alive"]:
                return False, "目标已淘汰"
            if target == username and cid in {"dark_forest", "droplet", "dimensions", "sophon", "probe"}:
                return False, "不能以此牌指定自己"
            return True, ""

        if cid == "escape":
            drawn = self._draw(username, 2)
            return True, f"抽了 {len(drawn)} 张牌"

        if cid == "broadcast":
            for name in self._alive_players():
                self._draw(name, 1)
            return True, "全员抽牌"

        if cid == "wallfacer":
            p["hp"] = min(p["max_hp"], p["hp"] + 1)
            return True, f"恢复至 {p['hp']} HP"

        if cid == "probe":
            ok, err = need_alive_target()
            if not ok:
                return False, err
            assert target is not None
            n = len(self.players[target]["hand"])
            return True, f"探测到 {target} 有 {n} 张手牌"

        if cid == "sophon":
            ok, err = need_alive_target()
            if not ok:
                return False, err
            assert target is not None
            self.players[target]["skip_next"] = True
            return True, f"{target} 下回合将被跳过"

        if cid in {"dark_forest", "droplet", "dimensions"}:
            ok, err = need_alive_target()
            if not ok:
                return False, err
            assert target is not None
            dmg = {"dark_forest": 2, "droplet": 1, "dimensions": 3}[cid]
            t = self.players[target]
            t["hp"] -= dmg
            msg = f"{target} 受到 {dmg} 点损伤（HP {t['hp']}）"
            if t["hp"] <= 0:
                t["alive"] = False
                t["hp"] = 0
                self.discard.extend(t["hand"])
                t["hand"] = []
                msg += f"，{target} 出局"
            return True, msg

        return True, "效果已结算"

    def public_player_view(self, name: str) -> dict[str, Any]:
        p = self.players[name]
        return {
            "username": name,
            "hp": p["hp"],
            "max_hp": p["max_hp"],
            "alive": p["alive"],
            "hand_count": len(p["hand"]),
            "skip_next": p["skip_next"],
            "faction": p["faction"] if self.phase == "ended" else None,
            "role_name": p["role_name"] if self.phase == "ended" else None,
        }

    def snapshot_for(self, viewer: str) -> dict[str, Any]:
        """Privacy-filtered state for one client."""
        me = self.players.get(viewer)
        private_hand = deepcopy(me["hand"]) if me else []
        private_role = None
        if me:
            private_role = {
                "role_id": me["role_id"],
                "role_name": me["role_name"],
                "faction": me["faction"],
            }
        return {
            "room_id": self.room_id,
            "phase": self.phase,
            "seq": self.seq,
            "current_player": self.current_player() if self.phase != "ended" else None,
            "player_order": self.player_order,
            "players": [self.public_player_view(n) for n in self.player_order],
            "deck_count": len(self.deck),
            "discard_count": len(self.discard),
            "log": self.log[-12:],
            "winner": self.winner,
            "winner_faction": self.winner_faction,
            "you": {
                "username": viewer,
                "hand": private_hand,
                "role": private_role,
                "hp": me["hp"] if me else 0,
                "alive": me["alive"] if me else False,
            },
        }
