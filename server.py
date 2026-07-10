from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

app = FastAPI()

@app.get("/")
async def root():
    return HTMLResponse("""
    <h1>🌌 三体杀 服务器已启动</h1>
    <p>当前功能：WebSocket 测试</p>
    <p>连接地址：<code>ws://124.222.186.12:8000/ws</code></p>
    """)

@app.websocket("/ws/{player_name}")
async def websocket_handler(websocket: WebSocket, player_name: str):
    await websocket.accept()
    print(f"✅ {player_name} 已连接")
    try:
        while True:
            data = await websocket.receive_text()
            print(f"📩 收到 {player_name}: {data}")
            await websocket.send_text(f"服务器已收到你的消息：{data}")
    except:
        print(f"❌ {player_name} 已断开")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)