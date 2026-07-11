import json
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
from fastapi import FastAPI, Request, WebSocket, WebSocketException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

BASE_DIR = Path(__file__).resolve().parent
USERS_FILE = BASE_DIR / "users.json"
LOGIN_PAGE = BASE_DIR / "static" / "login.html"
LOBBY_PAGE = BASE_DIR / "static" / "lobby.html"


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


def get_username_from_request(request: Request) -> str | None:
    username = request.headers.get("x-username") or request.cookies.get("username")
    return username.strip() if username else None


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path in {"/lobby", "/lobby/"}:
        username = get_username_from_request(request)
        if not username:
            return HTMLResponse(
                """
                <!DOCTYPE html>
                <html lang=\"zh-CN\">
                <head>
                    <meta charset=\"UTF-8\" />
                    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
                    <title>请先登录</title>
                    <style>
                        body { font-family: Arial, sans-serif; background: #050810; color: #fff; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
                        .box { text-align: center; padding: 32px; border-radius: 16px; background: rgba(26, 31, 53, 0.9); box-shadow: 0 10px 30px rgba(0,0,0,0.3); }
                        a { color: #00e5ff; text-decoration: none; }
                    </style>
                </head>
                <body>
                    <div class=\"box\">
                        <h2>请先登录</h2>
                        <p>你需要先登录才能进入大厅。</p>
                        <p><a href=\"/\">返回登录页</a></p>
                    </div>
                </body>
                </html>
                """,
                status_code=401,
            )
    response = await call_next(request)
    return response


@app.get("/")
async def root():
    if LOGIN_PAGE.exists():
        return FileResponse(LOGIN_PAGE)
    return JSONResponse({"message": "请在 static/login.html 中提供登录页"})


@app.post("/api/register")
async def register(request: Request):
    data = await request.json()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()

    if not username or not password:
        return JSONResponse({"success": False, "message": "用户名和密码不能为空"}, status_code=400)

    users = load_users()
    if username in users:
        return JSONResponse({"success": False, "message": "用户名已存在"}, status_code=409)

    users[username] = {
        "username": username,
        "password_hash": hash_password(password),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_users(users)
    return JSONResponse({"success": True, "message": "注册成功"})


@app.post("/api/login")
async def login(request: Request):
    data = await request.json()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()

    if not username or not password:
        return JSONResponse({"success": False, "message": "用户名和密码不能为空"}, status_code=400)

    users = load_users()
    user = users.get(username)
    if not user or not verify_password(password, user.get("password_hash", "")):
        return JSONResponse({"success": False, "message": "用户名或密码错误"}, status_code=401)

    return JSONResponse({"success": True, "message": "登录成功", "username": username})


@app.get("/lobby")
async def lobby():
    if LOBBY_PAGE.exists():
        return FileResponse(LOBBY_PAGE)
    return JSONResponse({"error": "找不到大厅页面"}, status_code=404)


@app.websocket("/ws/{player_name}")
async def websocket_handler(websocket: WebSocket, player_name: str):
    username = (websocket.headers.get("x-username") or websocket.cookies.get("username") or "").strip()
    if not username:
        raise WebSocketException(code=1008, reason="请先登录")

    if username != player_name:
        raise WebSocketException(code=1008, reason="用户名与登录身份不一致")

    await websocket.accept()
    print(f"✅ {player_name} 已连接")
    try:
        while True:
            data = await websocket.receive_text()
            print(f"📩 收到 {player_name}: {data}")
            await websocket.send_text(f"服务器已收到你的消息：{data}")
    except Exception:
        print(f"❌ {player_name} 已断开")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)