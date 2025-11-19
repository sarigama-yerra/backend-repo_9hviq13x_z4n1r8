import os
import ssl
import asyncio
import json
from typing import List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# WebSocket Connection Manager
# -----------------------------
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        data = json.dumps(message)
        to_remove = []
        for connection in list(self.active_connections):
            try:
                await connection.send_text(data)
            except Exception:
                to_remove.append(connection)
        for c in to_remove:
            self.disconnect(c)


manager = ConnectionManager()

# -----------------------------
# Roulette Logic
# -----------------------------
RED_NUMBERS = {
    1, 3, 5, 7, 9,
    12, 14, 16, 18,
    19, 21, 23, 25, 27,
    30, 32, 34, 36,
}


def spin_roulette():
    import random
    number = random.randint(0, 36)
    color = "green" if number == 0 else ("red" if number in RED_NUMBERS else "black")
    return {"number": number, "color": color}


# -----------------------------
# Twitch IRC Client (async)
# -----------------------------
class TwitchConfig(BaseModel):
    nick: str
    token: str  # OAuth token. Format: oauth:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    channel: str  # channel name without '#'


class TwitchClient:
    def __init__(self):
        self.config: Optional[TwitchConfig] = None
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.task: Optional[asyncio.Task] = None
        self.connected: bool = False

    async def start(self, config: TwitchConfig):
        self.config = config
        if self.task and not self.task.done():
            # Already running; restart
            await self.stop()
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        try:
            if self.writer:
                self.writer.close()
                try:
                    await self.writer.wait_closed()
                except Exception:
                    pass
            if self.task:
                self.task.cancel()
                try:
                    await self.task
                except Exception:
                    pass
        finally:
            self.reader = None
            self.writer = None
            self.task = None
            self.connected = False

    async def _send(self, line: str):
        if self.writer is None:
            return
        self.writer.write((line + "\r\n").encode("utf-8"))
        await self.writer.drain()

    async def _run(self):
        if not self.config:
            return
        host = "irc.chat.twitch.tv"
        port = 6697  # TLS
        try:
            ssl_ctx = ssl.create_default_context()
            reader, writer = await asyncio.open_connection(host, port, ssl=ssl_ctx)
            self.reader, self.writer = reader, writer

            await self._send(f"PASS {self.config.token}")
            await self._send(f"NICK {self.config.nick}")
            await self._send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await self._send(f"JOIN #{self.config.channel}")

            self.connected = True
            await manager.broadcast({
                "type": "twitch_status",
                "status": "connected",
                "channel": self.config.channel,
                "nick": self.config.nick,
            })

            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = line.decode("utf-8", errors="ignore").strip()

                # Respond to PINGs to keep connection alive
                if msg.startswith("PING"):
                    await self._send("PONG :tmi.twitch.tv")
                    continue

                # Parse PRIVMSG and look for !bet
                if "PRIVMSG" in msg:
                    try:
                        # Extract username and message content
                        prefix_end = msg.find("!")
                        username = msg[1:prefix_end] if prefix_end > 1 else "unknown"
                        trailing_idx = msg.find(" :")
                        content = msg[trailing_idx + 2:] if trailing_idx != -1 else ""
                    except Exception:
                        username, content = "unknown", ""

                    await manager.broadcast({
                        "type": "chat",
                        "user": username,
                        "message": content,
                    })

                    if content.lower().startswith("!bet"):
                        # Trigger a spin and broadcast result
                        result = spin_roulette()
                        await manager.broadcast({
                            "type": "spin",
                            "trigger": {"source": "twitch", "user": username},
                            "result": result,
                        })
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await manager.broadcast({
                "type": "twitch_status",
                "status": "error",
                "message": str(e),
            })
        finally:
            self.connected = False
            await manager.broadcast({
                "type": "twitch_status",
                "status": "disconnected",
            })


twitch_client = TwitchClient()


# -----------------------------
# API Routes
# -----------------------------
@app.get("/")
def read_root():
    return {"message": "Roulette backend running"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/test")
def test_database():
    """Simple health endpoint"""
    return {"backend": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send initial state
        await manager.broadcast({
            "type": "welcome",
            "message": "Connected to roulette server",
        })
        while True:
            # We don't expect messages from clients, but keep the socket open
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


@app.post("/spin")
async def manual_spin():
    result = spin_roulette()
    await manager.broadcast({
        "type": "spin",
        "trigger": {"source": "manual"},
        "result": result,
    })
    return result


@app.post("/twitch/config")
async def configure_twitch(cfg: TwitchConfig):
    # Basic validation
    if not cfg.token.startswith("oauth:"):
        raise HTTPException(status_code=400, detail="Token must start with 'oauth:'")
    await twitch_client.start(cfg)
    return {"status": "starting", "channel": cfg.channel}


@app.get("/twitch/status")
async def twitch_status():
    status = "connected" if twitch_client.connected else "disconnected"
    channel = twitch_client.config.channel if twitch_client.config else None
    return {"status": status, "channel": channel}


# Optional: load config from env on startup
@app.on_event("startup")
async def startup_event():
    nick = os.getenv("TWITCH_NICK")
    token = os.getenv("TWITCH_OAUTH_TOKEN")
    channel = os.getenv("TWITCH_CHANNEL")
    if nick and token and channel:
        try:
            await twitch_client.start(TwitchConfig(nick=nick, token=token, channel=channel))
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
