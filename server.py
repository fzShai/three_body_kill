import json
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

BASE_DIR = Path(__file__).resolve().parent
USERS_FILE = BASE_DIR / "users.json"
LOGIN_PAGE = BASE_DIR / "static" / "login.html"


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


@app.websocket("/ws/{player_name}")
async def websocket_handler(websocket: WebSocket, player_name: str):
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