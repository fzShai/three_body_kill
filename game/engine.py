"""Authoritative game session — Phase A core rules engine."""

from __future__ import annotations

import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from game.catalog import load_card_defs
from game.combat import can_dodge, compute_kill_damage
from game.draw import DrawSystem
from game.equipment import (
    ALL_SLOTS,
    SLOT_LABELS,
    TEMP_ASCEND_IDS,
    apply_equip_bonuses,
    empty_equipment,
    is_temp_ascend_card,
    resolve_slot,
)
from game.stats import initial_combat_fields
from game.turn import hand_limit

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TURN_SECONDS = 20.0
STATUS_LOCKED = "locked"
STATUS_KINDS = ("positive", "negative")
EQUIP_SLOTS = ALL_SLOTS


def load_roles() -> list[dict[str, Any]]:
    import json

    path = DATA_DIR / "roles.json"
    with path.open("r", encoding="utf-8") as f:
        return list(json.load(f).get("roles", []))


def _empty_equipment() -> dict[str, Any | None]:
    return empty_equipment()


def _assign_roles(player_names: list[str], roles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    import random

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
            "extra_draw": 0,
            "kill_limit_bonus": 0,
            "vision_clear_at_turn_end": False,
            "red_coast_used": False,
            **initial_combat_fields(),
        }
    return assigned


class GameSession:
    """In-memory authoritative game state (Phase A)."""

    def __init__(self, room_id: str, player_names: list[str]) -> None:
        self.room_id = room_id
        self.player_order = list(player_names)
        self.phase = "dealing"  # dealing | turn | prompt | dying | ended
        self.turn_phase = "play"  # draw | play | discard (within turn)
        self.turn_index = 0
        self.seq = 0
        self.log: list[str] = []
        self.roles_catalog = load_roles()
        self.card_defs = load_card_defs()
        self.draw_sys = DrawSystem()
        self.discard: list[dict[str, Any]] = []
        self.players = _assign_roles(player_names, self.roles_catalog)
        self.winner: str | None = None
        self.winner_faction: str | None = None
        self.player_online: dict[str, bool] = {name: True for name in player_names}
        self.turn_deadline_at = 0.0
        self.prompt: dict[str, Any] | None = None
        self.dying: dict[str, Any] | None = None
        self._deal_initial()
        self.phase = "turn"
        self.turn_phase = "draw"
        self._run_draw_phase()
        self._start_turn_timer()
        self._log(f"对局开始，先手：{self.current_player()}")

    @classmethod
    def create(cls, room_id: str, player_names: list[str]) -> GameSession:
        return cls(room_id=room_id, player_names=player_names)

    def _log(self, text: str) -> None:
        self.log.append(text)
        if len(self.log) > 100:
            self.log = self.log[-100:]

    def current_player(self) -> str:
        return self.player_order[self.turn_index % len(self.player_order)]

    def _start_turn_timer(self) -> None:
        self.turn_deadline_at = time.time() + TURN_SECONDS

    def refresh_turn_timer(self) -> None:
        if self.phase in {"turn", "prompt", "dying"}:
            self._start_turn_timer()

    def _deal_initial(self) -> None:
        for name in self.player_order:
            tech = self.players[name]["tech_level"]
            cards = self.draw_sys.draw_n(tech, 6)
            self.players[name]["hand"].extend(cards)
            self._log(f"{name} 开局摸 6 张")

    def _run_draw_phase(self) -> None:
        name = self.current_player()
        p = self.players[name]
        n = 3
        if p.get("ascension") == "psychic":
            n += 1
        n += int(p.get("extra_draw", 0))
        drawn = self.draw_sys.draw_n(p["tech_level"], n)
        p["hand"].extend(drawn)
        self.turn_phase = "play"
        p["kills_used_this_turn"] = 0
        p["red_coast_used"] = False
        if p.get("bronze_age_regen"):
            self._heal(name, 1)
            self._log(f"{name} 青铜时代号：回合开始回复 1 点")
        ship = (p.get("equipment") or {}).get("ship")
        if ship and (ship.get("id") == "quantum" or ship.get("ship_id") == "quantum"):
            kill = {
                "id": "kill_t3",
                "name": "3阶杀",
                "type": "basic",
                "subtype": "kill",
                "tier": 3,
                "instance_id": f"quantum-kill-{self.seq}-{name}",
                "implemented": True,
                "text": "量子号补给：三阶杀。",
            }
            p["hand"].append(kill)
            self._log(f"{name} 量子号：获得一张三阶杀")
        self._log(f"{name} 摸牌阶段摸了 {len(drawn)} 张")

    def _clear_vision_if_due(self, username: str) -> None:
        p = self.players[username]
        if p.get("vision_clear_at_turn_end") and p.get("vision_exposed"):
            p["vision_exposed"] = False
            p["vision_clear_at_turn_end"] = False
            self._log(f"{username} 的视野暴露结束")

    def _advance_turn(self) -> None:
        if self.phase == "ended":
            return
        name = self.current_player()
        if self.players[name].get("ascension") == "gene" and self.players[name]["alive"]:
            self._heal(name, 2)
            self._log(f"{name} 基因飞升：回合结束回复 2 点")
        self._clear_vision_if_due(name)
        n = len(self.player_order)
        for _ in range(n):
            self.turn_index = (self.turn_index + 1) % n
            nxt = self.current_player()
            if not self.players[nxt]["alive"]:
                continue
            if not self.player_online.get(nxt, True):
                self._log(f"{nxt} 离线，跳过回合")
                continue
            if self._has_status(nxt, STATUS_LOCKED):
                self._remove_status(nxt, STATUS_LOCKED)
                self._log(f"{nxt} 被锁死，跳过回合")
                continue
            self.phase = "turn"
            self.turn_phase = "draw"
            self._run_draw_phase()
            self._start_turn_timer()
            self._log(f"轮到 {nxt}")
            return
        self.phase = "ended"

    def expire_turn_if_due(self) -> bool:
        if time.time() < self.turn_deadline_at:
            return False
        if self.phase == "prompt" and self.prompt:
            self._log(f"{self.prompt.get('to')} 响应超时，视为不响应")
            self._resolve_kill_unanswered()
            self.seq += 1
            return True
        if self.phase == "dying":
            self._auto_resolve_dying()
            self.seq += 1
            return True
        if self.phase != "turn":
            return False
        name = self.current_player()
        if self.turn_phase in {"play", "discard"}:
            self._auto_discard(name)
        self._log(f"{name} 超时，结束回合")
        self._advance_turn()
        self._check_win()
        self.seq += 1
        return True

    def mark_disconnected(self, username: str) -> None:
        if username in self.players and self.players[username]["alive"]:
            self.player_online[username] = False
            self._log(f"{username} 断开连接（仍保留座位）")

    def sync_online(self, online_map: dict[str, bool]) -> None:
        for name in self.player_order:
            if name in online_map:
                self.player_online[name] = online_map[name]

    def skip_current_if_offline(self, username: str) -> bool:
        if self.player_online.get(username, True):
            return False
        if self.phase == "prompt" and self.prompt and self.prompt.get("to") == username:
            self._log(f"{username} 离线，视为不响应杀")
            self._resolve_kill_unanswered()
            self.seq += 1
            return True
        if self.phase != "turn" or self.current_player() != username:
            return False
        if self.turn_phase in {"play", "discard"}:
            self._auto_discard(username)
        self._log(f"{username} 离线，自动跳过回合")
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
            self.prompt = None
            self.dying = None
            if alive:
                self.winner = alive[0]
                self.winner_faction = self.players[alive[0]]["faction"]
                self._log(f"{self.winner} 获胜")
            else:
                self.winner = None
                self._log("无人存活，平局")
            return True
        if len(alive) >= len(self.player_order):
            return False
        factions = {self.players[n]["faction"] for n in alive}
        if factions <= {"earth"}:
            self.phase = "ended"
            self.winner = alive[0]
            self.winner_faction = "earth"
            self._log("地球阵营胜利")
            return True
        if factions <= {"eto", "trisolaris"}:
            self.phase = "ended"
            self.winner = alive[0]
            self.winner_faction = "trisolaris"
            self._log("三体相关阵营胜利")
            return True
        return False

    def _auto_discard(self, username: str) -> None:
        p = self.players[username]
        limit = hand_limit(p["max_hp"])
        while len(p["hand"]) > limit:
            card = p["hand"].pop()
            self.discard.append(card)
            self._log(f"{username} 弃置 {card.get('name')}")

    # --- status helpers (kept for compatibility) ---
    def _has_status(self, username: str, status_id: str) -> bool:
        return any(s.get("id") == status_id for s in self.players[username]["statuses"])

    def _apply_status(self, username: str, status_id: str, name: str, kind: str) -> bool:
        if kind not in STATUS_KINDS or self._has_status(username, status_id):
            return False
        self.players[username]["statuses"].append({"id": status_id, "name": name, "kind": kind})
        return True

    def _remove_status(self, username: str, status_id: str) -> bool:
        statuses = self.players[username]["statuses"]
        for i, s in enumerate(statuses):
            if s.get("id") == status_id:
                statuses.pop(i)
                return True
        return False

    def _raise_tech(self, username: str, by: int = 1) -> None:
        p = self.players[username]
        before = p["tech_level"]
        p["tech_level"] = min(6, p["tech_level"] + by)
        if before < 6 <= p["tech_level"] and not p.get("ascended"):
            self._grant_ascension(username)

    def _grant_ascension(self, username: str) -> None:
        import random

        p = self.players[username]
        choice = random.choice(["mech", "cyber", "gene", "psychic"])
        p["ascended"] = True
        p["ascension"] = choice
        labels = {"mech": "机械飞升", "cyber": "义体飞升", "gene": "基因飞升", "psychic": "灵能飞升"}
        if choice == "mech":
            p["damage_bonus"] += 1
        elif choice == "cyber":
            p["damage_reduction"] += 1
        self._log(f"{username} 科技达到 6，获得{labels[choice]}")

    def _clear_temp_ascend_statuses(self, username: str) -> None:
        """Reverse temp-ascend bonuses and drop those status entries."""
        p = self.players[username]
        remaining: list[dict[str, Any]] = []
        for s in p["statuses"]:
            sid = str(s.get("id") or "")
            if sid in TEMP_ASCEND_IDS:
                apply_equip_bonuses(p, {"id": sid}, equipping=False)
            else:
                remaining.append(s)
        p["statuses"] = remaining

    def _eliminate_player(self, username: str) -> None:
        t = self.players[username]
        t["alive"] = False
        t["hp"] = 0
        self.discard.extend(t["hand"])
        t["hand"] = []
        for slot in EQUIP_SLOTS:
            if t["equipment"].get(slot):
                self._unequip_slot(username, slot, to_discard=True)
        self._clear_temp_ascend_statuses(username)
        t["statuses"] = []

    def _incoming_damage(self, target: str, amount: int) -> int:
        """Apply armor / equipment modifiers to incoming final damage."""
        t = self.players[target]
        dmg = max(0, int(amount))
        if t.get("deep_sea") and not t.get("vision_exposed"):
            dmg = max(0, dmg - 1)
        if t.get("eco_bottle") and dmg > 3:
            dmg = 3
        if t.get("lightspeed_stacks") is not None:
            stacks = int(t.get("lightspeed_stacks", 0))
            red = min(3, stacks)
            dmg = max(0, dmg - red)
            t["lightspeed_stacks"] = min(3, stacks + 1)
            t["lightspeed_reduction"] = min(3, stacks + 1)
        return dmg

    def _deal_damage(self, source: str, target: str, final: int) -> str:
        t = self.players[target]
        final = self._incoming_damage(target, final)
        t["hp"] -= final
        msg = f"{target} 受到 {final} 点最终伤害（HP {t['hp']}）"
        if t["hp"] <= 0:
            msg += "，" + self._begin_dying(target)
        return msg

    def _heal(self, username: str, amount: int) -> None:
        p = self.players[username]
        bonus = 1 if p.get("deep_sea") and not p.get("vision_exposed") else 0
        p["hp"] = min(p["max_hp"], p["hp"] + amount + bonus)

    def _begin_dying(self, victim: str) -> str:
        self.phase = "dying"
        self.dying = {"victim": victim}
        self.prompt = None
        self._log(f"{victim} 进入濒死")
        self._start_turn_timer()
        # Phase A: auto-force peach if present, else die after prompt window via action/timeout
        return f"{victim} 濒死"

    def _force_peach_or_die(self, victim: str) -> None:
        p = self.players[victim]
        peach_idx = next((i for i, c in enumerate(p["hand"]) if c.get("subtype") == "heal" or c.get("id") == "peach"), None)
        if peach_idx is not None:
            card = p["hand"].pop(peach_idx)
            heal = int(card.get("heal", 2))
            p["hp"] = min(p["max_hp"], max(1, p["hp"] + heal))
            self.discard.append(card)
            self._log(f"{victim} 濒死强制使用 {card.get('name')}，HP {p['hp']}")
            self.dying = None
            self.phase = "turn"
            self.refresh_turn_timer()
            return
        self._eliminate_player(victim)
        self.dying = None
        self._log(f"{victim} 濒死无回复牌，出局")
        if not self._check_win():
            self.phase = "turn"
            self.refresh_turn_timer()

    def _auto_resolve_dying(self) -> None:
        if not self.dying:
            return
        self._force_peach_or_die(self.dying["victim"])

    def apply_action(self, username: str, action: dict[str, Any]) -> tuple[bool, str]:
        if self.phase == "ended":
            return False, "对局已结束"
        if username not in self.players:
            return False, "你不在对局中"
        if not self.players[username]["alive"] and self.phase != "dying":
            return False, "你已被淘汰"

        act = str(action.get("action", "")).strip()
        if act == "ping":
            return True, "pong"

        if self.phase == "dying":
            return self._apply_dying_action(username, action)

        if self.phase == "prompt":
            return self._apply_prompt_action(username, action)

        if self.phase != "turn":
            return False, "当前无法行动"

        if self.current_player() != username:
            return False, "还没轮到你"
        if not self.player_online.get(username, True):
            return False, "你已离线"

        if act == "end_play":
            self.turn_phase = "discard"
            self._log(f"{username} 进入弃牌阶段")
            self.refresh_turn_timer()
            self.seq += 1
            return True, "进入弃牌阶段"

        if act == "discard_done" or act == "pass":
            if self.turn_phase == "play":
                self.turn_phase = "discard"
                self._log(f"{username} 进入弃牌阶段")
                self.refresh_turn_timer()
                self.seq += 1
                return True, "进入弃牌阶段"
            if self.turn_phase != "discard":
                return False, "现在不是弃牌阶段"
            limit = hand_limit(self.players[username]["max_hp"])
            over = len(self.players[username]["hand"]) - limit
            if over > 0:
                return False, f"还需弃置 {over} 张牌"
            self._advance_turn()
            self._check_win()
            self.seq += 1
            return True, "回合结束"

        if act == "discard_card":
            if self.turn_phase != "discard":
                return False, "现在不是弃牌阶段"
            instance_id = str(action.get("instance_id", "")).strip()
            hand = self.players[username]["hand"]
            idx = next((i for i, c in enumerate(hand) if c["instance_id"] == instance_id), None)
            if idx is None:
                return False, "手牌中没有这张牌"
            card = hand.pop(idx)
            self.discard.append(card)
            self._log(f"{username} 弃置 {card.get('name')}")
            self.refresh_turn_timer()
            self.seq += 1
            return True, "已弃置"

        if act == "discard_for_tech":
            return self._discard_for_tech(username, action)

        if act == "recast":
            return self._recast(username, str(action.get("instance_id", "")).strip())

        if act == "play_card":
            return self._play_card(username, action)

        if act == "play_placeholder":
            # legacy no-op path: treat as end play
            return self.apply_action(username, {"action": "end_play"})

        return False, f"未知行动: {act}"

    def _apply_dying_action(self, username: str, action: dict[str, Any]) -> tuple[bool, str]:
        if not self.dying or self.dying.get("victim") != username:
            # others cannot act in Phase A dying; victim or timeout resolves
            if str(action.get("action")) == "dying_resolve":
                self._auto_resolve_dying()
                self.seq += 1
                return True, "濒死已结算"
            return False, "濒死阶段仅濒死者可结算"
        act = str(action.get("action", "")).strip()
        if act in {"dying_resolve", "dying_pass", "play_card"}:
            # play_card peach manually or force resolve
            if act == "play_card":
                instance_id = str(action.get("instance_id", "")).strip()
                hand = self.players[username]["hand"]
                idx = next((i for i, c in enumerate(hand) if c["instance_id"] == instance_id), None)
                if idx is None:
                    return False, "手牌中没有这张牌"
                card = hand[idx]
                if card.get("subtype") != "heal" and card.get("id") != "peach":
                    return False, "濒死只能使用治疗牌"
                hand.pop(idx)
                heal = int(card.get("heal", 2))
                self.players[username]["hp"] = min(self.players[username]["max_hp"], max(1, self.players[username]["hp"] + heal))
                self.discard.append(card)
                self.dying = None
                self.phase = "turn"
                self._log(f"{username} 濒死使用 {card.get('name')}，HP {self.players[username]['hp']}")
                self.refresh_turn_timer()
                self.seq += 1
                return True, "脱离濒死"
            self._force_peach_or_die(username)
            self.seq += 1
            return True, "濒死已结算"
        return False, "濒死阶段行动无效"

    def _alive_others(self, username: str) -> list[str]:
        return [n for n in self.player_order if n != username and self.players[n]["alive"]]

    @staticmethod
    def _is_basic_card(card: dict[str, Any]) -> bool:
        subtype = card.get("subtype")
        if subtype in {"kill", "dodge", "heal", "visitor"}:
            return True
        return card.get("id") in {"peach", "visitor"}

    def _discard_for_tech(self, username: str, action: dict[str, Any]) -> tuple[bool, str]:
        if self.turn_phase != "play":
            return False, "现在不是出牌阶段"
        raw_ids = action.get("instance_ids")
        if not isinstance(raw_ids, list):
            return False, "需要提供 instance_ids"
        ids = [str(x).strip() for x in raw_ids if str(x).strip()]
        if len(ids) != 4 or len(set(ids)) != 4:
            return False, "需要弃置恰好 4 张不同的基本牌"
        hand = self.players[username]["hand"]
        by_id = {c["instance_id"]: c for c in hand}
        cards: list[dict[str, Any]] = []
        for iid in ids:
            card = by_id.get(iid)
            if not card:
                return False, "手牌中没有所选牌"
            if not self._is_basic_card(card):
                return False, "只能弃置基本牌升科技"
            cards.append(card)
        remove_ids = set(ids)
        self.players[username]["hand"] = [c for c in hand if c["instance_id"] not in remove_ids]
        self.discard.extend(cards)
        self._raise_tech(username, 1)
        names = "、".join(c.get("name", "?") for c in cards)
        self._log(f"{username} 弃置 4 张基本牌（{names}）升科技至 {self.players[username]['tech_level']}")
        self.refresh_turn_timer()
        self.seq += 1
        return True, f"科技等级 {self.players[username]['tech_level']}"

    def _apply_temp_ascend(self, username: str, card: dict[str, Any]) -> tuple[bool, str]:
        if not self._card_implemented(card):
            return False, "该临时飞升效果尚未实装，可尝试重铸"
        cid = str(card.get("id") or "")
        name = str(card.get("name") or cid)
        if self._has_status(username, cid):
            return False, f"已拥有状态：{name}"
        if not self._apply_status(username, cid, name, "positive"):
            return False, f"无法施加状态：{name}"
        notes = apply_equip_bonuses(self.players[username], card, equipping=True)
        self.discard.append(card)
        note = f"（{', '.join(notes)}）" if notes else ""
        self._log(f"{username} 获得临时飞升：{name}{note}")
        return True, f"获得临时飞升：{name}"

    def _card_implemented(self, card: dict[str, Any]) -> bool:
        if "implemented" in card:
            return bool(card["implemented"])
        cid = card.get("id") or card.get("ship_id") or card.get("armor_id")
        defs = self.card_defs.get(str(cid or ""), {})
        if "implemented" in defs:
            return bool(defs["implemented"])
        if is_temp_ascend_card(card) or card.get("ship_id") or card.get("armor_id") or card.get("slot") in {
            "ship",
            "armor",
            "temp_ascend",
        }:
            known = {
                "blue_space", "natural_selection", "bronze_age", "quantum", "tang",
                "nano_center", "chip_workshop", "stars_plan",
                "deep_sea", "eco_bottle", "lightspeed_2",
            }
            return str(cid) in known
        return False

    def _card_has_legal_play(self, username: str, card: dict[str, Any]) -> bool:
        subtype = card.get("subtype")
        ctype = card.get("type")
        cid = card.get("id")

        if subtype == "dodge":
            return False
        if subtype == "kill":
            return bool(self._alive_others(username))
        if subtype == "heal" or cid == "peach":
            return True
        if subtype == "visitor" or cid == "visitor":
            return True
        if cid == "ladder_plan" or cid == "red_coast":
            if not self._card_implemented(card):
                return False
            if cid == "ladder_plan":
                return bool(self._alive_others(username))
            return True
        if is_temp_ascend_card(card):
            if not self._card_implemented(card):
                return False
            return not self._has_status(username, str(cid or ""))
        if ctype == "equipment" or resolve_slot(card):
            return self._card_implemented(card) and resolve_slot(card) is not None
        return False

    def _unequip_slot(self, username: str, slot: str, *, to_discard: bool = True) -> dict[str, Any] | None:
        p = self.players[username]
        old = p["equipment"].get(slot)
        if not old:
            return None
        notes = apply_equip_bonuses(p, old, equipping=False)
        p["equipment"][slot] = None
        if to_discard:
            self.discard.append(old)
        if old.get("id") == "tang" or old.get("ship_id") == "tang":
            drawn = self.draw_sys.draw_n(p["tech_level"], 2)
            p["hand"].extend(drawn)
            self._log(f"{username} 唐号离场：摸 {len(drawn)} 张")
        if notes:
            self._log(f"{username} 卸下 {old.get('name')}（{', '.join(notes)}）")
        return old

    def _equip_card(self, username: str, card: dict[str, Any]) -> tuple[bool, str]:
        slot = resolve_slot(card)
        if not slot:
            return False, "无法识别装备栏位"
        if not self._card_implemented(card):
            return False, "该装备效果尚未实装，可尝试重铸"
        p = self.players[username]
        self._unequip_slot(username, slot)
        p["equipment"][slot] = card
        notes = apply_equip_bonuses(p, card, equipping=True)
        if card.get("id") == "tang" or card.get("ship_id") == "tang":
            drawn = self.draw_sys.draw_n(p["tech_level"], 2)
            p["hand"].extend(drawn)
            self._log(f"{username} 唐号入场：摸 {len(drawn)} 张")
        label = SLOT_LABELS.get(slot, slot)
        note = f"（{', '.join(notes)}）" if notes else ""
        self._log(f"{username} 装备[{label}] {card.get('name')}{note}")
        return True, f"已装备 {card.get('name')}"

    def _recast(self, username: str, instance_id: str) -> tuple[bool, str]:
        if self.turn_phase != "play":
            return False, "现在不是出牌阶段"
        hand = self.players[username]["hand"]
        idx = next((i for i, c in enumerate(hand) if c["instance_id"] == instance_id), None)
        if idx is None:
            return False, "手牌中没有这张牌"
        card = hand[idx]
        if self._card_has_legal_play(username, card):
            return False, "该牌有合法打法，不能重铸"
        hand.pop(idx)
        self.discard.append(card)
        drawn = self.draw_sys.draw_one(self.players[username]["tech_level"])
        hand.append(drawn)
        self._log(f"{username} 重铸 {card.get('name')}，摸到 {drawn.get('name')}")
        self.refresh_turn_timer()
        self.seq += 1
        return True, f"重铸为 {drawn.get('name')}"

    def _play_card(self, username: str, action: dict[str, Any]) -> tuple[bool, str]:
        if self.turn_phase != "play":
            return False, "现在不是出牌阶段"
        instance_id = str(action.get("instance_id", "")).strip()
        target = str(action.get("target", "")).strip() or None
        hand = self.players[username]["hand"]
        idx = next((i for i, c in enumerate(hand) if c["instance_id"] == instance_id), None)
        if idx is None:
            return False, "手牌中没有这张牌"
        card = hand.pop(idx)
        subtype = card.get("subtype")
        ctype = card.get("type")
        cid = card.get("id")

        if subtype == "kill":
            ok, msg = self._play_kill(username, card, target)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self.refresh_turn_timer()
            self.seq += 1
            return True, msg

        if subtype == "dodge":
            hand.insert(idx, card)
            return False, "闪只能在响应时打出"

        if subtype == "heal" or cid == "peach":
            if target and target != username:
                hand.insert(idx, card)
                return False, "正常情况下桃只能对自己使用"
            heal = int(card.get("heal", 2))
            self._heal(username, heal)
            self.discard.append(card)
            self._log(f"{username} 使用桃，HP {self.players[username]['hp']}")
            self.refresh_turn_timer()
            self.seq += 1
            return True, f"回复至 {self.players[username]['hp']} HP"

        if subtype == "visitor" or cid == "visitor":
            self.discard.append(card)
            self._raise_tech(username, 1)
            self._log(f"{username} 使用天外来客，科技等级 {self.players[username]['tech_level']}")
            self.refresh_turn_timer()
            self.seq += 1
            return True, f"科技等级 {self.players[username]['tech_level']}"

        if cid == "ladder_plan":
            ok, msg = self._play_ladder_plan(username, card, target)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self.refresh_turn_timer()
            self.seq += 1
            return True, msg

        if cid == "red_coast":
            ok, msg = self._play_red_coast(username, card)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self.refresh_turn_timer()
            self.seq += 1
            return True, msg

        if is_temp_ascend_card(card):
            ok, msg = self._apply_temp_ascend(username, card)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self.refresh_turn_timer()
            self.seq += 1
            return True, msg

        if ctype == "equipment" or resolve_slot(card):
            ok, msg = self._equip_card(username, card)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self.refresh_turn_timer()
            self.seq += 1
            return True, msg

        hand.insert(idx, card)
        needs = card.get("needs") or self.card_defs.get(str(cid or ""), {}).get("needs") or []
        if needs:
            return False, f"该牌未实装（依赖：{', '.join(needs)}），可尝试重铸"
        return False, "该牌效果尚未实装，可尝试重铸"

    def _play_ladder_plan(self, username: str, card: dict[str, Any], target: str | None) -> tuple[bool, str]:
        if not target or target not in self.players:
            return False, "阶梯计划需要指定目标"
        if target == username:
            return False, "不能以自己为目标"
        if not self.players[target]["alive"]:
            return False, "目标已淘汰"
        t = self.players[target]
        t["vision_exposed"] = True
        t["vision_clear_at_turn_end"] = True
        self.discard.append(card)
        self._log(f"{username} 对 {target} 使用阶梯计划：视野暴露至其回合结束")
        return True, f"{target} 视野已暴露"

    def _play_red_coast(self, username: str, card: dict[str, Any]) -> tuple[bool, str]:
        p = self.players[username]
        if p.get("red_coast_used"):
            return False, "红岸计划每回合限一次"
        drawn = self.draw_sys.draw_n(p["tech_level"], 2)
        p["hand"].extend(drawn)
        p["red_coast_used"] = True
        self.discard.append(card)
        self._log(f"{username} 使用红岸计划，摸 {len(drawn)} 张")
        return True, f"摸了 {len(drawn)} 张"

    def _play_kill(self, username: str, card: dict[str, Any], target: str | None) -> tuple[bool, str]:
        p = self.players[username]
        kill_limit = 2 + int(p.get("kill_limit_bonus", 0))
        if p["kills_used_this_turn"] >= kill_limit:
            return False, f"本回合出杀已达上限（{kill_limit}）"
        if not target or target not in self.players:
            return False, "杀需要指定目标"
        if target == username:
            return False, "不能杀自己"
        if not self.players[target]["alive"]:
            return False, "目标已淘汰"
        tier = int(card.get("tier", 1))
        p["kills_used_this_turn"] += 1
        self.discard.append(card)
        self.prompt = {
            "type": "respond_dodge",
            "from": username,
            "to": target,
            "kill_tier": tier,
            "card_name": card.get("name"),
        }
        self.phase = "prompt"
        self._log(f"{username} 对 {target} 使用{card.get('name')}，等待闪响应")
        self._start_turn_timer()
        return True, f"等待 {target} 响应"

    def _apply_prompt_action(self, username: str, action: dict[str, Any]) -> tuple[bool, str]:
        if not self.prompt or self.prompt.get("type") != "respond_dodge":
            return False, "当前无响应"
        if self.prompt.get("to") != username:
            return False, "不是你的响应"
        act = str(action.get("action", "")).strip()
        if act in {"respond_pass", "pass"}:
            self._resolve_kill_unanswered()
            self.seq += 1
            return True, "不响应"
        if act == "respond_dodge" or act == "play_card":
            instance_id = str(action.get("instance_id", "")).strip()
            hand = self.players[username]["hand"]
            idx = next((i for i, c in enumerate(hand) if c["instance_id"] == instance_id), None)
            if idx is None:
                return False, "手牌中没有这张牌"
            card = hand[idx]
            if card.get("subtype") != "dodge":
                return False, "请打出闪来响应"
            if not can_dodge(card, int(self.prompt["kill_tier"])):
                return False, "闪的阶数不足以响应此杀"
            hand.pop(idx)
            self.discard.append(card)
            self._log(f"{username} 打出{card.get('name')}，响应成功")
            self.prompt = None
            self.phase = "turn"
            self.refresh_turn_timer()
            self.seq += 1
            return True, "成功闪避"
        return False, "无效的响应行动"

    def _resolve_kill_unanswered(self) -> None:
        if not self.prompt:
            return
        src = self.prompt["from"]
        tgt = self.prompt["to"]
        tier = int(self.prompt["kill_tier"])
        dmg = compute_kill_damage(tier, self.players[src], self.players[tgt])
        self.prompt = None
        msg = self._deal_damage(src, tgt, dmg)
        self._log(msg)
        if self.phase not in {"dying", "ended"}:
            self.phase = "turn"
            self.refresh_turn_timer()
        self._check_win()

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
            "tech_level": p["tech_level"],
            "vision_exposed": p["vision_exposed"],
            "damage_bonus": p["damage_bonus"],
            "damage_reduction": p["damage_reduction"],
            "ascension": p.get("ascension"),
            "faction": p["faction"] if self.phase == "ended" else None,
            "role_name": p["role_name"] if self.phase == "ended" else None,
        }

    def snapshot_for(self, viewer: str) -> dict[str, Any]:
        me = self.players.get(viewer)
        private_hand = deepcopy(me["hand"]) if me else []
        private_role = None
        if me:
            private_role = {
                "role_id": me["role_id"],
                "role_name": me["role_name"],
                "faction": me["faction"],
            }
        timed = self.phase in {"turn", "prompt", "dying"}
        remaining = max(0.0, self.turn_deadline_at - time.time()) if timed else 0.0
        limit = hand_limit(me["max_hp"]) if me else 0
        return {
            "room_id": self.room_id,
            "phase": self.phase,
            "turn_phase": self.turn_phase if self.phase == "turn" else None,
            "seq": self.seq,
            "current_player": self.current_player() if self.phase != "ended" else None,
            "player_order": self.player_order,
            "players": [self.public_player_view(n) for n in self.player_order],
            "deck_count": None,
            "discard_count": len(self.discard),
            "log": self.log[-16:],
            "winner": self.winner,
            "winner_faction": self.winner_faction,
            "turn_seconds": TURN_SECONDS,
            "turn_remaining": remaining,
            "turn_deadline_ms": int(self.turn_deadline_at * 1000) if timed else None,
            "prompt": deepcopy(self.prompt),
            "dying": deepcopy(self.dying),
            "you": {
                "username": viewer,
                "hand": private_hand,
                "role": private_role,
                "hp": me["hp"] if me else 0,
                "alive": me["alive"] if me else False,
                "tech_level": me["tech_level"] if me else 1,
                "hand_limit": limit,
                "kills_used_this_turn": me["kills_used_this_turn"] if me else 0,
            },
        }
