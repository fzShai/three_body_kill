"""三体杀 FastAPI entry: auth, rooms, websocket, pages."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import auth
from rooms import HOST_TRANSFER_SECONDS, room_manager
from ws_hub import make_message, ws_hub

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
LOGIN_PAGE = STATIC_DIR / "login.html"
LOBBY_PAGE = STATIC_DIR / "lobby.html"
BATTLE_PAGE = STATIC_DIR / "battle.html"
CODEX_PAGE = STATIC_DIR / "codex.html"
ABOUT_PAGE = STATIC_DIR / "about.html"
ROOM_PAGE = STATIC_DIR / "room.html"
TABLE_PAGE = STATIC_DIR / "table.html"
ROLES_DATA = DATA_DIR / "roles.json"
CARDS_DATA = DATA_DIR / "cards.json"

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    poller = asyncio.create_task(_host_transfer_poller())
    yield
    poller.cancel()
    try:
        await poller
    except asyncio.CancelledError:
        pass


app = FastAPI(title="三体杀", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

PROTECTED_PREFIXES = ("/lobby", "/battle", "/codex", "/about", "/room", "/table", "/api/rooms", "/api/codex")

_host_transfer_deadlines: dict[str, float] = {}
ROOM_OFFLINE_GRACE_SECONDS = 3.0
_room_cleanup_deadlines: dict[str, float] = {}


def _cancel_host_transfer(room_id: str) -> None:
    _host_transfer_deadlines.pop(room_id.upper(), None)


def _schedule_host_transfer(room_id: str) -> None:
    key = room_id.upper()
    deadline = time.monotonic() + HOST_TRANSFER_SECONDS
    _host_transfer_deadlines[key] = deadline


def _cancel_room_cleanup(room_id: str) -> None:
    _room_cleanup_deadlines.pop(room_id.upper(), None)


def _schedule_room_cleanup(room_id: str) -> None:
    key = room_id.upper()
    _room_cleanup_deadlines[key] = time.monotonic() + ROOM_OFFLINE_GRACE_SECONDS


async def _process_due_host_transfers() -> None:
    now = time.monotonic()
    due = [rid for rid, deadline in list(_host_transfer_deadlines.items()) if now >= deadline]
    for rid in due:
        _host_transfer_deadlines.pop(rid, None)
        room = room_manager.transfer_host_if_still_offline(rid)
        if room:
            await _broadcast_room_state(room)


async def _process_due_room_cleanups() -> None:
    now = time.monotonic()
    due = [rid for rid, deadline in list(_room_cleanup_deadlines.items()) if now >= deadline]
    for rid in due:
        _room_cleanup_deadlines.pop(rid, None)
        room = room_manager.get(rid)
        if not room:
            continue
        if not room_manager.all_players_offline(room):
            continue
        await _destroy_room_all_offline(rid)


async def _process_due_turn_timeouts() -> None:
    for room in list(room_manager._rooms.values()):
        if room.status != "playing" or not room.game:
            continue
        if not room.game.expire_turn_if_due():
            continue
        if room.game.phase == "ended":
            room.status = "ended"
        await _broadcast_game_state(room)
        if room.status == "ended":
            await _broadcast_room_state(room)


async def _host_transfer_poller() -> None:
    while True:
        await asyncio.sleep(0.25)
        await _process_due_host_transfers()
        await _process_due_room_cleanups()
        await _process_due_turn_timeouts()


async def _destroy_room_all_offline(room_id: str) -> None:
    key = room_id.upper()
    _cancel_host_transfer(key)
    _cancel_room_cleanup(key)
    names = room_manager.delete_room(key)
    for name in names:
        ws_hub.set_user_room(name, None)


async def _handle_player_online(username: str, room_id: str) -> None:
    room = room_manager.mark_player_online(room_id, username)
    if not room:
        return
    _cancel_room_cleanup(room_id)
    if room.host == username and room.status == "waiting":
        _cancel_host_transfer(room_id)
    room_manager.sync_game_online(room)
    if room.status == "playing" and room.game:
        await _broadcast_game_state(room)
    else:
        await _broadcast_room_state(room)


async def _handle_player_offline(username: str, room_id: str) -> None:
    room = room_manager.mark_player_offline(room_id, username)
    if not room:
        return

    was_host = room.host == username

    if room.status == "playing" and room.game:
        room_manager.sync_game_online(room)
        room.game.mark_disconnected(username)
        room.game.skip_current_if_offline(username)
        if room.game.phase == "ended":
            room.status = "ended"
    elif was_host and room.status == "waiting":
        _schedule_host_transfer(room_id)

    if room_manager.all_players_offline(room):
        # Waiting and playing both get a short grace window so page navigation
        # (room -> table) does not wipe the room when both sockets flip briefly.
        _schedule_room_cleanup(room_id)
        if room.status == "playing" and room.game:
            await _broadcast_game_state(room)
            await _broadcast_room_state(room)
        else:
            await _broadcast_room_state(room)
        return

    if room.status == "playing" and room.game:
        await _broadcast_game_state(room)
        await _broadcast_room_state(room)
    else:
        await _broadcast_room_state(room)


def _unauthorized_html() -> HTMLResponse:
    return HTMLResponse(
        """
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="UTF-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1.0" />
            <title>请先登录</title>
            <style>
                body { font-family: Arial, sans-serif; background: #050810; color: #fff; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
                .box { text-align: center; padding: 32px; border-radius: 16px; background: rgba(26, 31, 53, 0.9); }
                a { color: #00e5ff; text-decoration: none; }
            </style>
        </head>
        <body>
            <div class="box">
                <h2>请先登录</h2>
                <p>你需要先登录才能继续。</p>
                <p><a href="/">返回登录页</a></p>
            </div>
        </body>
        </html>
        """,
        status_code=401,
    )


def _path_needs_auth(path: str) -> bool:
    if path in {"/lobby", "/lobby/", "/battle", "/battle/", "/codex", "/codex/", "/about", "/about/"}:
        return True
    if path.startswith("/room") or path.startswith("/table"):
        return True
    if path.startswith("/api/rooms") or path.startswith("/api/codex"):
        return True
    return False


def _load_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if _path_needs_auth(request.url.path):
        username = auth.get_username_from_request(request)
        if not username:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"success": False, "message": "请先登录"}, status_code=401)
            return _unauthorized_html()
        request.state.username = username
    response = await call_next(request)
    return response


def _session_cookie_kwargs(token: str) -> dict[str, Any]:
    return {
        "key": "session",
        "value": token,
        "httponly": True,
        "samesite": "lax",
        "max_age": 60 * 60 * 24,
        "path": "/",
    }


async def _broadcast_room_state(room) -> None:
    if room is None:
        return
    names = [p.username for p in room.players]
    msg = make_message("room_state", room.to_public(), room_id=room.room_id)
    await ws_hub.broadcast_room(room.room_id, names, msg)


async def _broadcast_game_state(room) -> None:
    if room is None or room.game is None:
        return
    for p in room.players:
        snap = room.game.snapshot_for(p.username)
        await ws_hub.send_to(p.username, make_message("game_state", snap, room_id=room.room_id, seq=snap["seq"]))


@app.get("/")
async def root():
    if LOGIN_PAGE.exists():
        return FileResponse(LOGIN_PAGE)
    return JSONResponse({"message": "请在 static/login.html 中提供登录页"})


@app.post("/api/register")
async def register(request: Request):
    data = await request.json()
    ok, message, code = auth.register_user(str(data.get("username", "")), str(data.get("password", "")))
    return JSONResponse({"success": ok, "message": message}, status_code=code if not ok else 200)


@app.post("/api/login")
async def login(request: Request):
    data = await request.json()
    ok, message, token, code = auth.login_user(str(data.get("username", "")), str(data.get("password", "")))
    if not ok or not token:
        return JSONResponse({"success": False, "message": message}, status_code=code)
    username = str(data.get("username", "")).strip()
    resp = JSONResponse({"success": True, "message": message, "username": username})
    resp.set_cookie(**_session_cookie_kwargs(token))
    # Display name cookie (non-sensitive); session cookie is authoritative.
    # Cookie values must be latin-1, so percent-encode non-ASCII usernames.
    resp.set_cookie(
        key="username",
        value=quote(username, safe=""),
        httponly=False,
        samesite="lax",
        max_age=60 * 60 * 24,
        path="/",
    )
    return resp


@app.post("/api/logout")
async def logout(request: Request):
    username = getattr(request.state, "username", None) or auth.get_username_from_request(request)
    if username:
        left_rooms = room_manager.leave_all(username)
        ws_hub.set_user_room(username, None)
        for rid in left_rooms:
            room = room_manager.get(rid)
            if room:
                await _broadcast_room_state(room)
    token = auth.get_session_token_from_request(request)
    auth.revoke_session(token)
    resp = JSONResponse({"success": True, "message": "已退出"})
    resp.delete_cookie("session", path="/")
    resp.delete_cookie("username", path="/")
    return resp


@app.get("/lobby")
async def lobby():
    if LOBBY_PAGE.exists():
        return FileResponse(LOBBY_PAGE)
    return JSONResponse({"error": "找不到大厅页面"}, status_code=404)


@app.get("/battle")
async def battle():
    if BATTLE_PAGE.exists():
        return FileResponse(BATTLE_PAGE)
    return JSONResponse({"error": "找不到对战页面"}, status_code=404)


@app.get("/codex")
async def codex():
    if CODEX_PAGE.exists():
        return FileResponse(CODEX_PAGE)
    return JSONResponse({"error": "找不到图鉴页面"}, status_code=404)


@app.get("/about")
async def about():
    if ABOUT_PAGE.exists():
        return FileResponse(ABOUT_PAGE)
    return JSONResponse({"error": "找不到关于页面"}, status_code=404)


@app.get("/api/codex/roles")
async def api_codex_roles():
    data = _load_json_file(ROLES_DATA)
    if data is None:
        return JSONResponse({"success": False, "message": "找不到角色数据"}, status_code=404)
    return JSONResponse(data)


@app.get("/api/codex/cards")
async def api_codex_cards():
    data = _load_json_file(CARDS_DATA)
    if data is None:
        return JSONResponse({"success": False, "message": "找不到卡牌数据"}, status_code=404)
    return JSONResponse(data)


@app.get("/room")
@app.get("/room/{room_id}")
async def room_page(room_id: str | None = None):
    if ROOM_PAGE.exists():
        return FileResponse(ROOM_PAGE)
    return JSONResponse({"error": "找不到房间页面"}, status_code=404)


@app.get("/table")
@app.get("/table/{room_id}")
async def table_page(room_id: str | None = None):
    if TABLE_PAGE.exists():
        return FileResponse(TABLE_PAGE)
    return JSONResponse({"error": "找不到桌面页面"}, status_code=404)


@app.post("/api/rooms/{room_id}/leave")
async def leave_room_api(room_id: str, request: Request):
    username = getattr(request.state, "username", None) or auth.get_username_from_request(request)
    if not username:
        return JSONResponse({"success": False, "message": "请先登录"}, status_code=401)
    room = room_manager.leave_room(room_id, username)
    _cancel_host_transfer(room_id)
    ws_hub.set_user_room(username, None)
    if room:
        await _broadcast_room_state(room)
    return JSONResponse({"success": True, "room_id": room_id.upper()})


@app.get("/api/rooms")
async def list_rooms():
    rooms = room_manager.list_rooms()
    return JSONResponse({"success": True, "rooms": rooms})


@app.post("/api/rooms")
async def create_room(request: Request):
    username = getattr(request.state, "username", None) or auth.get_username_from_request(request)
    if not username:
        return JSONResponse({"success": False, "message": "请先登录"}, status_code=401)
    room = room_manager.create_room(username)
    _cancel_room_cleanup(room.room_id)
    ws_hub.set_user_room(username, room.room_id)
    await _broadcast_room_state(room)
    return JSONResponse({"success": True, "room": room.to_public()})


@app.post("/api/rooms/{room_id}/join")
async def join_room_api(room_id: str, request: Request):
    username = getattr(request.state, "username", None) or auth.get_username_from_request(request)
    if not username:
        return JSONResponse({"success": False, "message": "请先登录"}, status_code=401)
    room, err = room_manager.join_room(room_id, username)
    if err or not room:
        return JSONResponse({"success": False, "message": err or "加入失败"}, status_code=400)
    _cancel_room_cleanup(room.room_id)
    ws_hub.set_user_room(username, room.room_id)
    await _broadcast_room_state(room)
    return JSONResponse({"success": True, "room": room.to_public()})


@app.get("/api/rooms/{room_id}")
async def get_room(room_id: str):
    room = room_manager.get(room_id)
    if not room:
        return JSONResponse({"success": False, "message": "房间不存在"}, status_code=404)
    return JSONResponse({"success": True, "room": room.to_public()})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    username = auth.get_username_from_websocket(websocket)
    if not username:
        await websocket.close(code=1008, reason="请先登录")
        return

    await ws_hub.connect(username, websocket)
    await ws_hub.send_to(username, make_message("hello", {"username": username}))

    # Re-sync room if already seated
    rid = ws_hub.get_user_room(username)
    if rid:
        room = room_manager.get(rid)
        if room and room.find_player(username):
            await _handle_player_online(username, rid)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws_hub.send_to(username, make_message("error", {"message": "无效 JSON"}))
                continue
            await _handle_ws_message(username, data)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        ws_hub.disconnect(username, websocket)
        rid = ws_hub.get_user_room(username)
        # Mark offline only when no replacement connection exists.
        # Covers both active closes and send_to() clearing a dead socket first.
        if rid and not ws_hub.online(username):
            await _handle_player_offline(username, rid)
        print(f"[ws] {username} disconnected")


async def _handle_ws_message(username: str, data: dict[str, Any]) -> None:
    msg_type = str(data.get("type", "")).strip()
    payload = data.get("payload") or {}
    room_id = (data.get("room_id") or payload.get("room_id") or ws_hub.get_user_room(username) or "").upper()

    if msg_type == "ping":
        await ws_hub.send_to(username, make_message("pong", {}))
        return

    if msg_type == "join_room":
        rid = str(payload.get("room_id") or room_id).upper()
        room, err = room_manager.join_room(rid, username)
        if err or not room:
            await ws_hub.send_to(username, make_message("error", {"message": err or "加入失败"}))
            return
        ws_hub.set_user_room(username, room.room_id)
        await _broadcast_room_state(room)
        return

    if msg_type == "leave_room":
        rid = str(payload.get("room_id") or room_id).upper()
        _cancel_host_transfer(rid)
        room = room_manager.leave_room(rid, username)
        ws_hub.set_user_room(username, None)
        await ws_hub.send_to(username, make_message("left_room", {"room_id": rid}))
        if room:
            await _broadcast_room_state(room)
        return

    if msg_type == "set_ready":
        ready = bool(payload.get("ready", True))
        room, err = room_manager.set_ready(room_id, username, ready)
        if err or not room:
            await ws_hub.send_to(username, make_message("error", {"message": err or "失败"}))
            return
        await _broadcast_room_state(room)
        return

    if msg_type == "kick":
        target = str(payload.get("target", "")).strip()
        room, err = room_manager.kick(room_id, username, target)
        if err:
            await ws_hub.send_to(username, make_message("error", {"message": err}))
            return
        ws_hub.set_user_room(target, None)
        await ws_hub.send_to(target, make_message("kicked", {"room_id": room_id}))
        if room:
            await _broadcast_room_state(room)
        return

    if msg_type == "start_game":
        room, err = room_manager.start_game(room_id, username)
        if err or not room:
            await ws_hub.send_to(username, make_message("error", {"message": err or "无法开始"}))
            return
        await _broadcast_room_state(room)
        await ws_hub.broadcast_room(
            room.room_id,
            [p.username for p in room.players],
            make_message("game_started", {"room_id": room.room_id}, room_id=room.room_id),
        )
        await _broadcast_game_state(room)
        return

    if msg_type == "get_room_state":
        room = room_manager.get(room_id)
        if not room:
            await ws_hub.send_to(username, make_message("error", {"message": "房间不存在"}))
            return
        await ws_hub.send_to(username, make_message("room_state", room.to_public(), room_id=room.room_id))
        return

    if msg_type == "get_game_state":
        room = room_manager.get(room_id)
        if not room or not room.game:
            await ws_hub.send_to(username, make_message("error", {"message": "对局不存在"}))
            return
        snap = room.game.snapshot_for(username)
        await ws_hub.send_to(username, make_message("game_state", snap, room_id=room.room_id, seq=snap["seq"]))
        return

    if msg_type == "game_action":
        room = room_manager.get(room_id)
        if not room or not room.game:
            await ws_hub.send_to(username, make_message("error", {"message": "对局不存在"}))
            return
        ok, message = room.game.apply_action(username, payload)
        if not ok:
            await ws_hub.send_to(username, make_message("error", {"message": message}))
            return
        if room.game.phase == "ended":
            room.status = "ended"
        await _broadcast_game_state(room)
        if room.status == "ended":
            await _broadcast_room_state(room)
        return

    await ws_hub.send_to(username, make_message("error", {"message": f"未知消息类型: {msg_type}"}))


# Back-compat: old echo path redirects mentality — close with hint
@app.websocket("/ws/{player_name}")
async def websocket_legacy(websocket: WebSocket, player_name: str):
    await websocket.accept()
    await websocket.send_text(json.dumps({"type": "error", "payload": {"message": "请改用 /ws 并携带 session cookie"}}, ensure_ascii=False))
    await websocket.close(code=1008, reason="use /ws")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
