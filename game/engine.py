"""Authoritative game session — Phase A core rules engine."""

from __future__ import annotations

import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from game.catalog import load_card_defs
from game.combat import can_dodge, compute_kill_damage
from game.draw import DrawSystem
from game.stats import initial_combat_fields
from game.turn import hand_limit

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TURN_SECONDS = 20.0
STATUS_LOCKED = "locked"
STATUS_KINDS = ("positive", "negative")
EQUIP_SLOTS = ("stellar_track", "stability_system", "ship", "armor", "temp_ascend")
SLOT_LABELS = {
    "stellar_track": "恒星航迹",
    "stability_system": "维稳系统",
    "ship": "船",
    "armor": "甲",
    "temp_ascend": "临时飞升",
}

# #region agent log
_DEBUG_LOG = Path(__file__).resolve().parent.parent / "debug-2b39ab.log"


def _dbg(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    try:
        import json

        payload = {
            "sessionId": "2b39ab",
            "runId": "offline-play",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "timestamp": int(time.time() * 1000),
        }
        with _DEBUG_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


# #endregion


def load_roles() -> list[dict[str, Any]]:
    import json

    path = DATA_DIR / "roles.json"
    with path.open("r", encoding="utf-8") as f:
        return list(json.load(f).get("roles", []))


def _empty_equipment() -> dict[str, Any | None]:
    return {slot: None for slot in EQUIP_SLOTS}


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
        drawn = self.draw_sys.draw_n(p["tech_level"], n)
        p["hand"].extend(drawn)
        self.turn_phase = "play"
        p["kills_used_this_turn"] = 0
        self._log(f"{name} 摸牌阶段摸了 {len(drawn)} 张")

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
        if self.turn_phase == "discard":
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
        # #region agent log
        _dbg("A", "engine.py:skip_current_if_offline:entry", "skip offline check", {
            "username": username,
            "phase": self.phase,
            "current": self.current_player() if self.phase != "ended" else None,
            "online": dict(self.player_online),
            "turn_index": self.turn_index,
        })
        # #endregion
        if self.player_online.get(username, True):
            return False
        if self.phase == "prompt" and self.prompt and self.prompt.get("to") == username:
            self._log(f"{username} 离线，视为不响应杀")
            self._resolve_kill_unanswered()
            self.seq += 1
            return True
        if self.phase != "turn" or self.current_player() != username:
            # #region agent log
            _dbg("C", "engine.py:skip_current_if_offline:no_skip", "offline but not current turn", {
                "username": username,
                "phase": self.phase,
                "current": self.current_player() if self.phase != "ended" else None,
            })
            # #endregion
            return False
        self._log(f"{username} 离线，自动跳过回合")
        self._advance_turn()
        self._check_win()
        self.seq += 1
        # #region agent log
        _dbg("A", "engine.py:skip_current_if_offline:after", "after skip offline", {
            "username": username,
            "phase": self.phase,
            "current": self.current_player() if self.phase != "ended" else None,
            "online": dict(self.player_online),
            "winner": self.winner,
            "turn_phase": self.turn_phase,
        })
        # #endregion
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

    def _advance_turn(self) -> None:
        if self.phase == "ended":
            return
        name = self.current_player()
        if self.players[name].get("ascension") == "gene" and self.players[name]["alive"]:
            self._heal(name, 2)
            self._log(f"{name} 基因飞升：回合结束回复 2 点")
        n = len(self.player_order)
        for _ in range(n):
            self.turn_index = (self.turn_index + 1) % n
            nxt = self.current_player()
            if not self.players[nxt]["alive"]:
                continue
            if not self.player_online.get(nxt, True):
                self._log(f"{nxt} 离线，跳过回合")
                # #region agent log
                _dbg("A", "engine.py:_advance_turn:skip_offline", "advance skipped offline seat", {
                    "nxt": nxt, "turn_index": self.turn_index, "online": dict(self.player_online),
                })
                # #endregion
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
            # #region agent log
            _dbg("A", "engine.py:_advance_turn:landed", "turn landed on player", {
                "nxt": nxt, "phase": self.phase, "turn_phase": self.turn_phase,
            })
            # #endregion
            return
        # #region agent log
        _dbg("A", "engine.py:_advance_turn:ended", "no eligible player, phase ended", {
            "online": dict(self.player_online),
            "alive": {n: self.players[n]["alive"] for n in self.player_order},
        })
        # #endregion
        self.phase = "ended"

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

    def _heal(self, username: str, amount: int) -> None:
        p = self.players[username]
        p["hp"] = min(p["max_hp"], p["hp"] + amount)

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

    def _note_basic_used(self, username: str) -> None:
        p = self.players[username]
        p["basic_cards_used"] += 1
        if p["basic_cards_used"] >= 4:
            p["basic_cards_used"] = 0
            self._raise_tech(username, 1)
            self._log(f"{username} 使用满 4 张基本牌，科技等级升至 {p['tech_level']}")

    def _eliminate_player(self, username: str) -> None:
        t = self.players[username]
        t["alive"] = False
        t["hp"] = 0
        self.discard.extend(t["hand"])
        t["hand"] = []
        for slot in EQUIP_SLOTS:
            card = t["equipment"].get(slot)
            if card:
                self.discard.append(card)
                t["equipment"][slot] = None
        t["statuses"] = []

    def _deal_damage(self, source: str, target: str, final: int) -> str:
        t = self.players[target]
        t["hp"] -= final
        msg = f"{target} 受到 {final} 点最终伤害（HP {t['hp']}）"
        if t["hp"] <= 0:
            msg += "，" + self._begin_dying(target)
        return msg

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
            # #region agent log
            _dbg("B", "engine.py:apply_action:ended", "reject: game ended", {
                "username": username, "action": action.get("action"), "winner": self.winner,
            })
            # #endregion
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
            # #region agent log
            _dbg("E", "engine.py:apply_action:bad_phase", "reject: not turn phase", {
                "username": username, "phase": self.phase, "action": act,
            })
            # #endregion
            return False, "当前无法行动"

        if self.current_player() != username:
            # #region agent log
            _dbg("C", "engine.py:apply_action:not_turn", "reject: not your turn", {
                "username": username,
                "current": self.current_player(),
                "online": dict(self.player_online),
                "action": act,
            })
            # #endregion
            return False, "还没轮到你"
        if not self.player_online.get(username, True):
            # #region agent log
            _dbg("D", "engine.py:apply_action:self_offline", "reject: actor offline", {
                "username": username, "online": dict(self.player_online), "action": act,
            })
            # #endregion
            return False, "你已离线"

        if act == "end_play":
            self.turn_phase = "discard"
            self._log(f"{username} 进入弃牌阶段")
            self.refresh_turn_timer()
            self.seq += 1
            return True, "进入弃牌阶段"

        if act == "discard_done" or act == "pass":
            if self.turn_phase == "play" and act == "pass":
                self.turn_phase = "discard"
            if self.turn_phase == "discard":
                self._auto_discard(username)
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

    def _recast(self, username: str, instance_id: str) -> tuple[bool, str]:
        hand = self.players[username]["hand"]
        idx = next((i for i, c in enumerate(hand) if c["instance_id"] == instance_id), None)
        if idx is None:
            return False, "手牌中没有这张牌"
        card = hand.pop(idx)
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

        if subtype == "kill":
            ok, msg = self._play_kill(username, card, target)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self._note_basic_used(username)
            self.refresh_turn_timer()
            self.seq += 1
            return True, msg

        if subtype == "dodge":
            hand.insert(idx, card)
            return False, "闪只能在响应时打出"

        if subtype == "heal" or card.get("id") == "peach":
            if target and target != username:
                hand.insert(idx, card)
                return False, "正常情况下桃只能对自己使用"
            heal = int(card.get("heal", 2))
            self._heal(username, heal)
            self.discard.append(card)
            self._note_basic_used(username)
            self._log(f"{username} 使用桃，HP {self.players[username]['hp']}")
            self.refresh_turn_timer()
            self.seq += 1
            return True, f"回复至 {self.players[username]['hp']} HP"

        if subtype == "visitor" or card.get("id") == "visitor":
            self.discard.append(card)
            self._note_basic_used(username)
            self._raise_tech(username, 1)
            self._log(f"{username} 使用天外来客，科技等级 {self.players[username]['tech_level']}")
            self.refresh_turn_timer()
            self.seq += 1
            return True, f"科技等级 {self.players[username]['tech_level']}"

        # Phase A: unimplemented tricks — allow recast hint
        hand.insert(idx, card)
        if ctype == "trick" or ctype == "equipment":
            return False, "该牌效果尚未实装，可尝试重铸"
        return False, "无法使用该牌"

    def _play_kill(self, username: str, card: dict[str, Any], target: str | None) -> tuple[bool, str]:
        p = self.players[username]
        if p["kills_used_this_turn"] >= 2:
            return False, "本回合出杀已达上限（2）"
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
            self._note_basic_used(username)
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
