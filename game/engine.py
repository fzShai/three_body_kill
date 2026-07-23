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
    has_armor,
    has_ship,
    is_temp_ascend_card,
    resolve_slot,
)
from game.skills import (
    SKILL_BENEVOLENCE,
    SKILL_COHESION,
    SKILL_COUNTDOWN,
    SKILL_FLYING_BLADE,
    SKILL_LEADER,
    SKILL_MADONNA,
    SKILL_NATIVE,
    SKILL_RED_SHORE,
    SKILL_STARSHIP,
    SKILL_SWORD_HOLDER,
    SKILL_WALLFACER,
    SKILL_WANDER,
    STATUS_SKILLS_SEALED,
    skill_active,
)
from game.stats import final_basic_damage, final_true_damage, initial_combat_fields
from game.trick_effects import (
    HANDLERS as TRICK_HANDLERS,
    STATUS_CRADLE,
    STATUS_FLIPPED,
    STATUS_HIBERNATION,
    STATUS_TECH_LOCK,
    TARGET_TRICKS,
    field_bonus_damage,
    field_bonus_reduction,
    has_field,
    legal_play as trick_legal_play,
)
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
            "madonna_used": False,
            "flying_blade_used": False,
            "next_damage_true": False,
            "countdown": None,
            "countdown_done": False,
            "shield": 0,
            "cards_used_this_turn": 0,
            **initial_combat_fields(),
        }
        if skill_active(p, SKILL_STARSHIP):
            p["tech_level"] = 4
        if skill_active(p, SKILL_COUNTDOWN):
            p["countdown"] = 12
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
        self._pending_conclude: str | None = None
        self.fields: list[dict[str, Any]] = []
        self.field_multiplier: int = 1
        self.trisolaris_era: str | None = None
        self._pending_trick: dict[str, Any] | None = None
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
        if skill_active(p, SKILL_LEADER):
            n = 4
        if p.get("ascension") == "psychic":
            n += 1
        n += int(p.get("extra_draw", 0))
        drawn = self.draw_sys.draw_n(p["tech_level"], n)
        p["hand"].extend(drawn)
        self.turn_phase = "play"
        p["kills_used_this_turn"] = 0
        p["red_coast_used"] = False
        p["flying_blade_used"] = False
        p["next_damage_true"] = False
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
            p["hand"].append(kill)
            self._log(f"{name} 量子号：获得一张三阶杀")
        self._log(f"{name} 摸牌阶段摸了 {len(drawn)} 张")
        if skill_active(p, SKILL_BENEVOLENCE) and p.get("alive"):
            self._open_benevolence_prompt(name)

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
            if ptype == "benevolence_heal":
                self._log(f"{self.prompt.get('to')} 【仁心】超时，视为放弃")
                self._apply_benevolence(str(self.prompt.get("to")), None)
                self.seq += 1
                return True
            if ptype == "madonna_save":
                self._log(f"{self.prompt.get('to')} 【圣母】超时，视为放弃")
                self._apply_madonna(str(self.prompt.get("to")), False)
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
        if after > before:
            self._tick_countdowns(2)
        if before < 6 <= after and not p.get("ascended"):
            self._grant_ascension(username)
        if notify:
            self._on_tech_changed(username, before, after)
        return True

    def _tick_countdowns(self, amount: int) -> None:
        for name in self.player_order:
            p = self.players[name]
            if not p.get("alive") or p.get("countdown_done"):
                continue
            if not skill_active(p, SKILL_COUNTDOWN) or p.get("countdown") is None:
                continue
            before = int(p["countdown"])
            p["countdown"] = max(0, before - int(amount))
            self._log(f"{name} 【倒计时】：{before} → {p['countdown']}")
            if p["countdown"] == 0 and before > 0:
                self._resolve_countdown(name)

    def _resolve_countdown(self, username: str) -> None:
        p = self.players[username]
        p["countdown_done"] = True
        self._raise_tech(username, 1)
        p["max_hp"] = int(p["max_hp"]) + 1
        self._heal(username, 2)
        drawn = self.draw_sys.draw_n(p["tech_level"], 2)
        p["hand"].extend(drawn)
        self._log(f"{username} 【倒计时】归零：科技+1，上限+1，回2，摸{len(drawn)}")

    def _open_benevolence_prompt(self, username: str) -> None:
        targets = [n for n in self.player_order if self.players[n]["alive"]]
        self.prompt = {
            "type": "benevolence_heal",
            "to": username,
            "from": username,
            "targets": targets,
        }
        self.phase = "prompt"
        self._log(f"{username} 【仁心】：指定一名角色回复 2 点体力")
        self._start_turn_timer()

    def _apply_benevolence(self, username: str, target: str | None) -> None:
        self.prompt = None
        if target and target in self.players and self.players[target]["alive"]:
            self._heal(target, 2)
            self._log(f"{username} 【仁心】：{target} 回复至 {self.players[target]['hp']}")
        else:
            self._log(f"{username} 【仁心】未指定有效目标")
        if self.phase not in {"dying", "ended"}:
            self.phase = "turn"
            self.refresh_turn_timer()

    def _madonna_holders(self) -> list[str]:
        out = []
        for name in self.player_order:
            p = self.players[name]
            if (
                p.get("alive")
                and skill_active(p, SKILL_MADONNA)
                and not p.get("madonna_used")
            ):
                out.append(name)
        return out

    def _open_madonna_prompt(self, victim: str, holders: list[str]) -> None:
        self.prompt = {
            "type": "madonna_save",
            "to": holders[0],
            "queue": holders[1:],
            "victim": victim,
            "from": victim,
        }
        self.phase = "prompt"
        self._log(f"{holders[0]} 【圣母】：是否救治濒死的 {victim}？")
        self._start_turn_timer()

    def _apply_madonna(self, username: str, accept: bool) -> None:
        if not self.prompt or self.prompt.get("type") != "madonna_save":
            return
        victim = str(self.prompt.get("victim"))
        queue = list(self.prompt.get("queue") or [])
        if accept and victim in self.players:
            p = self.players[username]
            v = self.players[victim]
            p["madonna_used"] = True
            v["hp"] = int(v["max_hp"])
            self._set_tech(victim, 1, force=True)
            discarded = list(v["hand"])
            v["hand"] = []
            self.discard.extend(discarded)
            self.prompt = None
            self.dying = None
            self.phase = "turn"
            self._log(
                f"{username} 【圣母】救治 {victim}：满血、科技降至1、弃置{len(discarded)}张手牌"
            )
            self.refresh_turn_timer()
            return
        if queue:
            nxt = queue.pop(0)
            self.prompt["to"] = nxt
            self.prompt["queue"] = queue
            self._log(f"{nxt} 【圣母】：是否救治濒死的 {victim}？")
            self._start_turn_timer()
            return
        self.prompt = None
        self.phase = "dying"
        self.dying = {"victim": victim, "source": (self.dying or {}).get("source")}
        self._log(f"无人发动【圣母】，{victim} 进入濒死求桃")
        self._start_turn_timer()

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
            drawn = self.draw_sys.draw_n(p["tech_level"], 2)
            p["hand"].extend(drawn)
            self._log(f"{username} 发动【流浪】：失去 1 体力，摸 {len(drawn)} 张（HP {p['hp']}）")
            if p["hp"] <= 0:
                if after == "conclude_turn":
                    self._pending_conclude = username
                self._begin_dying(username)
                return
        else:
            self._log(f"{username} 放弃【流浪】")
        if native_after and native_after.get("kind") == "visitor":
            # visitor has no tier — no-op placeholder for future
            pass
        if after == "conclude_turn":
            self._finish_conclude_turn(username)
            return
        if self.phase not in {"dying", "ended"}:
            self.phase = "turn"
            self.refresh_turn_timer()

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
        if has_field(self, "crisis_field") and p.get("alive"):
            import random

            dmg = random.randint(1, 2) * max(1, int(self.field_multiplier or 1))
            self._log(f"危机场地：{username} 受到 {dmg} 点最终伤害")
            self._deal_damage(username, username, dmg)
        if p.get("finale_death_pending") and p.get("alive"):
            p["finale_death_pending"] = False
            self._log(f"{username} 终末到期，出局")
            self._eliminate_player(username)
            if self._check_win():
                return
        self._tick_countdowns(1)
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
            if self.trisolaris_era == "chaos" and self.players[nxt]["alive"]:
                self.players[nxt]["hp"] -= 1
                self._log(f"乱纪元：{nxt} 失去 1 体力（HP {self.players[nxt]['hp']}）")
                if self.players[nxt]["hp"] <= 0:
                    self._begin_dying(nxt)
                    if self.phase == "dying":
                        return
            self.phase = "turn"
            self.turn_phase = "draw"
            self._run_draw_phase()
            if self.trisolaris_era == "stable":
                extra = self.draw_sys.draw_n(self.players[nxt]["tech_level"], 1)
                self.players[nxt]["hand"].extend(extra)
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
        self._trigger_red_shore(username, killer)

    def _trigger_red_shore(self, victim: str, killer: str | None) -> None:
        for name in self.player_order:
            p = self.players[name]
            if not p.get("alive") or not skill_active(p, SKILL_RED_SHORE):
                continue
            drawn = self.draw_sys.draw_n(p["tech_level"], 1)
            p["hand"].extend(drawn)
            self._heal(name, 1)
            self._log(f"{name} 【红岸】：因 {victim} 死亡摸1回1")
            if killer and killer == name and killer != victim:
                drawn2 = self.draw_sys.draw_n(p["tech_level"], 1)
                p["hand"].extend(drawn2)
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
        if charges > 0 and dmg > 0:
            dmg = dmg // 2
            t["plan_part_charges"] = charges - 1
            self._log(f"{target} 计划的一部分：伤害减半（剩余{t['plan_part_charges']}次）")
        shield = int(t.get("shield") or 0)
        if shield > 0 and dmg > 0:
            absorb = min(shield, dmg)
            t["shield"] = shield - absorb
            dmg -= absorb
            if t.get("micro_universe_shield") is not None:
                t["micro_universe_shield"] = max(0, int(t["micro_universe_shield"]) - absorb)
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
        src_p = self.players.get(source)
        true_dmg = bool(src_p and src_p.get("next_damage_true") and source != target)
        if true_dmg and src_p is not None:
            src_p["next_damage_true"] = False
            final = final_true_damage(int(final), 0)
            self._log(f"{source} 【飞刃】：本次伤害视为真实伤害")

        if from_kill and int(t.get("quantum_ghost_hp") or 0) > 0:
            t["quantum_ghost_hp"] = 0
            self._unequip_slot(target, "armor", to_discard=True)
            self._log(f"{target} 量子幽灵替身承受杀，装备移出")
            return f"{target} 的量子幽灵替身承受了杀"

        final = self._incoming_damage(target, final, true_dmg=true_dmg)
        t["hp"] -= final
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
            reflect = final
            s = self.players[source]
            s["hp"] -= reflect
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
            self._log(f"{target} 【面壁者】：{source} 扣 1 点体力（HP {src['hp']}）")
            if src["hp"] <= 0:
                self._begin_dying(source, source=target)
        if from_trick and final > 0 and t.get("star_ring") and t.get("alive"):
            self._unequip_slot(target, "ship", to_discard=True)
            self._log(f"{target} 星环号：受锦囊伤害后失去舰船")
        if t["hp"] <= 0:
            msg += "，" + self._begin_dying(target, source=source)
        return msg

    def _heal(self, username: str, amount: int) -> None:
        p = self.players[username]
        bonus = 1 if p.get("deep_sea") and not p.get("vision_exposed") else 0
        p["hp"] = min(p["max_hp"], p["hp"] + amount + bonus)

    def _begin_dying(self, victim: str, *, source: str | None = None) -> str:
        holders = self._madonna_holders()
        if holders:
            self.dying = {"victim": victim, "source": source}
            self._open_madonna_prompt(victim, holders)
            return f"{victim} 濒死（圣母询问中）"
        self.phase = "dying"
        self.dying = {"victim": victim, "source": source}
        self.prompt = None
        self._log(f"{victim} 进入濒死")
        self._start_turn_timer()
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
            if self._pending_conclude:
                who = self._pending_conclude
                self._pending_conclude = None
                if self.phase not in {"ended"} and self.players.get(who, {}).get("alive") is not False:
                    self._finish_conclude_turn(who)
            return
        # 死神永生：即将出局时回2摸2并移出装备
        p = self.players[victim]
        if p.get("death_immortal") or has_armor(p, "death_immortal"):
            self._unequip_slot(victim, "armor", to_discard=True)
            p["hp"] = min(p["max_hp"], max(1, p["hp"] + 2))
            drawn = self.draw_sys.draw_n(p["tech_level"], 2)
            p["hand"].extend(drawn)
            self.dying = None
            self.phase = "turn"
            self._log(f"{victim} 死神永生：回2摸{len(drawn)}并移出装备（HP {p['hp']}）")
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
            self._log(f"{username} 弃置 {card.get('name')}")
            self.refresh_turn_timer()
            self.seq += 1
            return True, "已弃置"

        if act == "discard_for_tech":
            return self._discard_for_tech(username, action)

        if act == "recast":
            return self._recast(username, str(action.get("instance_id", "")).strip())

        if act == "flying_blade":
            p = self.players[username]
            if self.turn_phase != "play":
                return False, "现在不是出牌阶段"
            if not skill_active(p, SKILL_FLYING_BLADE):
                return False, "无法发动飞刃"
            if p.get("flying_blade_used"):
                return False, "本回合已发动飞刃"
            p["flying_blade_used"] = True
            p["next_damage_true"] = True
            self._log(f"{username} 【飞刃】：下一次伤害变为真实伤害")
            self.refresh_turn_timer()
            self.seq += 1
            return True, "飞刃已发动"

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
            v["hp"] = min(v["max_hp"], max(1, v["hp"] + heal))
            self.discard.append(card)
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
                "gravity", "star_ring", "ultimate_law",
                "nano_center", "chip_workshop", "stars_plan",
                "deep_sea", "eco_bottle", "lightspeed_2", "curvature", "solar_observe",
                "plan_part", "black_hole", "micro_universe", "death_immortal", "quantum_ghost",
            }
            return str(cid) in known
        return False

    @staticmethod
    def _triggers_native(card: dict[str, Any]) -> bool:
        """土著：1阶牌，或无阶的桃/天外来客。"""
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
            # 凝聚：天外来客可重铸 —— 对「有合法打法」判定仍为 True，重铸处单独放行
            return True
        if cid == "ball_lightning":
            return self._card_implemented(card) and bool(self._alive_others(username))
        if cid == "ladder_plan" or cid == "red_coast":
            if not self._card_implemented(card):
                return False
            if cid == "ladder_plan":
                return bool(self._unexposed_others(username))
            return True
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
        can_recast_visitor = (
            (card.get("subtype") == "visitor" or card.get("id") == "visitor")
            and skill_active(self.players[username], SKILL_COHESION)
        )
        if self._card_has_legal_play(username, card) and not can_recast_visitor:
            return False, "该牌有合法打法，不能重铸"
        hand.pop(idx)
        self.discard.append(card)
        drawn = self.draw_sys.draw_one(self.players[username]["tech_level"])
        hand.append(drawn)
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
            ok, msg = self._play_kill(username, card, target)
            if not ok:
                hand.insert(idx, card)
                return False, msg
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
            heal = int(card.get("heal", 2))
            self._heal(username, heal)
            self.discard.append(card)
            self._on_basic_played(username, card)
            self._on_card_used(username)
            self._log(f"{username} 使用桃，HP {self.players[username]['hp']}")
            self._maybe_native_repeat_instant(username, card, target=username, kind="peach", heal=heal)
            self.refresh_turn_timer()
            self.seq += 1
            return True, f"回复至 {self.players[username]['hp']} HP"

        if subtype == "visitor" or cid == "visitor":
            self.discard.append(card)
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
            ok, msg = self._play_ladder_plan(username, card, target)
            if not ok:
                hand.insert(idx, card)
                return False, msg
            self._maybe_native_repeat_instant(username, card, target=target, kind="ladder")
            self.refresh_turn_timer()
            self.seq += 1
            return True, msg

        if cid == "ball_lightning":
            ok, msg = self._play_ball_lightning(username, card, target)
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

        if cid in TRICK_HANDLERS:
            if cid in {"thought_stamp", "return_motion"}:
                hand.insert(idx, card)
                return False, "该牌只能在响应窗口打出"
            if self._needs_trick_interrupt(username, card):
                # keep card out; store pending
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
            self._on_card_used(username)
            self.refresh_turn_timer()
            self.seq += 1
            return True, msg

        if is_temp_ascend_card(card):
            ok, msg = self._apply_temp_ascend(username, card)
            if not ok:
                hand.insert(idx, card)
                return False, msg
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
            if cid in TRICK_HANDLERS:
                ok, msg = TRICK_HANDLERS[cid](self, username, card, target, action)
                self._log(msg if ok else f"结算失败：{msg}")
            self.refresh_turn_timer()
            return
        if ptype == "respond_toxic":
            nullified = bool(self.prompt.get("nullified"))
            src = self.prompt["from"]
            tgt = self.prompt["to"]
            base = int(self.prompt.get("base", 2))
            self.prompt = None
            self.phase = "turn"
            if nullified:
                self._log("剧毒之水被无效")
                self.refresh_turn_timer()
                return
            s, t = self.players[src], self.players[tgt]
            dmg = final_basic_damage(
                base,
                int(s.get("damage_bonus", 0)) + field_bonus_damage(self) * max(1, int(self.field_multiplier or 1)),
                int(t.get("damage_reduction", 0)),
            )
            msg = self._deal_damage(src, tgt, dmg, from_trick=True)
            self._log(f"剧毒之水结算：{msg}")
            self.refresh_turn_timer()
            self._check_win()

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
            if choice == "draw2":
                drawn = self.draw_sys.draw_n(p["tech_level"], 2)
                p["hand"].extend(drawn)
                self._log(f"{username} 古筝：摸 {len(drawn)} 张")
            elif choice == "heal2":
                self._heal(username, 2)
                self._log(f"{username} 古筝：回复至 {p['hp']}")
            elif choice == "tech1":
                self._raise_tech(username, 1)
                self._log(f"{username} 古筝：科技 {p['tech_level']}")
            self.prompt = None
            self.phase = "turn"
            self.refresh_turn_timer()
            self.seq += 1
            return True, "古筝已选择"
        if card_id == "star_ring_city":
            p = self.players[username]
            if choice == "draw1":
                drawn = self.draw_sys.draw_n(p["tech_level"], 1)
                p["hand"].extend(drawn)
                self._log(f"{username} 星环城：摸 1 张")
            else:
                if p["hand"]:
                    c = p["hand"].pop()
                    self.discard.append(c)
                    self._log(f"{username} 星环城：弃 {c.get('name')}")
                else:
                    self._log(f"{username} 星环城：无牌可弃")
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
        extra = 0
        if not is_native_repeat and int(p.get("deterrence_extra", 0)) > 0:
            extra = 1
            p["deterrence_extra"] = int(p.get("deterrence_extra", 0)) - 1
            self._log(f"{username} 威慑：本杀额外结算一次")
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
                "deterrence_extra": extra,
                "undodgeable": True,
            }
            self.phase = "prompt"
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
            "deterrence_extra": extra,
            "curvature": bool(tgt.get("curvature")),
        }
        self.phase = "prompt"
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
        saved = dict(self.prompt)
        if dodged and self.players.get(src, {}).get("gravity_ship"):
            hand = self.players[src]["hand"]
            if len(hand) >= 2:
                self.prompt = {
                    "type": "gravity_override",
                    "to": src,
                    "from": src,
                    "saved_kill": saved,
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
        if deterrence_extra and self.players.get(tgt, {}).get("alive"):
            fake = {"name": f"{tier}阶杀", "tier": tier}
            self._log(f"{src} 威慑追加杀")
            self._play_kill(src, fake, tgt, is_native_repeat=True)
            return
        if queue_wander and self.players.get(str(queue_wander), {}).get("alive"):
            self._open_wander_prompt(str(queue_wander))
            return
        self.phase = "turn"
        self.refresh_turn_timer()
        self._check_win()

    def _on_card_used(self, username: str) -> None:
        p = self.players[username]
        if not p.get("solar_observe"):
            return
        p["cards_used_this_turn"] = int(p.get("cards_used_this_turn") or 0) + 1
        if p["cards_used_this_turn"] % 4 == 0:
            drawn = self.draw_sys.draw_n(p["tech_level"], 1)
            p["hand"].extend(drawn)
            self._log(f"{username} 太阳系观测单元：使用4张牌，摸1张")

    def _on_basic_played(self, username: str, card: dict[str, Any]) -> None:
        p = self.players[username]
        if p.get("black_hole_basics") is None and not has_armor(p, "black_hole"):
            return
        if not self._is_basic_card(card):
            return
        p["black_hole_basics"] = int(p.get("black_hole_basics") or 0) + 1
        if p["black_hole_basics"] >= 3 and p["hand"]:
            import random
            from copy import deepcopy as _dc

            p["black_hole_basics"] = 0
            src = random.choice(p["hand"])
            copy = _dc(src)
            copy["instance_id"] = f"blackhole-{self.seq}-{src.get('instance_id')}"
            p["hand"].append(copy)
            self._log(f"{username} 黑洞：累积3张基本牌，复制 {copy.get('name')}")

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

        if ptype == "benevolence_heal":
            if self.prompt.get("to") != username:
                return False, "不是你的【仁心】"
            if act in {"benevolence_pass", "respond_pass", "pass"}:
                self._apply_benevolence(username, None)
                self.seq += 1
                return True, "放弃仁心"
            target = str(action.get("target", "")).strip() or None
            if act in {"benevolence_heal", "choose", "play_card"} and target:
                if target not in (self.prompt.get("targets") or []):
                    return False, "无效目标"
                self._apply_benevolence(username, target)
                self.seq += 1
                return True, "仁心已结算"
            return False, "请指定目标"

        if ptype == "madonna_save":
            if self.prompt.get("to") != username:
                return False, "不是你的【圣母】"
            if act in {"madonna_accept", "respond_accept", "wander_accept"}:
                self._apply_madonna(username, True)
                self.seq += 1
                return True, "发动圣母"
            if act in {"madonna_pass", "respond_pass", "pass", "wander_pass"}:
                self._apply_madonna(username, False)
                self.seq += 1
                return True, "放弃圣母"
            return False, "无效的圣母响应"

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
            "role_name": p["role_name"] if self.phase == "ended" else None,
            "skills_sealed": self._has_status(name, STATUS_SKILLS_SEALED),
            "shield": int(p.get("shield") or 0),
            "countdown": p.get("countdown"),
        }

    def snapshot_for(self, viewer: str) -> dict[str, Any]:
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
                "countdown": me.get("countdown") if me else None,
                "shield": int(me.get("shield") or 0) if me else 0,
                "flying_blade_ready": bool(
                    me
                    and skill_active(me, SKILL_FLYING_BLADE)
                    and not me.get("flying_blade_used")
                    and not me.get("next_damage_true")
                ),
                "ultimate_law_ready": bool(
                    me and has_ship(me, "ultimate_law") and not me.get("ultimate_law_used")
                ),
                "next_damage_true": bool(me.get("next_damage_true")) if me else False,
            },
        }
