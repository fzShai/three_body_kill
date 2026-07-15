"""WebSocket connection hub and JSON message helpers."""

from __future__ import annotations

import json
from typing import Any

from fastapi import WebSocket


def make_message(msg_type: str, payload: dict[str, Any] | None = None, room_id: str | None = None, seq: int | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": msg_type, "payload": payload or {}}
    if room_id is not None:
        msg["room_id"] = room_id
    if seq is not None:
        msg["seq"] = seq
    return msg


class ConnectionManager:
    """One active WebSocket per username."""

    def __init__(self) -> None:
        self._by_user: dict[str, WebSocket] = {}
        self._user_room: dict[str, str] = {}  # username -> room_id

    async def connect(self, username: str, websocket: WebSocket) -> None:
        await websocket.accept()
        old = self._by_user.get(username)
        if old is not None and old is not websocket:
            try:
                await old.close(code=4000, reason="replaced by new connection")
            except Exception:
                pass
        self._by_user[username] = websocket

    def disconnect(self, username: str, websocket: WebSocket | None = None) -> None:
        current = self._by_user.get(username)
        if websocket is not None and current is not websocket:
            return
        if current is not None:
            del self._by_user[username]

    def set_user_room(self, username: str, room_id: str | None) -> None:
        if room_id is None:
            self._user_room.pop(username, None)
        else:
            self._user_room[username] = room_id.upper()

    def get_user_room(self, username: str) -> str | None:
        return self._user_room.get(username)

    def online(self, username: str) -> bool:
        return username in self._by_user

    async def send_to(self, username: str, message: dict[str, Any]) -> None:
        ws = self._by_user.get(username)
        if not ws:
            return
        try:
            await ws.send_text(json.dumps(message, ensure_ascii=False))
        except Exception:
            self.disconnect(username, ws)

    async def broadcast_room(self, room_id: str, usernames: list[str], message: dict[str, Any]) -> None:
        for name in usernames:
            await self.send_to(name, message)


ws_hub = ConnectionManager()
