"""WebSocket connection hub and JSON message helpers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import WebSocket

# #region agent log
_DEBUG_LOG_SESSION = Path(__file__).resolve().parent / "debug-2bc8fb.log"


def _debug_session_log(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    try:
        payload = {
            "sessionId": "2bc8fb",
            "runId": "offline-skip",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "timestamp": int(time.time() * 1000),
        }
        with _DEBUG_LOG_SESSION.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


# #endregion


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

    def disconnect(self, username: str, websocket: WebSocket | None = None) -> bool:
        current = self._by_user.get(username)
        if websocket is not None and current is not websocket:
            return False
        if current is not None:
            del self._by_user[username]
            return True
        return False

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
            removed = self.disconnect(username, ws)
            # #region agent log
            _debug_session_log(
                "H_A",
                "ws_hub.py:send_to",
                "send failed, socket cleared without offline handler",
                {
                    "username": username,
                    "removed_active": removed,
                    "msg_type": message.get("type"),
                    "still_online": self.online(username),
                    "mapped_room": self.get_user_room(username),
                },
            )
            # #endregion

    async def broadcast_room(self, room_id: str, usernames: list[str], message: dict[str, Any]) -> None:
        for name in usernames:
            await self.send_to(name, message)


ws_hub = ConnectionManager()
