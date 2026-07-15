"""User store and session token auth (prototype, in-memory sessions)."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
from fastapi import Request, WebSocket

BASE_DIR = Path(__file__).resolve().parent
USERS_FILE = BASE_DIR / "users.json"

# token -> username
_sessions: dict[str, str] = {}


def load_users() -> dict:
    if not USERS_FILE.exists():
        USERS_FILE.write_text("{}", encoding="utf-8")
        return {}
    with USERS_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def save_users(users: dict) -> None:
    with USERS_FILE.open("w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_session(username: str) -> str:
    # Drop older tokens for same user (single active session)
    for tok, user in list(_sessions.items()):
        if user == username:
            del _sessions[tok]
    token = secrets.token_urlsafe(32)
    _sessions[token] = username
    return token


def revoke_session(token: str | None) -> None:
    if token and token in _sessions:
        del _sessions[token]


def username_from_token(token: str | None) -> str | None:
    if not token:
        return None
    return _sessions.get(token)


def get_session_token_from_request(request: Request) -> str | None:
    return (request.cookies.get("session") or request.headers.get("x-session") or "").strip() or None


def get_username_from_request(request: Request) -> str | None:
    token = get_session_token_from_request(request)
    user = username_from_token(token)
    if user:
        return user
    # Legacy fallback during transition: only if session missing
    return None


def get_username_from_websocket(websocket: WebSocket) -> str | None:
    token = (websocket.cookies.get("session") or "").strip()
    if not token:
        # Some clients pass via query
        token = (websocket.query_params.get("session") or "").strip()
    return username_from_token(token) if token else None


def register_user(username: str, password: str) -> tuple[bool, str, int]:
    username = username.strip()
    password = password.strip()
    if not username or not password:
        return False, "用户名和密码不能为空", 400
    users = load_users()
    if username in users:
        return False, "用户名已存在", 409
    users[username] = {
        "username": username,
        "password_hash": hash_password(password),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_users(users)
    return True, "注册成功", 200


def login_user(username: str, password: str) -> tuple[bool, str, str | None, int]:
    username = username.strip()
    password = password.strip()
    if not username or not password:
        return False, "用户名和密码不能为空", None, 400
    users = load_users()
    user = users.get(username)
    if not user or not verify_password(password, user.get("password_hash", "")):
        return False, "用户名或密码错误", None, 401
    token = create_session(username)
    return True, "登录成功", token, 200
