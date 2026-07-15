"""三体杀 FastAPI entry: auth, rooms, websocket, pages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import auth
from rooms import room_manager
from ws_hub import make_message, ws_hub

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
LOGIN_PAGE = STATIC_DIR / "login.html"
LOBBY_PAGE = STATIC_DIR / "lobby.html"
ROOM_PAGE = STATIC_DIR / "room.html"
TABLE_PAGE = STATIC_DIR / "table.html"

app = FastAPI(title="三体杀")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

PROTECTED_PREFIXES = ("/lobby", "/room", "/table", "/api/rooms")

# #region agent log
_DEBUG_LOG = BASE_DIR / "debug-2b39ab.log"


def _agent_log(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    try:
        import time

        payload = {
            "sessionId": "2b39ab",
            "runId": "pre-fix",
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
    if path in {"/lobby", "/lobby/"}:
        return True
    if path.startswith("/room") or path.startswith("/table"):
        return True
    if path.startswith("/api/rooms"):
        return True
    return False


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if _path_needs_auth(request.url.path):
        cookie_keys = list(request.cookies.keys())
        has_session = bool(request.cookies.get("session"))
        has_username_cookie = bool(request.cookies.get("username"))
        token = auth.get_session_token_from_request(request)
        username = auth.get_username_from_request(request)
        # #region agent log
        _agent_log(
            "B",
            "server.py:auth_middleware",
            "auth check for protected path",
            {
                "path": request.url.path,
                "cookie_keys": cookie_keys,
                "has_session": has_session,
                "has_username_cookie": has_username_cookie,
                "token_prefix": (token[:8] if token else None),
                "username_resolved": username,
                "session_store_size": len(auth._sessions),
            },
        )
        # #endregion
        if not username:
            # #region agent log
            _agent_log(
                "D",
                "server.py:auth_middleware",
                "auth failed -> 401",
                {
                    "path": request.url.path,
                    "reason": "no_session" if not has_session else "token_not_in_store",
                    "has_username_cookie": has_username_cookie,
                },
            )
            # #endregion
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
        # #region agent log
        _agent_log("A", "server.py:login", "login failed", {"ok": ok, "code": code})
        # #endregion
        return JSONResponse({"success": False, "message": message}, status_code=code)
    username = str(data.get("username", "")).strip()
    resp = JSONResponse({"success": True, "message": message, "username": username})
    resp.set_cookie(**_session_cookie_kwargs(token))
    # Display name cookie (non-sensitive); session cookie is authoritative
    resp.set_cookie(key="username", value=username, httponly=False, samesite="lax", max_age=60 * 60 * 24, path="/")
    # #region agent log
    _agent_log(
        "A",
        "server.py:login",
        "login success, cookies set",
        {
            "username": username,
            "token_prefix": token[:8],
            "session_store_size": len(auth._sessions),
            "token_in_store": token in auth._sessions,
        },
    )
    # #endregion
    return resp


@app.post("/api/logout")
async def logout(request: Request):
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


@app.get("/api/rooms")
async def list_rooms():
    return JSONResponse({"success": True, "rooms": room_manager.list_rooms()})


@app.post("/api/rooms")
async def create_room(request: Request):
    username = getattr(request.state, "username", None) or auth.get_username_from_request(request)
    if not username:
        return JSONResponse({"success": False, "message": "请先登录"}, status_code=401)
    room = room_manager.create_room(username)
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
            p = room.find_player(username)
            if p:
                p.connected = True
            await _broadcast_room_state(room)
            if room.status == "playing" and room.game:
                await _broadcast_game_state(room)

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
        if rid:
            room = room_manager.get(rid)
            if room:
                player = room.find_player(username)
                if player:
                    player.connected = False
                if room.status == "playing" and room.game:
                    room.game.mark_disconnected(username)
                    await _broadcast_game_state(room)
                else:
                    await _broadcast_room_state(room)
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
