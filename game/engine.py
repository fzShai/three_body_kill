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
    equip_id,
    has_ship,
    is_temp_ascend_card,
    resolve_slot,
)
from game.skills import (
    SKILL_COHESION,
    SKILL_LEADER,
    SKILL_NATIVE,
    SKILL_RED_SHORE,
    SKILL_STARSHIP,
    SKILL_SWORD_HOLDER,
    SKILL_WALLFACER,
    SKILL_WANDER,
    STATUS_SKILLS_SEALED,
    skill_active,
)
from game.stats import final_basic_damage, initial_combat_fields
from game.trick_effects import (
    HANDLERS as TRICK_HANDLERS,
    STATUS_BLACK_HOLE,
    STATUS_CRADLE,
    STATUS_DEATH_IMMORTAL,
    STATUS_FLIPPED,
    STATUS_HIBERNATION,
    STATUS_MICRO_UNIVERSE,
    STATUS_PLAN_PART,
    STATUS_TECH_LOCK,
    TARGET_TRICKS,
    clear_negative_statuses,
    discard_from_target,
    field_bonus_damage,
    field_bonus_reduction,
    has_field,
    legal_play as trick_legal_play,
)
from game.turn import hand_limit

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TURN_SECONDS = 25.0
STATUS_LOCKED = "locked"
STATUS_KINDS = ("positive", "negative")

# Fallback status blurbs when card_defs has no matching id
STATUS_TEXT_FALLBACK: dict[str, str] = {
    STATUS_SKILLS_SEALED: "非锁定技失效，直至状态移除（通常至其下个回合结束）。",
    STATUS_TECH_LOCK: "科技等级无法变化。",
    STATUS_CRADLE: "受到致命伤害时可能触发摇篮相关效果。",
    STATUS_HIBERNATION: "冬眠中：不可被选为目标。",
    STATUS_FLIPPED: "翻面：跳过下一回合。",
    STATUS_BLACK_HOLE: "黑洞：相关结算按牌面效果执行。",
    STATUS_DEATH_IMMORTAL: "濒死出局时可触发死神永生相关效果。",
    "locked": "被锁死：跳过回合。",
}
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
    if len(player_names) <= len(pool):
        chosen = random.sample(pool, len(player_names))
    else:
        random.shuffle(pool)
        chosen = [pool[i % len(pool)] for i in range(len(player_names))]
    assigned: dict[str, dict[str, Any]] = {}
    for name, role in zip(player_names, chosen):
        skills = deepcopy(role.get("skills") or [])
        p: dict[str, Any] = {
            "role_id": role["id"],
            "role_name": role["name"],
            "faction": role.get("faction"),
            "skills": skills,
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
            "shield": 0,
            "cards_used_this_turn": 0,
            **initial_combat_fields(),
        }
        if skill_active(p, SKILL_STARSHIP):
            p["tech_level"] = 4
        assigned[name] = p
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
        self.stage: dict[str, Any] = {"kind": "idle", "card": None, "from": None, "to": None, "text": ""}
        self._pending_conclude: str | None = None
        self.fields: list[dict[str, Any]] = []
        self.field_multiplier: int = 1
        self.trisolaris_era: str | None = None
        self._pending_trick: dict[str, Any] | None = None
        self._events: list[dict[str, Any]] = []
        self._deal_initial()
        self.phase = "turn"
        self.turn_phase = "draw"
        self._run_draw_phase()
        self._start_turn_timer()
        self._log(f"对局开始，先手：{self.current_player()}")
        # Opening deal/draw should not animate on first connect
        self._events.clear()

    @classmethod
    def create(cls, room_id: str, player_names: list[str]) -> GameSession:
        return cls(room_id=room_id, player_names=player_names)

    def _log(self, text: str) -> None:
        self.log.append(text)
        if len(self.log) > 100:
            self.log = self.log[-100:]

    def _card_pub(self, card: dict[str, Any] | None) -> dict[str, Any] | None:
        if not card:
            return None
        return {
            "id": card.get("id"),
            "name": card.get("name"),
            "type": card.get("type"),
            "subtype": card.get("subtype"),
            "tier": card.get("tier"),
            "slot": card.get("slot"),
            "instance_id": card.get("instance_id"),
            "text": card.get("text"),
            "heal": card.get("heal"),
        }

    def _emit(self, type_: str, **kwargs: Any) -> None:
        ev: dict[str, Any] = {"type": type_, "seq": self.seq}
        for key, val in kwargs.items():
            if val is not None:
                ev[key] = val
        self._events.append(ev)

    def _events_for_viewer(self, viewer: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in self._events:
            ev = dict(raw)
            if ev.get("type") == "draw" and ev.get("source") != viewer:
                ev["cards"] = None
                ev["hidden"] = True
            out.append(ev)
        return out

    def flush_events(self) -> None:
        self._events.clear()

    def _give_drawn(
        self,
        username: str,
        cards: list[dict[str, Any]] | dict[str, Any],
        *,
        emit: bool = True,
    ) -> list[dict[str, Any]]:
        if isinstance(cards, dict):
            cards = [cards]
        cards = list(cards)
        if not cards:
            return []
        self.players[username]["hand"].extend(cards)
        if emit:
            self._emit(
                "draw",
                source=username,
                count=len(cards),
                cards=[self._card_pub(c) for c in cards],
                instance_ids=[c.get("instance_id") for c in cards],
            )
        return cards

    def _emit_play(self, username: str, card: dict[str, Any], target: str | None = None) -> None:
        self._emit(
            "play",
            source=username,
            target=target,
            card=self._card_pub(card),
            instance_id=card.get("instance_id"),
        )

    def _emit_discard(self, username: str, card: dict[str, Any]) -> None:
        self._emit(
            "discard",
            source=username,
            card=self._card_pub(card),
            instance_id=card.get("instance_id"),
        )

    def _set_stage(
        self,
        kind: str,
        *,
        card: dict[str, Any] | None = None,
        from_name: str | None = None,
        to_name: str | None = None,
        text: str = "",
    ) -> None:
        card_view = None
        if card:
            card_view = {
                "name": card.get("name"),
                "id": card.get("id"),
                "type": card.get("type"),
                "subtype": card.get("subtype"),
                "tier": card.get("tier"),
            }
        self.stage = {
            "kind": kind,
            "card": card_view,
            "from": from_name,
            "to": to_name,
            "text": text,
        }

    def _compute_stage(self) -> dict[str, Any]:
        """Live stage for UI: prefer prompt/dying over last play."""
        if self.phase == "ended":
            text = f"对局结束 · 胜者 {self.winner}" if self.winner else "对局结束"
            return {"kind": "ended", "card": None, "from": None, "to": self.winner, "text": text}
        if self.phase == "dying" and self.dying:
            victim = self.dying.get("victim")
            return {
                "kind": "dying",
                "card": {"name": "桃", "subtype": "heal"},
                "from": self.dying.get("source"),
                "to": victim,
                "text": f"{victim} 濒死 · 求桃",
            }
        if self.phase == "prompt" and self.prompt:
            p = self.prompt
            ptype = p.get("type")
            card_name = p.get("card_name")
            card = {"name": card_name} if card_name else None
            if ptype == "respond_dodge":
                return {
                    "kind": "kill",
                    "card": card or {"name": "杀", "subtype": "kill"},
                    "from": p.get("from"),
                    "to": p.get("to"),
                    "text": f"{p.get('from')} 对 {p.get('to')} 使用{card_name or '杀'}",
                }
            if ptype == "interrupt_trick":
                return {
                    "kind": "trick",
                    "card": card or {"name": "锦囊"},
                    "from": p.get("from"),
                    "to": p.get("to"),
                    "text": f"{p.get('from')} 打出{card_name or '锦囊'} · 可打断",
                }
            if ptype == "respond_toxic":
                return {
                    "kind": "trick",
                    "card": card or {"name": "剧毒之水"},
                    "from": p.get("from"),
                    "to": p.get("to"),
                    "text": f"{p.get('from')} 对 {p.get('to')} 使用剧毒之水",
                }
            if ptype == "choice":
                labels = " / ".join(o.get("label", "") for o in (p.get("options") or []))
                return {
                    "kind": "choice",
                    "card": card,
                    "from": p.get("from"),
                    "to": p.get("to"),
                    "text": f"{p.get('to')} 请选择：{labels}",
                }
            if ptype == "wander_draw":
                return {
                    "kind": "skill",
                    "card": None,
                    "from": p.get("to"),
                    "to": p.get("to"),
                    "text": f"{p.get('to')} 【流浪】：是否失去 1 体力并摸两张？",
                }
            if ptype == "gravity_override":
                return {
                    "kind": "skill",
                    "card": None,
                    "from": p.get("to"),
                    "to": None,
                    "text": f"{p.get('to')} 万有引力号：弃两张使杀仍生效？",
                }
            return {
                "kind": "prompt",
                "card": card,
                "from": p.get("from"),
                "to": p.get("to"),
                "text": str(ptype or "响应"),
            }
        if self.phase == "turn" and self.turn_phase == "discard":
            who = self.current_player()
            return {
                "kind": "discard",
                "card": None,
                "from": who,
                "to": None,
                "text": f"{who} 弃牌阶段",
            }
        return dict(self.stage) if self.stage else {
            "kind": "idle",
            "card": None,
            "from": None,
            "to": None,
            "text": "",
        }

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
            self._give_drawn(name, cards, emit=False)
            self._log(f"{name} 开局摸 6 张")

    def _run_draw_phase(self) -> None:
        name = self.current_player()
        p = self.players[name]
        n = 3
        if skill_active(p, SKILL_LEADER):
            n = 4
        if p.get("ascension") == "psychic":
            n += 1
        n += int(p.get("extra_draw", 0))
        drawn = self.draw_sys.draw_n(p["tech_level"], n)
        self._give_drawn(name, drawn)
        self.turn_phase = "play"
        p["kills_used_this_turn"] = 0
        p["red_coast_used"] = False
        p["cards_used_this_turn"] = 0
        if p.get("ultimate_law_used") is not None:
            p["ultimate_law_used"] = False
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
            self._give_drawn(name, kill)
            self._log(f"{name} 量子号：获得一张三阶杀")
        self._log(f"{name} 摸牌阶段摸了 {len(drawn)} 张")

    def _clear_vision_if_due(self, username: str) -> None:
        p = self.players[username]
        if p.get("vision_clear_at_turn_end") and p.get("vision_exposed"):
            p["vision_exposed"] = False
            p["vision_clear_at_turn_end"] = False
            self._log(f"{username} 的视野暴露结束")

    def expire_turn_if_due(self) -> bool:
        if time.time() < self.turn_deadline_at:
            return False
        if self.phase == "prompt" and self.prompt:
            ptype = self.prompt.get("type")
            if ptype == "wander_draw":
                self._log(f"{self.prompt.get('to')} 【流浪】超时，视为放弃")
                self._apply_wander(str(self.prompt.get("to")), False)
                self.seq += 1
                return True
            if ptype == "gravity_override":
                self._log(f"{self.prompt.get('to')} 万有引力号超时，视为放弃")
                self.apply_action(str(self.prompt.get("to")), {"action": "gravity_pass"})
                return True
            ptype = self.prompt.get("type")
            if ptype == "choice":
                self._log(f"{self.prompt.get('to')} 选择超时，自动第一项")
                opts = self.prompt.get("options") or []
                if opts:
                    self.apply_action(str(self.prompt.get("to")), {"action": "choose", "choice": opts[0]["id"]})
                else:
                    self.prompt = None
                    self.phase = "turn"
                self.seq += 1
                return True
            if ptype == "soap_heal":
                to = str(self.prompt.get("to"))
                targets = list(self.prompt.get("targets") or [])
                pick = targets[0] if targets else to
                self._log(f"{to} 香皂超时，自动给 {pick} +1")
                self.apply_action(to, {"action": "choose", "target": pick})
                return True
            if ptype in {"interrupt_trick", "respond_toxic"}:
                self._log(f"{self.prompt.get('to')} 打断超时，视为不响应")
                self._resolve_interrupt_or_toxic()
                self.seq += 1
                return True
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
        self._conclude_turn(name)
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
        self._conclude_turn(username)
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
                self.winner_faction = None
                self._log(f"{self.winner} 获胜")
            else:
                self.winner = None
                self._log("无人存活，平局")
            return True
        return False

    def _auto_discard(self, username: str) -> None:
        p = self.players[username]
        limit = hand_limit(p["max_hp"])
        while len(p["hand"]) > limit:
            card = p["hand"].pop()
            self.discard.append(card)
            self._emit_discard(username, card)
            self._log(f"{username} 弃置 {card.get('name')}")

    # --- status helpers (kept for compatibility) ---
    def _has_status(self, username: str, status_id: str) -> bool:
        return any(s.get("id") == status_id for s in self.players[username]["statuses"])

    def _apply_status(self, username: str, status_id: str, name: str, kind: str) -> bool:
        if kind not in STATUS_KINDS or self._has_status(username, status_id):
            return False
        self.players[username]["statuses"].append({"id": status_id, "name": name, "kind": kind})
        self._emit("status", target=username, status_id=status_id, name=name, kind=kind)
        return True

    def _remove_status(self, username: str, status_id: str) -> bool:
        statuses = self.players[username]["statuses"]
        for i, s in enumerate(statuses):
            if s.get("id") == status_id:
                statuses.pop(i)
                return True
        return False

    def _raise_tech(self, username: str, by: int = 1) -> None:
        self._set_tech(username, self.players[username]["tech_level"] + by)

    def _set_tech(self, username: str, level: int, *, notify: bool = True, force: bool = False) -> bool:
        p = self.players[username]
        if not force and (self._has_status(username, STATUS_TECH_LOCK) or p.get("cold_silence")):
            self._log(f"{username} 科技被锁定，无法变化")
            return False
        before = int(p["tech_level"])
        p["tech_level"] = max(1, min(6, int(level)))
        after = int(p["tech_level"])
        if before == after:
            return False
        if before < 6 <= after and not p.get("ascended"):
            self._grant_ascension(username)
        if notify:
            self._on_tech_changed(username, before, after)
        return True

    def _on_tech_changed(self, username: str, before: int, after: int) -> None:
        p = self.players[username]
        if not p.get("alive"):
            return
        if not skill_active(p, SKILL_WANDER):
            return
        # Do not nest wander over kill response; queue after current prompt if needed
        if self.phase == "prompt" and self.prompt and self.prompt.get("type") != "wander_draw":
            self.prompt["queue_wander"] = username
            return
        if self.phase == "dying":
            return
        self._open_wander_prompt(username)

    def _open_wander_prompt(self, username: str, *, after: str | None = None) -> None:
        self.prompt = {
            "type": "wander_draw",
            "to": username,
            "from": username,
            "after": after,
            "confirm": {
                "accept_label": "发动流浪",
                "pass_label": "放弃流浪",
                "accept_action": "wander_accept",
                "pass_action": "wander_pass",
                "needs_cards": 0,
            },
        }
        self.phase = "prompt"
        self._log(f"{username} 【流浪】：是否失去 1 点体力并摸两张牌？")
        self._start_turn_timer()

    def _apply_wander(self, username: str, accept: bool) -> None:
        after = (self.prompt or {}).get("after")
        native_after = (self.prompt or {}).get("native_after")
        self.prompt = None
        if accept and self.players[username]["alive"]:
            p = self.players[username]
            p["hp"] -= 1
            self._emit("damage", source=username, target=username, value=1, reason="wander")
            drawn = self.draw_sys.draw_n(p["tech_level"], 2)
            self._give_drawn(username, drawn)
            self._log(f"{username} 发动【流浪】：失去 1 体力，摸 {len(drawn)} 张（HP {p['hp']}）")
            if p["hp"] <= 0:
                if after == "conclude_turn":
                    self._pending_conclude = username
                self._begin_dying(username)
                return
        else:
            self._log(f"{username} 放弃【流浪】")
        if native_after and native_after.get("kind") == "visitor":
            who = str(native_after.get("from") or username)
            if who in self.players and self.players[who].get("alive"):
                fake = {"id": "visitor", "subtype": "visitor", "pool_entry": 15}
                self._maybe_native_repeat_instant(who, fake, target=None, kind="visitor")
        if after == "conclude_turn":
            self._finish_conclude_turn(username)
            return
        if self.phase not in {"dying", "ended"}:
            self.phase = "turn"
            self.refresh_turn_timer()

    def _end_play_phase(self, username: str) -> tuple[bool, str]:
        """Leave play phase: skip discard when hand is within limit."""
        limit = hand_limit(self.players[username]["max_hp"])
        if len(self.players[username]["hand"]) <= limit:
            self._log(f"{username} 无需弃牌，跳过弃牌阶段")
            self._conclude_turn(username)
            self._check_win()
            self.seq += 1
            return True, "无需弃牌，回合结束"
        self.turn_phase = "discard"
        self._set_stage(
            "discard",
            from_name=username,
            text=f"{username} 弃牌阶段",
        )
        self._log(f"{username} 进入弃牌阶段")
        self.refresh_turn_timer()
        self.seq += 1
        return True, "进入弃牌阶段"

    def _conclude_turn(self, username: str) -> None:
        """End-of-turn skills then advance. May pause for wander."""
        p = self.players[username]
        if p.get("alive") and skill_active(p, SKILL_STARSHIP):
            before = p["tech_level"]
            if before > 1:
                self._set_tech(username, before - 1)
                self._log(f"{username} 【星舰】：科技降至 {p['tech_level']}")
        if self.phase == "prompt" and self.prompt and self.prompt.get("type") == "wander_draw":
            self.prompt["after"] = "conclude_turn"
            return
        self._finish_conclude_turn(username)

    def _finish_conclude_turn(self, username: str) -> None:
        if self._has_status(username, STATUS_SKILLS_SEALED):
            self._remove_status(username, STATUS_SKILLS_SEALED)
            self._log(f"{username} 的非锁定技封印结束")
        p = self.players[username]
        if p.get("tech_lock_clear_at_turn_end"):
            self._remove_status(username, STATUS_TECH_LOCK)
            p["tech_lock_clear_at_turn_end"] = False
            self._log(f"{username} 的科技锁定结束")
        if has_field(self, "crisis_field") and p.get("alive") and not p.get("finale_field_immune"):
            import random

            alive = [n for n in self.player_order if self.players[n]["alive"]]
            if alive:
                victim = random.choice(alive)
                dmg = random.randint(0, 3) * max(1, int(self.field_multiplier or 1))
                if dmg > 0:
                    self._log(f"危机场地：对 {victim} 造成 {dmg} 点最终伤害")
                    self._deal_damage(username, victim, dmg)
                else:
                    self._log(f"危机场地：判定 0，无伤害")
        if p.get("finale_death_pending") and p.get("alive"):
            p["finale_death_pending"] = False
            self._log(f"{username} 终末到期，出局")
            self._eliminate_player(username)
            if self._check_win():
                return
        if self.phase == "ended":
            return
        self._advance_turn()

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
            if self._has_status(nxt, STATUS_FLIPPED):
                self._remove_status(nxt, STATUS_FLIPPED)
                self._log(f"{nxt} 翻面，跳过回合")
                continue
            # clear hibernation at turn start
            if self.players[nxt].get("hibernation_clear_at_turn_start"):
                self._remove_status(nxt, STATUS_HIBERNATION)
                self.players[nxt]["hibernation_clear_at_turn_start"] = False
                self._log(f"{nxt} 冬眠结束")
            if has_field(self, "trisolaris_field") and self.players[nxt]["alive"]:
                import random

                roll = random.randint(1, 4)
                if roll == 1:
                    prev = self.trisolaris_era or "stable"
                    self.trisolaris_era = "chaos" if prev == "stable" else "stable"
                    label = "恒纪元" if self.trisolaris_era == "stable" else "乱纪元"
                    self._log(f"三体判定 {roll}：切换至{label}")
                else:
                    self._log(f"三体判定 {roll}：纪元不变")
            if self.trisolaris_era == "chaos" and self.players[nxt]["alive"]:
                self.players[nxt]["chaos_cards_used"] = 0
            self.phase = "turn"
            self.turn_phase = "draw"
            self._run_draw_phase()
            if self.trisolaris_era == "stable":
                extra = self.draw_sys.draw_n(self.players[nxt]["tech_level"], 1)
                self._give_drawn(nxt, extra)
                self._log(f"恒纪元：{nxt} 额外摸 1 张")
            self._start_turn_timer()
            self._log(f"轮到 {nxt}")
            return
        self.phase = "ended"

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
        self._emit(
            "ascension",
            target=username,
            ascension=choice,
            name=labels[choice],
            permanent=True,
        )
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

    def _eliminate_player(self, username: str, *, killer: str | None = None) -> None:
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
        self._emit("die", target=username, source=killer)
        self._trigger_red_shore(username, killer)

    def _trigger_red_shore(self, victim: str, killer: str | None) -> None:
        for name in self.player_order:
            p = self.players[name]
            if not p.get("alive") or not skill_active(p, SKILL_RED_SHORE):
                continue
            drawn = self.draw_sys.draw_n(p["tech_level"], 1)
            self._give_drawn(name, drawn)
            self._heal(name, 1)
            self._log(f"{name} 【红岸】：因 {victim} 死亡摸1回1")
            if killer and killer == name and killer != victim:
                drawn2 = self.draw_sys.draw_n(p["tech_level"], 1)
                self._give_drawn(name, drawn2)
                self._heal(name, 1)
                self._log(f"{name} 【红岸】击杀加成：再摸1回1")

    def _incoming_damage(self, target: str, amount: int, *, true_dmg: bool = False) -> int:
        """Apply armor / equipment modifiers to incoming final damage."""
        t = self.players[target]
        dmg = max(0, int(amount))
        if true_dmg:
            return dmg
        red = field_bonus_reduction(self) * max(1, int(self.field_multiplier or 1))
        dmg = max(0, dmg - red)
        if t.get("deep_sea") and not t.get("vision_exposed"):
            dmg = max(0, dmg - 1)
        if t.get("eco_bottle") and dmg > 3:
            dmg = 3
        if t.get("lightspeed_stacks") is not None:
            stacks = int(t.get("lightspeed_stacks", 0))
            red2 = min(3, stacks)
            dmg = max(0, dmg - red2)
            t["lightspeed_stacks"] = min(3, stacks + 1)
            t["lightspeed_reduction"] = min(3, stacks + 1)
        charges = int(t.get("plan_part_charges") or 0)
        if charges > 0 and self._has_status(target, STATUS_PLAN_PART) and dmg > 0:
            dmg = dmg // 2
            t["plan_part_charges"] = charges - 1
            t["plan_part_pending"] = True
            self._log(f"{target} 计划的一部分：伤害减半（剩余{t['plan_part_charges']}次）")
        shield = int(t.get("shield") or 0)
        if shield > 0 and dmg > 0:
            absorb = min(shield, dmg)
            t["shield"] = shield - absorb
            dmg -= absorb
            if t.get("micro_universe_shield") is not None:
                t["micro_universe_shield"] = max(0, int(t["micro_universe_shield"]) - absorb)
                if int(t.get("micro_universe_shield") or 0) <= 0 and self._has_status(target, STATUS_MICRO_UNIVERSE):
                    self._remove_status(target, STATUS_MICRO_UNIVERSE)
                    t.pop("micro_universe_shield", None)
                    self._log(f"{target} 小宇宙护盾耗尽，状态移除")
            self._log(f"{target} 护盾吸收 {absorb}（剩余护盾 {t['shield']}）")
        return dmg

    def _deal_damage(
        self,
        source: str,
        target: str,
        final: int,
        *,
        from_trick: bool = False,
        from_kill: bool = False,
    ) -> str:
        t = self.players[target]

        # 量子幽灵：8 血嘲讽替身承担伤害（伤害+1）
        ghost_hp = int(t.get("quantum_ghost_hp") or 0)
        if ghost_hp > 0 and final > 0 and source != target:
            ghost_dmg = int(final) + 1
            t["quantum_ghost_hp"] = max(0, ghost_hp - ghost_dmg)
            self._emit("damage", source=source, target=target, value=ghost_dmg, reason="quantum_ghost")
            self._log(f"{target} 量子幽灵替身承受 {ghost_dmg}（剩余 {t['quantum_ghost_hp']}）")
            if t["quantum_ghost_hp"] <= 0:
                self._unequip_slot(target, "armor", to_discard=True)
                self._log(f"{target} 量子幽灵替身消散，装备移出")
            return f"{target} 的量子幽灵替身承受了伤害"

        final = self._incoming_damage(target, final, true_dmg=False)
        t["hp"] -= final
        if final > 0:
            self._emit("damage", source=source, target=target, value=final)
        msg = f"{target} 受到 {final} 点最终伤害（HP {t['hp']}）"
        src = self.players.get(source)
        if src and src.get("swordholder_ready") and final > 0 and source != target:
            self._heal(source, final)
            src["swordholder_ready"] = False
            self._log(f"{source} 执剑：回复 {final} 点")
        if (
            final > 0
            and source
            and source != target
            and self._has_status(target, STATUS_CRADLE)
            and self.players.get(source, {}).get("alive")
        ):
            reflect = min(3, final)
            self._remove_status(target, STATUS_CRADLE)
            s = self.players[source]
            s["hp"] -= reflect
            if reflect > 0:
                self._emit("damage", source=target, target=source, value=reflect, reason="cradle")
            self._log(f"{target} 摇篮反弹 {reflect} 点给 {source}（HP {s['hp']}）")
            if s["hp"] <= 0:
                self._begin_dying(source, source=target)
        if (
            final >= 2
            and source
            and source != target
            and skill_active(t, SKILL_WALLFACER)
            and not t.get("vision_exposed")
            and src
            and not src.get("vision_exposed")
            and src.get("alive")
        ):
            src["hp"] -= 1
            self._emit("damage", source=target, target=source, value=1, reason="wallfacer")
            self._log(f"{target} 【面壁者】：{source} 扣 1 点体力（HP {src['hp']}）")
            if src["hp"] <= 0:
                self._begin_dying(source, source=target)
        if from_trick and final > 0 and t.get("star_ring") and t.get("alive"):
            self._unequip_slot(target, "ship", to_discard=True)
            self._log(f"{target} 星环号：受锦囊伤害后失去舰船")
        # 计划的一部分：受伤后二选一
        if final > 0 and t.get("plan_part_pending") and t.get("alive"):
            t["plan_part_pending"] = False
            self._open_plan_part_choice(target, source)
        if t["hp"] <= 0:
            msg += "，" + self._begin_dying(target, source=source)
        return msg

    def _heal(self, username: str, amount: int) -> None:
        p = self.players[username]
        before = int(p["hp"])
        bonus = 1 if p.get("deep_sea") and not p.get("vision_exposed") else 0
        p["hp"] = min(p["max_hp"], p["hp"] + amount + bonus)
        gained = int(p["hp"]) - before
        if gained > 0:
            self._emit("heal", target=username, value=gained)

    def _begin_dying(self, victim: str, *, source: str | None = None) -> str:
        self.phase = "dying"
        self.dying = {"victim": victim, "source": source}
        self.prompt = None
        self._set_stage(
            "dying",
            card={"name": "桃", "subtype": "heal"},
            from_name=source,
            to_name=victim,
            text=f"{victim} 濒死 · 求桃",
        )
        self._emit("dying", target=victim, source=source)
        self._log(f"{victim} 进入濒死")
        self._start_turn_timer()
        return f"{victim} 濒死"

    def _force_peach_or_die(self, victim: str) -> None:
        p = self.players[victim]
        peach_idx = next((i for i, c in enumerate(p["hand"]) if c.get("subtype") == "heal" or c.get("id") == "peach"), None)
        if peach_idx is not None:
            card = p["hand"].pop(peach_idx)
            heal = int(card.get("heal", 2))
            before = int(p["hp"])
            p["hp"] = min(p["max_hp"], max(1, p["hp"] + heal))
            gained = int(p["hp"]) - before
            self.discard.append(card)
            self._emit_play(victim, card, victim)
            if gained > 0:
                self._emit("heal", target=victim, value=gained)
            self._log(f"{victim} 濒死强制使用 {card.get('name')}，HP {p['hp']}")
            self.dying = None
            self.phase = "turn"
            self.refresh_turn_timer()
            if self._pending_conclude:
                who = self._pending_conclude
                self._pending_conclude = None
                if self.phase not in {"ended"} and self.players.get(who, {}).get("alive") is not False:
                    self._finish_conclude_turn(who)
            return
        # 死神永生：即将出局时回2摸2并移除状态
        p = self.players[victim]
        if p.get("death_immortal") or self._has_status(victim, STATUS_DEATH_IMMORTAL):
            self._remove_status(victim, STATUS_DEATH_IMMORTAL)
            p["death_immortal"] = False
            before = int(p["hp"])
            p["hp"] = min(p["max_hp"], max(1, p["hp"] + 2))
            gained = int(p["hp"]) - before
            if gained > 0:
                self._emit("heal", target=victim, value=gained)
            drawn = self.draw_sys.draw_n(p["tech_level"], 2)
            self._give_drawn(victim, drawn)
            self.dying = None
            self.phase = "turn"
            self._log(f"{victim} 死神永生：回2摸{len(drawn)}并移除状态（HP {p['hp']}）")
            self.refresh_turn_timer()
            if self._pending_conclude:
                who = self._pending_conclude
                self._pending_conclude = None
                if self.phase not in {"ended"} and self.players.get(who, {}).get("alive") is not False:
                    self._finish_conclude_turn(who)
            return
        killer = (self.dying or {}).get("source")
        self._eliminate_player(victim, killer=killer if isinstance(killer, str) else None)
        self.dying = None
        self._log(f"{victim} 濒死无回复牌，出局")
        pending = self._pending_conclude
        self._pending_conclude = None
        if not self._check_win():
            self.phase = "turn"
            self.refresh_turn_timer()
            if pending:
                self._finish_conclude_turn(pending)

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
            return self._end_play_phase(username)

        if act == "discard_done" or act == "pass":
            if self.turn_phase == "play":
                return self._end_play_phase(username)
            if self.turn_phase != "discard":
                return False, "现在不是弃牌阶段"
            limit = hand_limit(self.players[username]["max_hp"])
            over = len(self.players[username]["hand"]) - limit
            if over > 0:
                return False, f"还需弃置 {over} 张牌"
            self._conclude_turn(username)
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
            self._emit_discard(username, card)
            self._log(f"{username} 弃置 {card.get('name')}")
            self.refresh_turn_timer()
            self.seq += 1
            return True, "已弃置"

        if act == "discard_for_tech":
            return self._discard_for_tech(username, action)

        if act == "recast":
            return self._recast(username, str(action.get("instance_id", "")).strip())

        if act == "ultimate_law":
            return self._use_ultimate_law(username, action)

        if act == "play_card":
            return self._play_card(username, action)

        if act == "play_placeholder":
            # legacy no-op path: treat as end play
            return self.apply_action(username, {"action": "end_play"})

        return False, f"未知行动: {act}"

    def _apply_dying_action(self, username: str, action: dict[str, Any]) -> tuple[bool, str]:
        if not self.dying:
            return False, "当前不在濒死阶段"
        victim = self.dying.get("victim")
        if victim not in self.players:
            return False, "濒死目标无效"

        act = str(action.get("action", "")).strip()

        # Timeout / resolve: only victim (or anyone via dying_resolve for auto path)
        # Others must not force-resolve death for the victim.
        if act in {"dying_resolve", "dying_pass"}:
            if username != victim:
                return False, "仅濒死者可结算濒死"
            self._force_peach_or_die(victim)
            self.seq += 1
            return True, "濒死已结算"

        if act == "play_card":
            if not self.players[username]["alive"] and username != victim:
                return False, "你已被淘汰"
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
            v = self.players[victim]
            before = int(v["hp"])
            v["hp"] = min(v["max_hp"], max(1, v["hp"] + heal))
            gained = int(v["hp"]) - before
            self.discard.append(card)
            self._emit_play(username, card, victim)
            if gained > 0:
                self._emit("heal", target=victim, value=gained)
            self.dying = None
            self.phase = "turn"
            if username == victim:
                self._log(f"{username} 濒死使用 {card.get('name')}，HP {v['hp']}")
            else:
                self._log(f"{username} 对 {victim} 使用 {card.get('name')} 救人，HP {v['hp']}")
            self.refresh_turn_timer()
            self.seq += 1
            if self._pending_conclude:
                who = self._pending_conclude
                self._pending_conclude = None
                self._finish_conclude_turn(who)
            return True, "脱离濒死"

        return False, "濒死阶段行动无效"

    def _alive_others(self, username: str) -> list[str]:
        return [n for n in self.player_order if n != username and self.players[n]["alive"]]

    def _unexposed_others(self, username: str) -> list[str]:
        return [
            n
            for n in self._alive_others(username)
            if not self.players[n].get("vision_exposed")
        ]

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
        for card in cards:
            self._emit_discard(username, card)
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
        self._emit(
            "ascension",
            target=username,
            ascension=cid,
            name=name,
            permanent=False,
            card=self._card_pub(card),
        )
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
                "gravity", "star_ring", "ultimate_law",
                "nano_center", "chip_workshop", "stars_plan",
                "deep_sea", "eco_bottle", "lightspeed_2", "curvature", "solar_observe",
                "quantum_ghost",
            }
            return str(cid) in known
        return False

    @staticmethod
    def _triggers_native(card: dict[str, Any]) -> bool:
        """土著：科技池 1 阶牌（pool_entry 1–22）；无 pool_entry 时回退 tier/桃/天外来客。"""
        entry = card.get("pool_entry")
        if entry is not None:
            try:
                return 1 <= int(entry) <= 22
            except (TypeError, ValueError):
                pass
        if int(card.get("tier", 0) or 0) == 1:
            return True
        subtype = card.get("subtype")
        cid = card.get("id")
        if subtype == "heal" or cid == "peach":
            return True
        if subtype == "visitor" or cid == "visitor":
            return True
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
            # 满级无合法打法 → 可重铸；未满级时凝聚仍可破例重铸
            return int(self.players[username].get("tech_level", 1)) < 6
        if cid == "ball_lightning":
            return self._card_implemented(card) and bool(self._alive_others(username))
        if cid == "ladder_plan" or cid == "red_coast":
            if not self._card_implemented(card):
                return False
            if cid == "ladder_plan":
                return bool(self._unexposed_others(username))
            return not bool(self.players[username].get("red_coast_used"))
        if cid in TRICK_HANDLERS:
            return trick_legal_play(self, username, card)
        if is_temp_ascend_card(card):
            if not self._card_implemented(card):
                return False
            return not self._has_status(username, str(cid or ""))
        if ctype == "equipment" or resolve_slot(card):
            if not self._card_implemented(card):
                return False
            slot = resolve_slot(card)
            if not slot:
                return False
            # 槽位已有装备时仍可打出替换，但不算「合法打法」，允许重铸
            return self.players[username]["equipment"].get(slot) is None
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
            self._give_drawn(username, drawn)
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
            self._give_drawn(username, drawn)
            self._log(f"{username} 唐号入场：摸 {len(drawn)} 张")
        label = SLOT_LABELS.get(slot, slot)
        note = f"（{', '.join(notes)}）" if notes else ""
        self._set_stage(
            "equip",
            card=card,
            from_name=username,
            to_name=username,
            text=f"{username} 装备 {card.get('name')}",
        )
        self._emit(
            "equip",
            source=username,
            target=username,
            card=self._card_pub(card),
            slot=slot,
        )
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
        can_recast_visitor = (
            (card.get("subtype") == "visitor" or card.get("id") == "visitor")
            and skill_active(self.players[username], SKILL_COHESION)
        )
        can_recast_hibernation = card.get("id") == "hibernation"
        if self._card_has_legal_play(username, card) and not can_recast_visitor and not can_recast_hibernation:
            return False, "该牌有合法打法，不能重铸"
        hand.pop(idx)
        self.discard.append(card)
        self._emit_discard(username, card)
        drawn = self.draw_sys.draw_one(self.players[username]["tech_level"])
        self._give_drawn(username, drawn)
        self._log(f"{username} 重铸了 {card.get('name')}")
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
            extra_target = str(action.get("extra_target", "")).strip() or None
            if self.players[username].get("deterrence_extra_target") and extra_target:
                self._pending_deterrence_target = extra_target
            ok, msg = self._play_kill(username, card, target)
            if not ok:
                hand.insert(idx, card)
                self._pending_deterrence_target = None
                return False, msg
            self._emit_play(username, card, target)
            self._on_card_used(username)
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
            if self.players[username].get("deterrence_extra_target"):
                # 基本牌目标+1：桃通常仅自己，额外目标可指定他人回复1（简化：给 extra_target +1）
                extra = str(action.get("extra_target", "")).strip() or None
                self.players[username]["deterrence_extra_target"] = False
                if extra and extra in self.players and self.players[extra]["alive"]:
                    self._heal(extra, 1)
                    self._log(f"{username} 威慑：额外令 {extra} 回复1")
            heal = int(card.get("heal", 2))
            self._heal(username, heal)
            self.discard.append(card)
            self._emit_play(username, card, username)
            self._set_stage(
                "heal",
                card=card,
                from_name=username,
                to_name=username,
                text=f"{username} 使用桃",
            )
            self._on_basic_played(username, card)
            self._on_card_used(username)
            self._log(f"{username} 使用桃，HP {self.players[username]['hp']}")
            self._maybe_native_repeat_instant(username, card, target=username, kind="peach", heal=heal)
            self.refresh_turn_timer()
            self.seq += 1
            return True, f"回复至 {self.players[username]['hp']} HP"

        if subtype == "visitor" or cid == "visitor":
            if int(self.players[username].get("tech_level", 1)) >= 6:
                hand.insert(idx, card)
                return False, "科技已满级，可将本牌重铸"
            if self.players[username].get("deterrence_extra_target"):
                self.players[username]["deterrence_extra_target"] = False
                self._log(f"{username} 威慑在天外来客上消耗（无额外目标）")
            self.discard.append(card)
            self._emit_play(username, card)
            self._set_stage(
                "basic",
                card=card,
                from_name=username,
                to_name=None,
                text=f"{username} 使用天外来客",
            )
            self._on_basic_played(username, card)
            self._on_card_used(username)
            self._raise_tech(username, 1)
            self._log(f"{username} 使用天外来客，科技等级 {self.players[username]['tech_level']}")
            if self.phase == "prompt" and self.prompt and self.prompt.get("type") == "wander_draw":
                # wander will resume turn; native repeat after wander if needed
                self.prompt["native_after"] = {"kind": "visitor", "from": username}
            else:
                self._maybe_native_repeat_instant(username, card, target=None, kind="visitor")
            self.refresh_turn_timer()
            self.seq += 1
            return True, f"科技等级 {self.players[username]['tech_level']}"

        if cid == "ladder_plan":
            if self._needs_trick_interrupt(username, card):
                self._emit_play(username, card, target)
                self._open_trick_interrupt(username, card, target, action)
                self.refresh_turn_timer()
                self.seq += 1
                return True, "等待打断响应"
            ok, msg = self._play_ladder_plan(username, card, target)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self._emit_play(username, card, target)
            self._on_card_used(username)
            self._maybe_native_repeat_instant(username, card, target=target, kind="ladder")
            self.refresh_turn_timer()
            self.seq += 1
            return True, msg

        if cid == "ball_lightning":
            if self._needs_trick_interrupt(username, card):
                self._emit_play(username, card, target)
                self._open_trick_interrupt(username, card, target, action)
                self.refresh_turn_timer()
                self.seq += 1
                return True, "等待打断响应"
            ok, msg = self._play_ball_lightning(username, card, target)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self._emit_play(username, card, target)
            self._on_card_used(username)
            self._maybe_native_repeat_instant(username, card, target=target, kind="ball_lightning")
            self.refresh_turn_timer()
            self.seq += 1
            return True, msg

        if cid in TRICK_HANDLERS:
            if cid in {"thought_stamp", "return_motion"}:
                hand.insert(idx, card)
                return False, "该牌只能在响应窗口打出"
            if self._needs_trick_interrupt(username, card):
                # keep card out; store pending
                self._emit_play(username, card, target)
                self._open_trick_interrupt(username, card, target, action)
                self.refresh_turn_timer()
                self.seq += 1
                return True, "等待打断响应"
            if cid == "toxic_water":
                card = {**card, "allow_response": True}
            ok, msg = TRICK_HANDLERS[cid](self, username, card, target, action)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self._emit_play(username, card, target)
            self._on_card_used(username)
            self._after_trick_settled(username, card, target, action)
            self.refresh_turn_timer()
            self.seq += 1
            return True, msg

        if is_temp_ascend_card(card):
            # 纳米等：卡面「可被思想钢印响应」
            if self._needs_trick_interrupt(username, card):
                self._emit_play(username, card, target)
                self._open_trick_interrupt(username, card, target, action)
                self.refresh_turn_timer()
                self.seq += 1
                return True, "等待打断响应"
            ok, msg = self._apply_temp_ascend(username, card)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self._emit_play(username, card, username)
            self._on_card_used(username)
            self.refresh_turn_timer()
            self.seq += 1
            return True, msg

        if ctype == "equipment" or resolve_slot(card):
            p = self.players[username]
            tech_before = p["tech_level"]
            ok, msg = self._equip_card(username, card)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self._emit_play(username, card, username)
            if p["tech_level"] != tech_before:
                self._on_tech_changed(username, tech_before, p["tech_level"])
            self._on_card_used(username)
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
        if self.players[target].get("vision_exposed"):
            return False, "目标视野已暴露"
        t = self.players[target]
        t["vision_exposed"] = True
        t["vision_clear_at_turn_end"] = True
        self.discard.append(card)
        self._log(f"{username} 对 {target} 使用阶梯计划：视野暴露至其回合结束")
        return True, f"{target} 视野已暴露"

    def _play_ball_lightning(self, username: str, card: dict[str, Any], target: str | None) -> tuple[bool, str]:
        if not target or target not in self.players:
            return False, "球状闪电需要指定目标"
        if not self.players[target]["alive"]:
            return False, "目标已淘汰"
        self.discard.append(card)
        if self._has_status(target, STATUS_SKILLS_SEALED):
            self._log(f"{username} 对 {target} 使用球状闪电（封印已存在）")
        else:
            self._apply_status(target, STATUS_SKILLS_SEALED, "非锁定技失效", "negative")
            self._log(f"{username} 对 {target} 使用球状闪电：非锁定技失效至其下回合结束")
        return True, f"{target} 非锁定技已封印"

    def _maybe_native_repeat_instant(
        self,
        username: str,
        card: dict[str, Any],
        *,
        target: str | None,
        kind: str,
        heal: int = 2,
        choice: str | None = None,
        is_repeat: bool = False,
    ) -> None:
        if is_repeat:
            return
        if not self._triggers_native(card):
            return
        if not skill_active(self.players[username], SKILL_NATIVE):
            return
        if kind == "peach":
            self._heal(username, heal)
            self._log(f"{username} 【土著】：桃效果再结算一次，HP {self.players[username]['hp']}")
        elif kind == "visitor":
            self._raise_tech(username, 1)
            self._log(f"{username} 【土著】：天外来客效果再结算一次，科技 {self.players[username]['tech_level']}")
        elif kind == "ladder" and target:
            if target in self.players and self.players[target]["alive"] and not self.players[target].get("vision_exposed"):
                t = self.players[target]
                t["vision_exposed"] = True
                t["vision_clear_at_turn_end"] = True
                self._log(f"{username} 【土著】：阶梯计划再结算一次，{target} 视野暴露")
        elif kind == "red_coast":
            p = self.players[username]
            drawn = self.draw_sys.draw_n(p["tech_level"], 2)
            self._give_drawn(username, drawn)
            self._log(f"{username} 【土著】：红岸计划再结算一次，摸 {len(drawn)} 张")
        elif kind == "ball_lightning" and target:
            if target in self.players and self.players[target]["alive"]:
                if not self._has_status(target, STATUS_SKILLS_SEALED):
                    self._apply_status(target, STATUS_SKILLS_SEALED, "非锁定技失效", "negative")
                self._log(f"{username} 【土著】：球状闪电再结算一次，{target} 非锁定技已封印")
        elif kind == "wallfacer_plan" and target:
            if target in self.players and self.players[target]["alive"]:
                n = 2 if self.players[target].get("vision_exposed") else 1
                taken = discard_from_target(self, target, n)
                self._log(f"{username} 【土著】：面壁计划再结算一次，{target} 弃 {taken} 张")
        elif kind == "curtain":
            cleared = clear_negative_statuses(self, username)
            drawn = self.draw_sys.draw_n(self.players[username]["tech_level"], 1)
            self._give_drawn(username, drawn)
            self._log(f"{username} 【土著】：帷幕再结算一次，清除 {cleared} 个负面，摸 {len(drawn)} 张")
        elif kind == "guzheng_plan" and choice:
            p = self.players[username]
            if choice == "draw2":
                drawn = self.draw_sys.draw_n(p["tech_level"], 2)
                self._give_drawn(username, drawn)
                self._log(f"{username} 【土著】：古筝再结算一次，摸 {len(drawn)} 张")
            elif choice == "heal2":
                self._heal(username, 2)
                self._log(f"{username} 【土著】：古筝再结算一次，回复至 {p['hp']}")
            elif choice == "discard_target2" and target:
                if target in self.players and self.players[target]["alive"]:
                    n = discard_from_target(self, target, 2)
                    self._log(f"{username} 【土著】：古筝再结算一次，弃置 {target} {n} 张")

    def _after_trick_settled(
        self,
        username: str,
        card: dict[str, Any],
        target: str | None,
        _action: dict[str, Any],
    ) -> None:
        """Instant tricks: native re-settle; choice tricks (古筝): stamp will_native on prompt."""
        cid = card.get("id")
        if cid == "guzheng_plan":
            if (
                self.prompt
                and self.prompt.get("type") == "choice"
                and self.prompt.get("card_id") == "guzheng_plan"
                and self._triggers_native(card)
                and skill_active(self.players[username], SKILL_NATIVE)
            ):
                self.prompt["will_native"] = True
            return
        if cid in {"wallfacer_plan", "curtain", "red_coast"}:
            self._maybe_native_repeat_instant(username, card, target=target, kind=str(cid))

    def _compute_kill_damage_full(self, tier: int, src: str, tgt: str) -> int:
        s, t = self.players[src], self.players[tgt]
        bonus = int(s.get("damage_bonus", 0)) + field_bonus_damage(self) * max(1, int(self.field_multiplier or 1))
        # temporarily inject for compute_kill_damage
        s2 = {**s, "damage_bonus": bonus}
        t2 = {**t, "damage_reduction": int(t.get("damage_reduction", 0))}
        return compute_kill_damage(tier, s2, t2)

    def _needs_trick_interrupt(self, username: str, card: dict[str, Any]) -> bool:
        cid = card.get("id")
        if cid in {"thought_stamp", "return_motion"}:
            return False
        for name in self.player_order:
            if name == username or not self.players[name]["alive"]:
                continue
            for c in self.players[name]["hand"]:
                if c.get("id") in {"thought_stamp", "return_motion"}:
                    return True
        return False

    def _open_trick_interrupt(self, username: str, card: dict[str, Any], target: str | None, action: dict[str, Any]) -> None:
        responders = []
        for name in self.player_order:
            if name == username or not self.players[name]["alive"]:
                continue
            if any(c.get("id") in {"thought_stamp", "return_motion"} for c in self.players[name]["hand"]):
                responders.append(name)
        self._pending_trick = {
            "from": username,
            "card": card,
            "target": target,
            "action": dict(action),
        }
        self.prompt = {
            "type": "interrupt_trick",
            "from": username,
            "to": responders[0],
            "queue": responders[1:],
            "card_name": card.get("name"),
            "nullified": False,
        }
        self.phase = "prompt"
        self._set_stage(
            "trick",
            card=card,
            from_name=username,
            to_name=target,
            text=f"{username} 打出{card.get('name')}",
        )
        self._log(f"{username} 打出{card.get('name')}，等待打断（{responders[0]}）")
        self._start_turn_timer()

    def _resolve_interrupt_or_toxic(self) -> None:
        if not self.prompt:
            return
        ptype = self.prompt.get("type")
        if ptype == "interrupt_trick":
            nullified = bool(self.prompt.get("nullified"))
            queue = list(self.prompt.get("queue") or [])
            if not nullified and queue:
                nxt = queue.pop(0)
                self.prompt["to"] = nxt
                self.prompt["queue"] = queue
                self._log(f"等待 {nxt} 打断响应")
                self._start_turn_timer()
                return
            pending = self._pending_trick
            self.prompt = None
            self._pending_trick = None
            self.phase = "turn"
            if not pending:
                self.refresh_turn_timer()
                return
            if nullified:
                self.discard.append(pending["card"])
                self._log(f"{pending['card'].get('name')} 被无效")
                self.refresh_turn_timer()
                return
            card = pending["card"]
            username = pending["from"]
            target = pending.get("target")
            action = pending.get("action") or {}
            cid = card.get("id")
            if cid == "toxic_water":
                card = {**card, "allow_response": True}
            if cid == "ladder_plan":
                ok, msg = self._play_ladder_plan(username, card, target)
                if not ok:
                    self.players[username]["hand"].append(card)
                    self._log(f"结算失败：{msg}")
                else:
                    self._log(msg)
                    self._on_card_used(username)
                    self._maybe_native_repeat_instant(username, card, target=target, kind="ladder")
            elif cid == "ball_lightning":
                ok, msg = self._play_ball_lightning(username, card, target)
                if not ok:
                    self.players[username]["hand"].append(card)
                    self._log(f"结算失败：{msg}")
                else:
                    self._log(msg)
                    self._on_card_used(username)
                    self._maybe_native_repeat_instant(username, card, target=target, kind="ball_lightning")
            elif is_temp_ascend_card(card):
                ok, msg = self._apply_temp_ascend(username, card)
                if not ok:
                    self.players[username]["hand"].append(card)
                    self._log(f"结算失败：{msg}")
                else:
                    self._log(msg)
                    self._on_card_used(username)
            elif cid in TRICK_HANDLERS:
                ok, msg = TRICK_HANDLERS[cid](self, username, card, target, action)
                if not ok:
                    # 结算失败（如古筝缺弃牌）：退回手牌
                    self.players[username]["hand"].append(card)
                    self._log(f"结算失败：{msg}")
                else:
                    self._log(msg)
                    self._on_card_used(username)
                    self._after_trick_settled(username, card, target, action)
            self.refresh_turn_timer()
            return
        if ptype == "respond_toxic":
            nullified = bool(self.prompt.get("nullified"))
            src = self.prompt["from"]
            tgt = self.prompt["to"]
            base = int(self.prompt.get("base", 2))
            queue = list(self.prompt.get("queue") or [])
            nullified_targets = list(self.prompt.get("nullified_targets") or [])
            if nullified:
                nullified_targets.append(tgt)
                self._log(f"{tgt} 响应剧毒之水成功")
            else:
                s, t = self.players[src], self.players[tgt]
                dmg = final_basic_damage(
                    base,
                    int(s.get("damage_bonus", 0)) + field_bonus_damage(self) * max(1, int(self.field_multiplier or 1)),
                    int(t.get("damage_reduction", 0)) + field_bonus_reduction(self) * max(1, int(self.field_multiplier or 1)),
                )
                msg = self._deal_damage(src, tgt, dmg, from_trick=True)
                self._log(f"剧毒之水结算：{msg}")
            if queue:
                nxt = queue.pop(0)
                self.prompt = {
                    "type": "respond_toxic",
                    "from": src,
                    "to": nxt,
                    "queue": queue,
                    "card_name": self.prompt.get("card_name"),
                    "base": base,
                    "nullified": False,
                    "nullified_targets": nullified_targets,
                }
                self.phase = "prompt"
                self._log(f"等待 {nxt} 响应剧毒之水")
                self._start_turn_timer()
                return
            self.prompt = None
            self.phase = "turn"
            self.refresh_turn_timer()
            self._check_win()
            return

    def _open_plan_part_choice(self, username: str, source: str | None) -> None:
        if self.phase in {"dying", "ended"}:
            return
        self.prompt = {
            "type": "choice",
            "to": username,
            "from": source,
            "card_id": "plan_part",
            "options": [
                {"id": "draw2", "label": "摸两张牌"},
                {"id": "retaliate", "label": "对伤害来源造成1点基础伤害"},
            ],
        }
        self.phase = "prompt"
        self._log(f"{username} 计划的一部分：选择摸2或反伤")
        self._start_turn_timer()

    def _start_next_killer_kill(self) -> None:
        queue = list(getattr(self, "killer_queue", None) or [])
        while queue:
            item = queue.pop(0)
            self.killer_queue = queue
            helper = item["from"]
            prey = item["to"]
            card = item["card"]
            if not self.players.get(helper, {}).get("alive"):
                continue
            if not self.players.get(prey, {}).get("alive"):
                continue
            if self._has_status(prey, STATUS_HIBERNATION):
                self.players[helper]["hand"].append(card)
                continue
            ok, msg = self._play_kill(helper, card, prey, is_native_repeat=True)
            self._log(f"Killer.5.2：{helper} 对 {prey} 出杀 — {msg}")
            return
        self.killer_queue = []
        if self.phase not in {"dying", "ended", "prompt"}:
            self.phase = "turn"
            self.refresh_turn_timer()

    def _apply_choice_prompt(self, username: str, action: dict[str, Any]) -> tuple[bool, str]:
        if not self.prompt or self.prompt.get("to") != username:
            return False, "不是你的选择"
        act = str(action.get("action", "")).strip()
        choice = str(action.get("choice", "")).strip()
        if act not in {"choose", "choice"} or not choice:
            return False, "请选择一项"
        opts = {o["id"] for o in (self.prompt.get("options") or [])}
        if choice not in opts:
            return False, "无效选项"
        card_id = self.prompt.get("card_id")
        if card_id == "guzheng_plan":
            p = self.players[username]
            will_native = bool(self.prompt.get("will_native"))
            choice_target: str | None = None
            if choice == "draw2":
                drawn = self.draw_sys.draw_n(p["tech_level"], 2)
                self._give_drawn(username, drawn)
                self._log(f"{username} 古筝：摸 {len(drawn)} 张")
            elif choice == "heal2":
                self._heal(username, 2)
                self._log(f"{username} 古筝：回复至 {p['hp']}")
            elif choice == "discard_target2":
                choice_target = str(action.get("target", "")).strip()
                if not choice_target or choice_target not in self.players or not self.players[choice_target]["alive"]:
                    return False, "请选择弃牌目标"
                n = discard_from_target(self, choice_target, 2)
                self._log(f"{username} 古筝：弃置 {choice_target} {n} 张")
            if will_native:
                fake = {"id": "guzheng_plan", "pool_entry": 18}
                self._maybe_native_repeat_instant(
                    username,
                    fake,
                    target=choice_target,
                    kind="guzheng_plan",
                    choice=choice,
                )
            self.prompt = None
            self.phase = "turn"
            self.refresh_turn_timer()
            self.seq += 1
            return True, "古筝已选择"
        if card_id == "star_ring_city":
            p = self.players[username]
            owner = str(self.prompt.get("from") or "")
            if choice == "give2":
                given = 0
                for _ in range(2):
                    if not p["hand"]:
                        break
                    c = p["hand"].pop(0)
                    if owner and owner in self.players and self.players[owner]["alive"]:
                        self.players[owner]["hand"].append(c)
                        given += 1
                    else:
                        self.discard.append(c)
                self._log(f"{username} 星环城：给予 {owner} {given} 张牌")
            else:
                src = self.players.get(owner) or p
                dmg = final_basic_damage(
                    1,
                    int(src.get("damage_bonus", 0)) + field_bonus_damage(self) * max(1, int(self.field_multiplier or 1)),
                    int(p.get("damage_reduction", 0)) + field_bonus_reduction(self) * max(1, int(self.field_multiplier or 1)),
                )
                msg = self._deal_damage(owner or username, username, dmg, from_trick=True)
                if owner and owner in self.players and self.players[owner]["alive"] and dmg > 0:
                    self._heal(owner, dmg)
                self._log(f"{username} 星环城：{msg}，{owner} 等量回血")
            queue = list(self.prompt.get("queue") or [])
            if queue:
                nxt = queue.pop(0)
                self.prompt["to"] = nxt
                self.prompt["queue"] = queue
                self._start_turn_timer()
                self.seq += 1
                return True, f"轮到 {nxt}"
            self.prompt = None
            self.phase = "turn"
            self.refresh_turn_timer()
            self.seq += 1
            return True, "星环城结束"
        if card_id == "zeroing":
            attacker = str(self.prompt.get("from") or "")
            p = self.players[username]
            if choice == "half_dmg":
                import math

                half = math.ceil(int(p["tech_level"]) / 2)
                src = self.players.get(attacker) or p
                dmg = final_basic_damage(
                    half,
                    int(src.get("damage_bonus", 0)) + field_bonus_damage(self) * max(1, int(self.field_multiplier or 1)),
                    int(p.get("damage_reduction", 0)) + field_bonus_reduction(self) * max(1, int(self.field_multiplier or 1)),
                )
                msg = self._deal_damage(attacker or username, username, dmg, from_trick=True)
                self._log(f"{username} 归零选受伤：{msg}")
            else:
                before = p["tech_level"]
                if not self._has_status(username, STATUS_TECH_LOCK):
                    p["tech_level"] = max(1, before - 1)
                discard_from_target(self, username, 1)
                self._log(f"{username} 归零选降科：科技 {before}→{p['tech_level']}")
            queue = list(self.prompt.get("queue") or [])
            if queue and self.phase not in {"dying", "ended"}:
                nxt = queue.pop(0)
                self.prompt["to"] = nxt
                self.prompt["queue"] = queue
                self.phase = "prompt"
                self._start_turn_timer()
                self.seq += 1
                return True, f"轮到 {nxt}"
            self.prompt = None
            if self.phase not in {"dying", "ended"}:
                self.phase = "turn"
                self.refresh_turn_timer()
            self.seq += 1
            return True, "归零选择完成"
        if card_id == "plan_part":
            p = self.players[username]
            if choice == "draw2":
                drawn = self.draw_sys.draw_n(p["tech_level"], 2)
                self._give_drawn(username, drawn)
                self._log(f"{username} 计划的一部分：摸 {len(drawn)} 张")
            else:
                src = str(self.prompt.get("from") or "")
                if src and src in self.players and self.players[src]["alive"]:
                    dmg = final_basic_damage(
                        1,
                        int(p.get("damage_bonus", 0)) + field_bonus_damage(self) * max(1, int(self.field_multiplier or 1)),
                        int(self.players[src].get("damage_reduction", 0))
                        + field_bonus_reduction(self) * max(1, int(self.field_multiplier or 1)),
                    )
                    msg = self._deal_damage(username, src, dmg, from_trick=True)
                    self._log(f"{username} 计划的一部分反伤：{msg}")
                else:
                    self._log(f"{username} 计划的一部分：无伤害来源可反")
            if int(p.get("plan_part_charges") or 0) <= 0:
                self._remove_status(username, STATUS_PLAN_PART)
                p.pop("plan_part_charges", None)
                self._log(f"{username} 计划的一部分状态结束")
            self.prompt = None
            if self.phase not in {"dying", "ended"}:
                self.phase = "turn"
                self.refresh_turn_timer()
            self.seq += 1
            return True, "计划的一部分已选择"
        return False, "未知选择牌"

    def _apply_interrupt_prompt(self, username: str, action: dict[str, Any]) -> tuple[bool, str]:
        if not self.prompt or self.prompt.get("to") != username:
            return False, "不是你的打断窗口"
        act = str(action.get("action", "")).strip()
        if act in {"respond_pass", "pass", "interrupt_pass"}:
            self._resolve_interrupt_or_toxic()
            self.seq += 1
            return True, "不打断"
        if act in {"play_card", "respond_dodge", "interrupt_play"}:
            instance_id = str(action.get("instance_id", "")).strip()
            hand = self.players[username]["hand"]
            idx = next((i for i, c in enumerate(hand) if c["instance_id"] == instance_id), None)
            if idx is None:
                return False, "手牌中没有这张牌"
            card = hand[idx]
            cid = card.get("id")
            if cid not in {"thought_stamp", "return_motion"}:
                return False, "只能打出思想钢印或回归运动"
            hand.pop(idx)
            ok, msg = TRICK_HANDLERS[cid](self, username, card, None, action)
            if ok:
                self._emit_play(username, card)
            self.seq += 1
            return ok, msg
        return False, "无效打断行动"

    def _apply_toxic_prompt(self, username: str, action: dict[str, Any]) -> tuple[bool, str]:
        if not self.prompt or self.prompt.get("to") != username:
            return False, "不是你的剧毒响应"
        act = str(action.get("action", "")).strip()
        if act in {"respond_pass", "pass"}:
            self._resolve_interrupt_or_toxic()
            self.seq += 1
            return True, "不响应剧毒"
        if act in {"play_card", "respond_dodge"}:
            instance_id = str(action.get("instance_id", "")).strip()
            hand = self.players[username]["hand"]
            idx = next((i for i, c in enumerate(hand) if c["instance_id"] == instance_id), None)
            if idx is None:
                return False, "手牌中没有这张牌"
            card = hand[idx]
            # 杀或思想钢印可响应
            if card.get("subtype") == "kill" or card.get("id") == "thought_stamp":
                hand.pop(idx)
                self.discard.append(card)
                self._emit_play(username, card)
                self.prompt["nullified"] = True
                self._log(f"{username} 用{card.get('name')}响应剧毒之水")
                self._resolve_interrupt_or_toxic()
                self.seq += 1
                return True, "剧毒之水被响应"
            return False, "请打出杀或思想钢印响应"
        return False, "无效响应"

    def _play_kill(
        self,
        username: str,
        card: dict[str, Any],
        target: str | None,
        *,
        is_native_repeat: bool = False,
    ) -> tuple[bool, str]:
        p = self.players[username]
        kill_limit = 2 + int(p.get("kill_limit_bonus", 0))
        if not is_native_repeat and p["kills_used_this_turn"] >= kill_limit:
            return False, f"本回合出杀已达上限（{kill_limit}）"
        if not target or target not in self.players:
            return False, "杀需要指定目标"
        if target == username:
            return False, "不能杀自己"
        if not self.players[target]["alive"]:
            return False, "目标已淘汰"
        if self._has_status(target, STATUS_HIBERNATION):
            return False, "目标冬眠中，不可选中"
        tgt = self.players[target]
        if tgt.get("star_ring"):
            return False, "星环号：无法成为基本牌目标"
        if skill_active(tgt, SKILL_SWORD_HOLDER) and tgt.get("vision_exposed"):
            return False, "执剑人视野已暴露，不能对其使用杀"
        tier = int(card.get("tier", 1))
        deterrence_extra_target = None
        if not is_native_repeat and p.get("deterrence_extra_target"):
            p["deterrence_extra_target"] = False
            raw_extra = getattr(self, "_pending_deterrence_target", None)
            if raw_extra and raw_extra != target and raw_extra in self.players and self.players[raw_extra]["alive"]:
                if not self._has_status(raw_extra, STATUS_HIBERNATION):
                    deterrence_extra_target = raw_extra
                    self._log(f"{username} 威慑：额外目标 {deterrence_extra_target}")
            self._pending_deterrence_target = None
        if not is_native_repeat:
            p["kills_used_this_turn"] += 1
            self.discard.append(card)
            self._on_basic_played(username, card)
        will_repeat = (
            not is_native_repeat
            and self._triggers_native(card)
            and skill_active(p, SKILL_NATIVE)
        )
        undodgeable = bool(
            skill_active(p, SKILL_LEADER)
            or (skill_active(p, SKILL_SWORD_HOLDER) and tgt.get("vision_exposed"))
        )
        if undodgeable:
            self.prompt = {
                "type": "respond_dodge",
                "from": username,
                "to": target,
                "kill_tier": tier,
                "card_name": card.get("name") if not is_native_repeat else f"{card.get('name')}（土著）",
                "will_native_repeat": will_repeat,
                "is_native_repeat": is_native_repeat,
                "deterrence_extra_target": deterrence_extra_target,
                "undodgeable": True,
            }
            self.phase = "prompt"
            self._set_stage(
                "kill",
                card=card,
                from_name=username,
                to_name=target,
                text=f"{username} 对 {target} 使用{card.get('name')}",
            )
            reason = "领袖" if skill_active(p, SKILL_LEADER) else "执剑人"
            self._log(f"{username} 对 {target} 使用{card.get('name')}（{reason}：无法响应）")
            self._finish_kill_prompt(dodged=False)
            return True, f"{reason}杀已结算"

        self.prompt = {
            "type": "respond_dodge",
            "from": username,
            "to": target,
            "kill_tier": tier,
            "card_name": card.get("name") if not is_native_repeat else f"{card.get('name')}（土著）",
            "will_native_repeat": will_repeat,
            "is_native_repeat": is_native_repeat,
            "deterrence_extra_target": deterrence_extra_target,
            "curvature": bool(tgt.get("curvature")),
            "extra_actions": (
                [{"id": "curvature", "label": "曲率判定", "action": "curvature_judge"}]
                if tgt.get("curvature")
                else []
            ),
        }
        self.phase = "prompt"
        self._set_stage(
            "kill",
            card=card,
            from_name=username,
            to_name=target,
            text=f"{username} 对 {target} 使用{card.get('name')}",
        )
        if is_native_repeat:
            self._log(f"{username} 【土著】：对 {target} 再次结算{card.get('name')}，等待闪响应")
        else:
            self._log(f"{username} 对 {target} 使用{card.get('name')}，等待闪响应")
        self._start_turn_timer()
        return True, f"等待 {target} 响应"

    def _finish_kill_prompt(self, dodged: bool) -> None:
        if not self.prompt or self.prompt.get("type") != "respond_dodge":
            return
        src = self.prompt["from"]
        tgt = self.prompt["to"]
        tier = int(self.prompt["kill_tier"])
        will_repeat = bool(self.prompt.get("will_native_repeat"))
        queue_wander = self.prompt.get("queue_wander")
        deterrence_extra = int(self.prompt.get("deterrence_extra") or 0)
        deterrence_extra_target = self.prompt.get("deterrence_extra_target")
        saved = dict(self.prompt)
        if dodged and self.players.get(src, {}).get("gravity_ship"):
            hand = self.players[src]["hand"]
            if len(hand) >= 2:
                self.prompt = {
                    "type": "gravity_override",
                    "to": src,
                    "from": src,
                    "saved_kill": saved,
                    "confirm": {
                        "accept_label": "引力覆盖",
                        "pass_label": "放弃引力",
                        "accept_action": "gravity_accept",
                        "pass_action": "gravity_pass",
                        "needs_cards": 2,
                    },
                }
                self.phase = "prompt"
                self._log(f"{src} 万有引力号：是否弃两张手牌使杀仍生效？")
                self._start_turn_timer()
                return
        self.prompt = None
        if not dodged:
            dmg = self._compute_kill_damage_full(tier, src, tgt)
            msg = self._deal_damage(src, tgt, dmg, from_kill=True)
            self._log(msg)
        if self.phase == "ended":
            return
        if self.phase == "dying":
            return
        # A nested prompt (流浪 / 圣母等) may have opened during damage resolution
        if self.phase == "prompt" and self.prompt:
            return
        if will_repeat and self.players.get(tgt, {}).get("alive"):
            fake = {"name": f"{tier}阶杀", "tier": tier}
            self._play_kill(src, fake, tgt, is_native_repeat=True)
            return
        if deterrence_extra_target and self.players.get(str(deterrence_extra_target), {}).get("alive"):
            fake = {"name": f"{tier}阶杀", "tier": tier}
            self._log(f"{src} 威慑追加目标 {deterrence_extra_target}")
            self._play_kill(src, fake, str(deterrence_extra_target), is_native_repeat=True)
            return
        if deterrence_extra and self.players.get(tgt, {}).get("alive"):
            fake = {"name": f"{tier}阶杀", "tier": tier}
            self._log(f"{src} 威慑追加杀")
            self._play_kill(src, fake, tgt, is_native_repeat=True)
            return
        if getattr(self, "killer_queue", None):
            self._start_next_killer_kill()
            return
        if queue_wander and self.players.get(str(queue_wander), {}).get("alive"):
            self._open_wander_prompt(str(queue_wander))
            return
        self.phase = "turn"
        self.refresh_turn_timer()
        self._check_win()

    def _on_card_used(self, username: str) -> None:
        p = self.players[username]
        if self.trisolaris_era == "chaos" and p.get("alive"):
            p["chaos_cards_used"] = int(p.get("chaos_cards_used") or 0) + 1
            if p["chaos_cards_used"] % 2 == 0:
                self._log(f"乱纪元：{username} 使用2张牌，受到1点最终伤害")
                self._deal_damage(username, username, 1)
        if not p.get("solar_observe"):
            return
        p["cards_used_this_turn"] = int(p.get("cards_used_this_turn") or 0) + 1
        if p["cards_used_this_turn"] % 4 == 0:
            drawn = self.draw_sys.draw_n(p["tech_level"], 1)
            self._give_drawn(username, drawn)
            self._log(f"{username} 太阳系观测单元：使用4张牌，摸1张")

    def _on_basic_played(self, username: str, card: dict[str, Any]) -> None:
        if not self._is_basic_card(card):
            return
        # 黑洞：敌方累计使用3张基本牌时，持有者获得这3张复制
        for holder in self.player_order:
            if holder == username:
                continue
            hp = self.players[holder]
            if not hp.get("alive") or not self._has_status(holder, STATUS_BLACK_HOLE):
                continue
            buf = list(hp.get("black_hole_enemy_basics") or [])
            buf.append(deepcopy(card))
            if len(buf) >= 3:
                copies = []
                for src in buf[:3]:
                    copy = deepcopy(src)
                    copy["instance_id"] = f"blackhole-{self.seq}-{src.get('instance_id')}"
                    copies.append(copy)
                self._give_drawn(holder, copies)
                hp["black_hole_enemy_basics"] = []
                self._remove_status(holder, STATUS_BLACK_HOLE)
                self._log(f"{holder} 黑洞：获得敌方3张基本牌复制，状态移除")
            else:
                hp["black_hole_enemy_basics"] = buf
                self._log(f"{holder} 黑洞：敌方基本牌累计 {len(buf)}/3")

    def _apply_prompt_action(self, username: str, action: dict[str, Any]) -> tuple[bool, str]:
        if not self.prompt:
            return False, "当前无响应"
        ptype = self.prompt.get("type")
        act = str(action.get("action", "")).strip()

        if ptype == "wander_draw":
            if self.prompt.get("to") != username:
                return False, "不是你的【流浪】询问"
            if act in {"wander_accept", "respond_accept"}:
                self._apply_wander(username, True)
                self.seq += 1
                return True, "发动流浪"
            if act in {"wander_pass", "respond_pass", "pass"}:
                self._apply_wander(username, False)
                self.seq += 1
                return True, "放弃流浪"
            return False, "无效的流浪响应"

        if ptype == "gravity_override":
            if self.prompt.get("to") != username:
                return False, "不是你的万有引力号窗口"
            saved = self.prompt.get("saved_kill") or {}
            if act in {"gravity_pass", "respond_pass", "pass"}:
                self.prompt = saved if saved.get("type") == "respond_dodge" else None
                # treat as successful dodge already happened
                self.prompt = None
                src = saved.get("from")
                tgt = saved.get("to")
                will_repeat = bool(saved.get("will_native_repeat"))
                queue_wander = saved.get("queue_wander")
                deterrence_extra = int(saved.get("deterrence_extra") or 0)
                tier = int(saved.get("kill_tier") or 1)
                self.phase = "turn"
                self._log(f"{username} 放弃万有引力号")
                if will_repeat and self.players.get(str(tgt), {}).get("alive"):
                    self._play_kill(str(src), {"name": f"{tier}阶杀", "tier": tier}, str(tgt), is_native_repeat=True)
                elif deterrence_extra and self.players.get(str(tgt), {}).get("alive"):
                    self._play_kill(str(src), {"name": f"{tier}阶杀", "tier": tier}, str(tgt), is_native_repeat=True)
                elif queue_wander and self.players.get(str(queue_wander), {}).get("alive"):
                    self._open_wander_prompt(str(queue_wander))
                else:
                    self.refresh_turn_timer()
                self.seq += 1
                return True, "放弃引力覆盖"
            if act in {"gravity_accept", "respond_accept"}:
                hand = self.players[username]["hand"]
                ids = action.get("instance_ids")
                if not isinstance(ids, list) or len(ids) != 2:
                    return False, "需要弃置两张手牌"
                ids = [str(x).strip() for x in ids]
                if len(set(ids)) != 2:
                    return False, "需要两张不同的牌"
                by_id = {c["instance_id"]: c for c in hand}
                if any(i not in by_id for i in ids):
                    return False, "手牌中没有所选牌"
                remove = set(ids)
                discarded = [c for c in hand if c["instance_id"] in remove]
                self.players[username]["hand"] = [c for c in hand if c["instance_id"] not in remove]
                self.discard.extend(discarded)
                self._log(f"{username} 万有引力号：弃两张使杀仍生效")
                self.prompt = saved
                self._finish_kill_prompt(dodged=False)
                self.seq += 1
                return True, "引力覆盖成功"
            return False, "无效引力响应"

        if ptype == "choice":
            return self._apply_choice_prompt(username, action)
        if ptype == "soap_heal":
            if self.prompt.get("to") != username:
                return False, "不是你的香皂选择"
            target = str(action.get("target", "")).strip()
            if act not in {"choose", "soap_heal", "play_card"} or not target:
                return False, "请指定+1血目标"
            if target not in (self.prompt.get("targets") or []):
                return False, "无效目标"
            if not self.players.get(target, {}).get("alive"):
                return False, "目标已淘汰"
            self._heal(target, 1)
            remaining = int(self.prompt.get("remaining") or 1) - 1
            self._log(f"{username} 香皂：{target} +1（剩余 {remaining}）")
            if remaining > 0:
                alive = [n for n in self.player_order if self.players[n]["alive"]]
                self.prompt["remaining"] = remaining
                self.prompt["targets"] = alive
                self._start_turn_timer()
                self.seq += 1
                return True, f"香皂剩余 {remaining}"
            self.prompt = None
            self.phase = "turn"
            self.refresh_turn_timer()
            self.seq += 1
            return True, "香皂结束"
        if ptype == "interrupt_trick":
            return self._apply_interrupt_prompt(username, action)
        if ptype == "respond_toxic":
            return self._apply_toxic_prompt(username, action)
        if ptype != "respond_dodge":
            return False, "当前无响应"
        if self.prompt.get("to") != username:
            return False, "不是你的响应"
        if act in {"respond_pass", "pass"}:
            self._finish_kill_prompt(dodged=False)
            self.seq += 1
            return True, "不响应"
        if act == "curvature_judge":
            if not self.players[username].get("curvature"):
                return False, "未装备曲率引擎"
            import random
            roll = random.randint(1, 2)
            self._log(f"{username} 曲率引擎判定：{roll}")
            if roll == 1:
                self._finish_kill_prompt(dodged=True)
                self.seq += 1
                return True, "曲率判定视为出闪"
            self._finish_kill_prompt(dodged=False)
            self.seq += 1
            return True, "曲率判定失败"
        if act == "respond_dodge" or act == "play_card":
            instance_id = str(action.get("instance_id", "")).strip()
            hand = self.players[username]["hand"]
            idx = next((i for i, c in enumerate(hand) if c["instance_id"] == instance_id), None)
            if idx is None:
                return False, "手牌中没有这张牌"
            card = hand[idx]
            if card.get("subtype") != "dodge":
                return False, "请打出闪来响应"
            if has_field(self, "sophon_blind"):
                return False, "智子盲区：无法响应基本牌"
            if not can_dodge(card, int(self.prompt["kill_tier"])):
                return False, "闪的阶数不足以响应此杀"
            hand.pop(idx)
            self.discard.append(card)
            self._emit_play(username, card)
            self._log(f"{username} 打出{card.get('name')}，响应成功")
            self._finish_kill_prompt(dodged=True)
            self.seq += 1
            return True, "成功闪避"
        return False, "无效的响应行动"

    def _resolve_kill_unanswered(self) -> None:
        self._finish_kill_prompt(dodged=False)

    def _use_ultimate_law(self, username: str, action: dict[str, Any]) -> tuple[bool, str]:
        p = self.players[username]
        if self.turn_phase != "play":
            return False, "现在不是出牌阶段"
        if not has_ship(p, "ultimate_law"):
            return False, "未装备终极规律号"
        if p.get("ultimate_law_used"):
            return False, "本回合已使用终极规律号"
        target = str(action.get("target", "")).strip()
        if not target or target not in self.players or not self.players[target]["alive"]:
            return False, "需要指定存活目标"
        thand = self.players[target]["hand"]
        if not thand:
            return False, "目标没有手牌"
        instance_id = str(action.get("instance_id", "")).strip()
        if instance_id:
            src = next((c for c in thand if c["instance_id"] == instance_id), None)
            if not src:
                return False, "目标手牌中没有这张牌"
        else:
            import random

            src = random.choice(thand)
        from copy import deepcopy as _dc

        copy = _dc(src)
        copy["instance_id"] = f"law-{self.seq}-{src.get('instance_id')}"
        p["hand"].append(copy)
        p["ultimate_law_used"] = True
        self._log(f"{username} 终极规律号：观看并复制 {target} 的 {copy.get('name')}")
        self.refresh_turn_timer()
        self.seq += 1
        return True, f"复制了 {copy.get('name')}"


    def _status_text(self, status_id: str) -> str:
        cdef = (self.card_defs or {}).get(status_id) or {}
        text = str(cdef.get("text") or "").strip()
        if text:
            return text
        return STATUS_TEXT_FALLBACK.get(status_id, "")

    def _enrich_status(self, name: str, status: dict[str, Any]) -> dict[str, Any]:
        p = self.players[name]
        out = deepcopy(status)
        sid = str(out.get("id") or "")
        out["text"] = self._status_text(sid)
        if sid == STATUS_PLAN_PART:
            charges = int(p.get("plan_part_charges") or 0)
            out["value"] = charges
            out["unit"] = "次"
        elif sid == STATUS_MICRO_UNIVERSE:
            shield = int(p.get("micro_universe_shield") or 0)
            out["value"] = shield
            out["unit"] = "点"
        elif sid == "countdown" or p.get("countdown") is not None and sid == "countdown":
            pass
        return out

    def public_player_view(self, name: str) -> dict[str, Any]:
        p = self.players[name]
        statuses = [self._enrich_status(name, s) for s in (p.get("statuses") or [])]
        return {
            "username": name,
            "hp": p["hp"],
            "max_hp": p["max_hp"],
            "alive": p["alive"],
            "hand_count": len(p["hand"]),
            "online": self.player_online.get(name, True),
            "equipment": deepcopy(p["equipment"]),
            "statuses": statuses,
            "tech_level": p["tech_level"],
            "vision_exposed": p["vision_exposed"],
            "damage_bonus": p["damage_bonus"],
            "damage_reduction": p["damage_reduction"],
            "ascension": p.get("ascension"),
            "role_id": p.get("role_id"),
            "role_name": p["role_name"],
            "skills": deepcopy(p.get("skills") or []),
            "skills_sealed": self._has_status(name, STATUS_SKILLS_SEALED),
            "shield": int(p.get("shield") or 0),
            "countdown": p.get("countdown"),
            "plan_part_charges": int(p.get("plan_part_charges") or 0) or None,
            "micro_universe_shield": (
                int(p["micro_universe_shield"])
                if p.get("micro_universe_shield") is not None
                else None
            ),
        }

    def _viewer_actions(self, viewer: str) -> list[dict[str, Any]]:
        """Play-phase voluntary actions for the viewer (role/equipment actives)."""
        me = self.players.get(viewer)
        if not me or not me.get("alive"):
            return []
        if self.phase != "turn" or self.turn_phase != "play" or self.current_player() != viewer:
            return []
        out: list[dict[str, Any]] = []
        if has_ship(me, "ultimate_law") and not me.get("ultimate_law_used"):
            out.append(
                {
                    "id": "ultimate_law",
                    "label": "终极规律",
                    "action": "ultimate_law",
                    "needs_target": True,
                }
            )
        return out

    def snapshot_for(self, viewer: str, *, with_events: bool = True) -> dict[str, Any]:
        me = self.players.get(viewer)
        private_hand = deepcopy(me["hand"]) if me else []
        private_role = None
        if me:
            private_role = {
                "role_id": me["role_id"],
                "role_name": me["role_name"],
                "skills": deepcopy(me.get("skills") or []),
            }
        timed = self.phase in {"turn", "prompt", "dying"}
        remaining = max(0.0, self.turn_deadline_at - time.time()) if timed else 0.0
        limit = hand_limit(me["max_hp"]) if me else 0
        actions = self._viewer_actions(viewer)
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
            "events": self._events_for_viewer(viewer) if with_events else [],
            "log": self.log[-16:],
            "winner": self.winner,
            "winner_faction": self.winner_faction,
            "turn_seconds": TURN_SECONDS,
            "turn_remaining": remaining,
            "turn_deadline_ms": int(self.turn_deadline_at * 1000) if timed else None,
            "prompt": deepcopy(self.prompt),
            "dying": deepcopy(self.dying),
            "stage": self._compute_stage(),
            "fields": deepcopy(self.fields),
            "field_multiplier": self.field_multiplier,
            "trisolaris_era": self.trisolaris_era,
            "you": {
                "username": viewer,
                "hand": private_hand,
                "role": private_role,
                "hp": me["hp"] if me else 0,
                "alive": me["alive"] if me else False,
                "tech_level": me["tech_level"] if me else 1,
                "hand_limit": limit,
                "kills_used_this_turn": me["kills_used_this_turn"] if me else 0,
                "skills_sealed": self._has_status(viewer, STATUS_SKILLS_SEALED) if me else False,
                "shield": int(me.get("shield") or 0) if me else 0,
                "actions": actions,
                # compat for older clients
                "ultimate_law_ready": any(a.get("id") == "ultimate_law" for a in actions),
            },
        }
