"""Authoritative game session and placeholder rules engine."""

from __future__ import annotations

import json
import random
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TURN_SECONDS = 12.0
EQUIP_SLOTS = ("stellar_track", "stability_system")
SLOT_LABELS = {
    "stellar_track": "恒星航迹",
    "stability_system": "维稳系统",
}
STATUS_LOCKED = "locked"
STATUS_KINDS = ("positive", "negative")


def _empty_equipment() -> dict[str, Any | None]:
    return {slot: None for slot in EQUIP_SLOTS}


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
            "hand": [],
            "equipment": _empty_equipment(),
            "statuses": [],
        }
    return assigned


class GameSession:
    """In-memory authoritative game state."""

    def __init__(self, room_id: str, player_names: list[str]) -> None:
        self.room_id = room_id
        self.player_order = list(player_names)
        self.phase = "dealing"  # dealing | turn | dying | ended
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
        self.player_online: dict[str, bool] = {name: True for name in player_names}
        self.turn_deadline_at = 0.0
        self.dying: dict[str, Any] | None = None
        self._deal_initial()
        self.phase = "turn"
        self._start_turn_timer()
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

    def _start_turn_timer(self) -> None:
        self.turn_deadline_at = time.time() + TURN_SECONDS

    def refresh_turn_timer(self) -> None:
        if self.phase in {"turn", "dying"}:
            self._start_turn_timer()

    def expire_turn_if_due(self) -> bool:
        """Auto-end current turn / dying response when the timer expires."""
        if time.time() < self.turn_deadline_at:
            return False
        if self.phase == "dying":
            name = self._dying_current()
            if name:
                self._log(f"{name} 濒死救援超时，自动跳过")
            self._advance_dying_responder()
            self.seq += 1
            return True
        if self.phase != "turn":
            return False
        name = self.current_player()
        self._log(f"{name} 出牌超时，自动结束回合")
        self._advance_turn()
        self._check_win()
        self.seq += 1
        return True

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
            self.player_online[username] = False
            self._log(f"{username} 断开连接（仍保留座位）")

    def sync_online(self, online_map: dict[str, bool]) -> None:
        for name in self.player_order:
            if name in online_map:
                self.player_online[name] = online_map[name]

    def skip_current_if_offline(self, username: str) -> bool:
        """If it's this player's turn / dying response and they are offline, auto-skip."""
        if self.player_online.get(username, True):
            return False
        if self.phase == "dying" and self._dying_current() == username:
            self._log(f"{username} 离线，跳过濒死救援")
            self._advance_dying_responder()
            self.seq += 1
            return True
        if self.phase != "turn":
            return False
        if self.current_player() != username:
            return False
        self._log(f"{username} 离线，自动跳过出牌阶段")
        self._advance_turn()
        self._check_win()
        self.seq += 1
        return True

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

        # No eliminations yet — do not end by faction (2-player same-faction
        # games would otherwise end on the first pass/skip/timeout).
        if len(alive) >= len(self.player_order):
            return False

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
            if not self.player_online.get(name, True):
                self._log(f"{name} 离线，跳过回合")
                continue
            if self._has_status(name, STATUS_LOCKED):
                self._remove_status(name, STATUS_LOCKED)
                self._log(f"{name} 被锁死，跳过回合")
                continue
            self._draw(name, 1)
            self._start_turn_timer()
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

        if self.phase == "dying":
            return self._apply_dying_action(username, action)

        if act == "pass":
            if self.current_player() != username:
                return False, "还没轮到你"
            if not self.player_online.get(username, True):
                return False, "你已离线"
            self._log(f"{username} 选择过牌")
            self._advance_turn()
            self._check_win()
            self.seq += 1
            return True, "已过牌"

        if act == "play_placeholder":
            if self.current_player() != username:
                return False, "还没轮到你"
            if not self.player_online.get(username, True):
                return False, "你已离线"
            hand = self.players[username]["hand"]
            if not hand:
                return False, "没有手牌可打出"
            card = hand.pop(0)
            self.discard.append(card)
            self._log(f"{username} 打出占位牌 {card['name']}")
            self.refresh_turn_timer()
            if self._check_win():
                self.seq += 1
                return True, f"打出 {card['name']}"
            self.seq += 1
            return True, f"打出 {card['name']}"

        if act == "play_card":
            if self.current_player() != username:
                return False, "还没轮到你"
            if not self.player_online.get(username, True):
                return False, "你已离线"
            instance_id = str(action.get("instance_id", "")).strip()
            target = str(action.get("target", "")).strip() or None
            hand = self.players[username]["hand"]
            idx = next((i for i, c in enumerate(hand) if c["instance_id"] == instance_id), None)
            if idx is None:
                return False, "手牌中没有这张牌"
            card = hand.pop(idx)
            if card.get("type") == "equipment":
                ok, msg = self._equip_card(username, card)
                if not ok:
                    hand.insert(idx, card)
                    return False, msg
                self._log(f"{username} {msg}")
            else:
                ok, msg = self._resolve_card(username, card, target)
                if not ok:
                    hand.insert(idx, card)
                    return False, msg
                self.discard.append(card)
                self._log(f"{username} 打出 {card['name']}：{msg}")
            self.refresh_turn_timer()
            if self.phase != "dying" and self._check_win():
                self.seq += 1
                return True, msg
            self.seq += 1
            return True, msg

        return False, f"未知行动: {act}"

    def _dying_order(self, victim: str) -> list[str]:
        n = len(self.player_order)
        start = self.player_order.index(victim)
        order: list[str] = []
        for i in range(n):
            name = self.player_order[(start + i) % n]
            if self.players[name]["alive"]:
                order.append(name)
        return order

    def _dying_current(self) -> str | None:
        if not self.dying:
            return None
        order = self.dying["order"]
        idx = self.dying["index"]
        if idx < 0 or idx >= len(order):
            return None
        return order[idx]

    def _begin_dying(self, victim: str) -> str:
        order = self._dying_order(victim)
        if not order:
            self._eliminate_player(victim)
            return f"{victim} 出局"
        self.phase = "dying"
        self.dying = {"victim": victim, "order": order, "index": -1}
        self._log(f"{victim} 进入濒死")
        self._advance_dying_responder()
        return f"{victim} 濒死，等待救援"

    def _advance_dying_responder(self) -> None:
        if not self.dying:
            return
        self.dying["index"] += 1
        while self.dying["index"] < len(self.dying["order"]):
            name = self.dying["order"][self.dying["index"]]
            if not self.players[name]["alive"]:
                self.dying["index"] += 1
                continue
            if not self.player_online.get(name, True):
                self._log(f"{name} 离线，跳过濒死救援")
                self.dying["index"] += 1
                continue
            self._start_turn_timer()
            self._log(f"濒死救援：轮到 {name}")
            return
        self._resolve_dying()

    def _resolve_dying(self) -> None:
        if not self.dying:
            return
        victim = self.dying["victim"]
        self.dying = None
        if self.players[victim]["hp"] > 0:
            self.phase = "turn"
            self._start_turn_timer()
            self._log(f"{victim} 脱离濒死（HP {self.players[victim]['hp']}）")
            return
        self._eliminate_player(victim)
        self._log(f"{victim} 濒死求援失败，出局")
        if self._check_win():
            return
        self.phase = "turn"
        self._start_turn_timer()

    def _apply_dying_action(self, username: str, action: dict[str, Any]) -> tuple[bool, str]:
        act = str(action.get("action", "")).strip()
        current = self._dying_current()
        if act == "dying_pass":
            if current != username:
                return False, "还没轮到你救援"
            self._log(f"{username} 结束濒死救援")
            self._advance_dying_responder()
            self.seq += 1
            return True, "已结束救援"

        if act == "play_card":
            if current != username:
                return False, "还没轮到你救援"
            if not self.dying:
                return False, "当前不在濒死结算"
            instance_id = str(action.get("instance_id", "")).strip()
            hand = self.players[username]["hand"]
            idx = next((i for i, c in enumerate(hand) if c["instance_id"] == instance_id), None)
            if idx is None:
                return False, "手牌中没有这张牌"
            card = hand.pop(idx)
            if card.get("type") != "heal":
                hand.insert(idx, card)
                return False, "濒死阶段只能打出治疗牌"
            victim = self.dying["victim"]
            ok, msg = self._resolve_card(username, card, victim)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self.discard.append(card)
            self._log(f"{username} 打出 {card['name']}：{msg}")
            self.refresh_turn_timer()
            self.seq += 1
            return True, msg

        return False, "濒死阶段只能出治疗牌或结束救援"

    def _equip_card(self, username: str, card: dict[str, Any]) -> tuple[bool, str]:
        slot = str(card.get("slot", "")).strip()
        if slot not in EQUIP_SLOTS:
            return False, "无效的装备栏"
        player = self.players[username]
        old = player["equipment"].get(slot)
        player["equipment"][slot] = card
        label = SLOT_LABELS[slot]
        if old:
            self.discard.append(old)
            return True, f"装备 {card['name']} 至{label}，弃置 {old['name']}"
        return True, f"装备 {card['name']} 至{label}"

    def _discard_equipment(self, username: str) -> None:
        player = self.players[username]
        for slot in EQUIP_SLOTS:
            card = player["equipment"].get(slot)
            if card:
                self.discard.append(card)
                player["equipment"][slot] = None

    def _eliminate_player(self, username: str) -> None:
        t = self.players[username]
        t["alive"] = False
        t["hp"] = 0
        self.discard.extend(t["hand"])
        t["hand"] = []
        self._discard_equipment(username)
        t["statuses"] = []

    def _has_status(self, username: str, status_id: str) -> bool:
        return any(s.get("id") == status_id for s in self.players[username]["statuses"])

    def _apply_status(self, username: str, status_id: str, name: str, kind: str) -> bool:
        if kind not in STATUS_KINDS:
            return False
        if self._has_status(username, status_id):
            return False
        self.players[username]["statuses"].append({
            "id": status_id,
            "name": name,
            "kind": kind,
        })
        return True

    def _remove_status(self, username: str, status_id: str) -> bool:
        statuses = self.players[username]["statuses"]
        for i, s in enumerate(statuses):
            if s.get("id") == status_id:
                statuses.pop(i)
                return True
        return False

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

        if card.get("type") == "heal":
            amount = int(card.get("heal", 0))
            if self.phase == "dying" and self.dying:
                heal_target = self.dying["victim"]
            else:
                heal_target = target or username
                if heal_target != username:
                    return False, "正常情况下只能对自己使用治疗牌"
            if heal_target not in self.players or not self.players[heal_target]["alive"]:
                return False, "目标无效"
            ht = self.players[heal_target]
            ht["hp"] = min(ht["max_hp"], ht["hp"] + amount)
            return True, f"{heal_target} 恢复至 {ht['hp']} HP"

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
            if self._apply_status(target, STATUS_LOCKED, "锁死", "negative"):
                return True, f"{target} 获得负面状态「锁死」"
            return True, f"{target} 已有「锁死」，未叠加"

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
                msg += "，" + self._begin_dying(target)
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
            "online": self.player_online.get(name, True),
            "equipment": deepcopy(p["equipment"]),
            "statuses": deepcopy(p["statuses"]),
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
        timed = self.phase in {"turn", "dying"}
        remaining = max(0.0, self.turn_deadline_at - time.time()) if timed else 0.0
        dying_view = None
        if self.dying:
            dying_view = {
                "victim": self.dying["victim"],
                "current": self._dying_current(),
                "order": list(self.dying["order"]),
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
            "turn_seconds": TURN_SECONDS,
            "turn_remaining": remaining,
            "turn_deadline_ms": int(self.turn_deadline_at * 1000) if timed else None,
            "dying": dying_view,
            "you": {
                "username": viewer,
                "hand": private_hand,
                "role": private_role,
                "hp": me["hp"] if me else 0,
                "alive": me["alive"] if me else False,
            },
        }
