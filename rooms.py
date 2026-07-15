"""In-memory room manager."""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from game.engine import GameSession

MAX_PLAYERS = 6
MIN_PLAYERS_TO_START = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_room_id() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


@dataclass
class PlayerSeat:
    username: str
    seat: int
    ready: bool = False
    connected: bool = True

    def to_public(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "seat": self.seat,
            "ready": self.ready,
            "connected": self.connected,
        }


@dataclass
class Room:
    room_id: str
    host: str
    max_players: int = MAX_PLAYERS
    status: str = "waiting"  # waiting | playing | ended
    created_at: str = field(default_factory=_now)
    players: list[PlayerSeat] = field(default_factory=list)
    game: GameSession | None = None

    def find_player(self, username: str) -> PlayerSeat | None:
        for p in self.players:
            if p.username == username:
                return p
        return None

    def to_public(self) -> dict[str, Any]:
        return {
            "room_id": self.room_id,
            "host": self.host,
            "status": self.status,
            "max_players": self.max_players,
            "player_count": len(self.players),
            "created_at": self.created_at,
            "players": [p.to_public() for p in sorted(self.players, key=lambda x: x.seat)],
            "can_join": self.status == "waiting" and len(self.players) < self.max_players,
            "min_players_to_start": MIN_PLAYERS_TO_START,
        }

    def to_list_item(self) -> dict[str, Any]:
        return {
            "room_id": self.room_id,
            "host": self.host,
            "status": self.status,
            "player_count": len(self.players),
            "max_players": self.max_players,
            "can_join": self.status == "waiting" and len(self.players) < self.max_players,
        }


class RoomManager:
    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}

    def create_room(self, host: str) -> Room:
        # Leave previous waiting rooms as player
        self.leave_all(host)

        for _ in range(20):
            rid = _gen_room_id()
            if rid not in self._rooms:
                break
        else:
            rid = secrets.token_hex(4).upper()

        room = Room(room_id=rid, host=host)
        room.players.append(PlayerSeat(username=host, seat=0, ready=False))
        self._rooms[rid] = room
        return room

    def get(self, room_id: str) -> Room | None:
        return self._rooms.get(room_id.upper())

    def list_rooms(self) -> list[dict[str, Any]]:
        items = [r.to_list_item() for r in self._rooms.values() if r.status != "ended"]
        items.sort(key=lambda x: x["room_id"])
        return items

    def join_room(self, room_id: str, username: str) -> tuple[Room | None, str | None]:
        room = self.get(room_id)
        if not room:
            return None, "房间不存在"
        existing = room.find_player(username)
        if existing:
            existing.connected = True
            return room, None
        if room.status != "waiting":
            return None, "对局已开始，无法加入"
        if len(room.players) >= room.max_players:
            return None, "房间已满"

        self.leave_all(username)
        used = {p.seat for p in room.players}
        seat = next(i for i in range(room.max_players) if i not in used)
        room.players.append(PlayerSeat(username=username, seat=seat, ready=False))
        return room, None

    def leave_room(self, room_id: str, username: str) -> Room | None:
        room = self.get(room_id)
        if not room:
            return None
        room.players = [p for p in room.players if p.username != username]
        if not room.players:
            if room_id.upper() in self._rooms:
                del self._rooms[room_id.upper()]
            return None
        if room.host == username:
            room.host = sorted(room.players, key=lambda p: p.seat)[0].username
            for p in room.players:
                p.ready = False
        if room.status == "playing" and room.game:
            room.game.mark_disconnected(username)
        return room

    def leave_all(self, username: str) -> list[str]:
        affected: list[str] = []
        for rid in list(self._rooms.keys()):
            room = self._rooms.get(rid)
            if not room:
                continue
            if room.find_player(username):
                self.leave_room(rid, username)
                affected.append(rid)
        return affected

    def set_ready(self, room_id: str, username: str, ready: bool) -> tuple[Room | None, str | None]:
        room = self.get(room_id)
        if not room:
            return None, "房间不存在"
        if room.status != "waiting":
            return None, "对局已开始"
        player = room.find_player(username)
        if not player:
            return None, "你不在该房间"
        player.ready = ready
        return room, None

    def kick(self, room_id: str, host: str, target: str) -> tuple[Room | None, str | None]:
        room = self.get(room_id)
        if not room:
            return None, "房间不存在"
        if room.host != host:
            return None, "只有房主可以踢人"
        if target == host:
            return None, "不能踢出自己"
        if not room.find_player(target):
            return None, "目标玩家不在房间"
        return self.leave_room(room_id, target), None

    def start_game(self, room_id: str, username: str) -> tuple[Room | None, str | None]:
        room = self.get(room_id)
        if not room:
            return None, "房间不存在"
        if room.host != username:
            return None, "只有房主可以开始游戏"
        if room.status != "waiting":
            return None, "对局已开始或已结束"
        if len(room.players) < MIN_PLAYERS_TO_START:
            return None, f"至少需要 {MIN_PLAYERS_TO_START} 名玩家"
        not_ready = [p.username for p in room.players if not p.ready and p.username != room.host]
        # Host auto-counts as ready on start; others must be ready
        for p in room.players:
            if p.username == room.host:
                p.ready = True
        not_ready = [p.username for p in room.players if not p.ready]
        if not_ready:
            return None, f"尚未准备: {', '.join(not_ready)}"

        names = [p.username for p in sorted(room.players, key=lambda x: x.seat)]
        room.game = GameSession.create(room_id=room.room_id, player_names=names)
        room.status = "playing"
        return room, None


room_manager = RoomManager()
