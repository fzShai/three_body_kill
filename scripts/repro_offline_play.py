"""Reproduce: can the remaining online player still act after one goes offline?"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from game.engine import GameSession
from server import app, room_manager, ws_hub

LOG = ROOT / "debug-2b39ab.log"


def _log(msg: str, data: dict) -> None:
    payload = {
        "sessionId": "2b39ab",
        "runId": "offline-play",
        "hypothesisId": "R",
        "location": "scripts/repro_offline_play.py",
        "message": msg,
        "data": data,
        "timestamp": 0,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    print(msg, data)


def case_engine_direct() -> None:
    """Unit-level: disconnect current player, remaining player ends turn."""
    g = GameSession.create("TEST01", ["p1", "p2"])
    first = g.current_player()
    other = "p2" if first == "p1" else "p1"
    g.mark_disconnected(first)
    skipped = g.skip_current_if_offline(first)
    _log(
        "engine_after_offline",
        {
            "first": first,
            "other": other,
            "skipped": skipped,
            "phase": g.phase,
            "current": g.current_player() if g.phase != "ended" else None,
            "online": dict(g.player_online),
        },
    )
    ok, msg = g.apply_action(other, {"action": "pass"})
    _log("engine_other_pass", {"ok": ok, "message": msg, "phase": g.phase, "current": g.current_player() if g.phase != "ended" else None})
    assert skipped, "expected turn skip for offline current"
    assert g.phase != "ended", f"game should not end, phase={g.phase}"
    assert ok, f"online player should be able to act, got: {msg}"


def case_ws_flow() -> None:
    """HTTP/WS: offline current player; remaining player sends game_action."""
    with TestClient(app) as c1, TestClient(app) as c2:
        for u, c in (("op1", c1), ("op2", c2)):
            c.post("/api/register", json={"username": u, "password": "x"})
            c.post("/api/login", json={"username": u, "password": "x"})
        r = c1.post("/api/rooms")
        rid = r.json()["room"]["room_id"]
        c2.post(f"/api/rooms/{rid}/join")
        room_manager.set_ready(rid, "op2", True)
        room, err = room_manager.start_game(rid, "op1")
        assert not err and room and room.game
        first = room.game.current_player()
        other = "op2" if first == "op1" else "op1"
        other_client = c2 if other == "op2" else c1
        first_client = c1 if first == "op1" else c2

        ws_hub.set_user_room("op1", rid)
        ws_hub.set_user_room("op2", rid)

        # Keep other online; disconnect first
        with other_client.websocket_connect("/ws") as ws_other:
            with first_client.websocket_connect("/ws") as ws_first:
                pass  # first disconnects here

            room = room_manager.get(rid)
            assert room and room.game
            _log(
                "ws_after_first_offline",
                {
                    "first": first,
                    "other": other,
                    "phase": room.game.phase,
                    "current": room.game.current_player() if room.game.phase != "ended" else None,
                    "online": dict(room.game.player_online),
                    "room_status": room.status,
                },
            )
            ws_other.send_text(
                json.dumps(
                    {
                        "type": "game_action",
                        "room_id": rid,
                        "payload": {"action": "pass"},
                    }
                )
            )
            # drain a few messages looking for error / game_state
            got_error = None
            for _ in range(8):
                try:
                    raw = ws_other.receive_text()
                except Exception as e:
                    _log("ws_receive_fail", {"error": str(e)})
                    break
                msg = json.loads(raw)
                if msg.get("type") == "error":
                    got_error = msg.get("payload", {}).get("message")
                    break
                if msg.get("type") == "game_state":
                    snap = msg.get("payload") or {}
                    _log(
                        "ws_game_state_after_pass",
                        {
                            "phase": snap.get("phase"),
                            "current": snap.get("current_player"),
                            "seq": snap.get("seq"),
                        },
                    )
                    got_error = False
                    break

            room = room_manager.get(rid)
            _log(
                "ws_final",
                {
                    "got_error": got_error,
                    "phase": room.game.phase if room and room.game else None,
                    "current": room.game.current_player() if room and room.game and room.game.phase != "ended" else None,
                    "room_exists": room is not None,
                },
            )
            assert room and room.game and room.game.phase != "ended", "game ended after one offline"
            assert got_error is False or got_error is None, f"online player blocked: {got_error}"


def main() -> None:
    if LOG.exists():
        LOG.unlink()
    case_engine_direct()
    case_ws_flow()
    print("REPRO_DONE")


if __name__ == "__main__":
    main()
