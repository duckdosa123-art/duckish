#!/usr/bin/env python3
"""
Simulation test for the Duckish Lichess bot.

Spins up a mock Lichess Bot API server (http.server.ThreadingHTTPServer),
points LichessClient at it, runs a GameHandler against a scripted mock engine,
and verifies that the bot plays the expected moves.

No network access, no real engine and no real experience DB: everything is
local and scripted. Run from anywhere:

    python bridge/test_lichess_sim.py

Exit code 0 = all checks passed, 1 = at least one check failed.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

# Make the bridge package importable regardless of the caller's cwd.
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

import lichess.api as lichess_api
from lichess.api import LichessClient
from lichess.runner import GameHandler


# ---- Mock server state ------------------------------------------------------

class MockState:
    """Shared, thread-safe state for the mock Lichess server."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.posted_moves: List[Dict[str, str]] = []  # {"game_id": ..., "uci": ...}
        self.challenges: List[str] = []
        self.streams_served: List[str] = []

    def reset(self) -> None:
        with self.lock:
            self.posted_moves = []
            self.challenges = []
            self.streams_served = []


STATE = MockState()

# NDJSON events served for GET /api/bot/game/stream/{gameId}.
# The bot is White: it must move on the gameFull event (moves: ""), then on
# each of its turns in the gameState updates. The 3-move gameState leaves
# Black to move, so the bot must NOT move there (turn-check verification).
GAME_EVENTS: Dict[str, List[Dict[str, Any]]] = {
    "g1": [
        {
            "type": "gameFull",
            "id": "g1",
            "variant": "standard",
            "initialFen": "startpos",
            "state": {
                "type": "gameState",
                "moves": "",
                "wtime": 180000,
                "btime": 180000,
                "winc": 2000,
                "binc": 2000,
                "status": "started",
            },
            "white": {"id": "me", "name": "me", "rating": 2000},
            "black": {"id": "opp", "name": "Opponent", "rating": 1800},
        },
        {
            "type": "gameState",
            "moves": "e2e4 e7e5",
            "wtime": 179000,
            "btime": 179500,
            "winc": 2000,
            "binc": 2000,
            "status": "started",
        },
        {
            "type": "gameState",
            "moves": "e2e4 e7e5 g1f3",
            "wtime": 178500,
            "btime": 179000,
            "winc": 2000,
            "binc": 2000,
            "status": "started",
        },
        {
            "type": "gameState",
            "moves": "e2e4 e7e5 g1f3 b8c6",
            "wtime": 178000,
            "btime": 178500,
            "winc": 2000,
            "binc": 2000,
            "status": "started",
        },
        {"type": "gameFinish", "status": "resign", "winner": "black"},
    ]
}

ONLINE_BOTS = [
    {"id": "botA", "username": "BotA", "perfs": {"blitz": {"games": 100, "rating": 2000}}},
    {"id": "botB", "username": "BotB", "perfs": {"blitz": {"games": 50, "rating": 1900}}},
]

EVENT_STREAM_EVENTS = [
    {
        "type": "gameStart",
        "game": {
            "id": "g1",
            "color": "white",
            "opponent": {"id": "opp", "username": "Opponent", "rating": 1800},
        },
    }
]


class MockLichessHandler(BaseHTTPRequestHandler):
    """Serves a tiny subset of the Lichess Bot API over NDJSON/JSON."""

    protocol_version = "HTTP/1.0"  # one response per connection, then close

    def log_message(self, format, *args):  # noqa: A002 - match stdlib signature
        pass  # keep test output clean

    # -- helpers ---------------------------------------------------------------

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client hung up early (e.g. bot list closes after 1 bot)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        self._send_bytes(json.dumps(obj).encode("utf-8"), "application/json", status)

    def _send_ndjson(self, events: List[Dict[str, Any]], status: int = 200) -> None:
        body = "".join(json.dumps(e) + "\n" for e in events).encode("utf-8")
        self._send_bytes(body, "application/x-ndjson", status)

    # -- routes ----------------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/bot/online":
            self._send_ndjson(ONLINE_BOTS)
        elif path == "/api/stream/event":
            self._send_ndjson(EVENT_STREAM_EVENTS)
        elif path.startswith("/api/bot/game/stream/"):
            game_id = path.rsplit("/", 1)[-1]
            events = GAME_EVENTS.get(game_id)
            if events is None:
                self._send_json({"error": "no such game"}, status=404)
                return
            with STATE.lock:
                STATE.streams_served.append(game_id)
            self._send_ndjson(events)
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        # Drain any request body so the connection closes cleanly.
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)

        if path.startswith("/api/bot/game/") and "/move/" in path:
            rest = path[len("/api/bot/game/"):]
            game_id, uci = rest.split("/move/", 1)
            with STATE.lock:
                STATE.posted_moves.append({"game_id": game_id, "uci": uci})
            self._send_json({"ok": True})
        elif path.startswith("/api/challenge/"):
            with STATE.lock:
                STATE.challenges.append(path[len("/api/challenge/"):])
            self._send_json(
                {
                    "challenge": {
                        "id": "chal123",
                        "status": "created",
                        "variant": {"key": "standard", "name": "Standard"},
                        "rated": True,
                        "timeControl": {"type": "clock", "limit": 180, "increment": 2},
                        "challenger": {"id": "me", "name": "me"},
                    }
                }
            )
        else:
            self._send_json({"error": "not found"}, status=404)


# ---- Mock engine / experience ------------------------------------------------

class MockEngine:
    """Scripted UCI engine: returns a fixed move for each known position."""

    def __init__(self, script: Dict[Tuple[str, ...], str]) -> None:
        self.script = script
        self.calls: List[Dict[str, Any]] = []

    def best_move(
        self,
        moves_uci: List[str],
        wtime: Optional[int] = None,
        btime: Optional[int] = None,
        winc: Optional[int] = None,
        binc: Optional[int] = None,
        movetime: Optional[int] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        self.calls.append(
            {
                "moves": list(moves_uci),
                "wtime": wtime,
                "btime": btime,
                "winc": winc,
                "binc": binc,
            }
        )
        return self.script.get(tuple(moves_uci))


class MockExperience:
    """No-op experience store that records what the handler would persist."""

    def __init__(self) -> None:
        self.positions: List[Tuple[str, Any, str]] = []
        self.moves: List[Tuple[str, str, float, float]] = []

    def record_position(self, fen: str, eval_cp: Any, result: str) -> None:
        self.positions.append((fen, eval_cp, result))

    def record_move(
        self,
        fen: str,
        move_uci: str,
        penalty_cp: float = 0.0,
        bonus_cp: float = 0.0,
    ) -> None:
        self.moves.append((fen, move_uci, penalty_cp, bonus_cp))


# ---- Test harness ------------------------------------------------------------

FAILURES: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    status = "PASS" if cond else "FAIL"
    suffix = f" ({detail})" if detail else ""
    print(f"[{status}] {name}{suffix}", flush=True)
    if not cond:
        FAILURES.append(name)
    return cond


def section(name: str, fn) -> None:
    """Run one test section; a raised exception is recorded as a failure."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - report and keep going
        check(name, False, f"raised {type(e).__name__}: {e}")


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockLichessHandler)
    host, port = server.server_address
    base_url = f"http://{host}:{port}/api"
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    original_api = lichess_api.LICHESS_API
    lichess_api.LICHESS_API = base_url  # point the client at the mock server

    try:
        client = LichessClient("test-token")

        def test_bot_list() -> None:
            STATE.reset()
            bots = client.get_online_bots(own_username="me")
            names = [b.get("username") or b.get("id") for b in bots]
            check("bot list: get_online_bots returns bots", len(bots) >= 1, f"got {names}")

        def test_challenge() -> None:
            STATE.reset()
            result = client.challenge_user("BotA")
            chal = result.get("challenge", {}) if isinstance(result, dict) else {}
            check(
                "challenge: challenge_user returns challenge id",
                isinstance(chal, dict) and chal.get("id") == "chal123",
                f"got {result}",
            )
            check(
                "challenge: POST /api/challenge/{username} was called",
                STATE.challenges == ["BotA"],
                f"got {STATE.challenges}",
            )

        def test_event_stream() -> None:
            resp = client.stream_events()
            first_line = ""
            try:
                for raw in resp.iter_lines():
                    if raw:
                        first_line = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
                        break
            finally:
                resp.close()
            first_event = json.loads(first_line) if first_line else {}
            check(
                "event stream: GET /api/stream/event yields NDJSON event",
                first_event.get("type") == "gameStart",
                f"got {first_event}",
            )

        def test_game_flow() -> None:
            STATE.reset()
            engine = MockEngine(
                {
                    (): "e2e4",
                    ("e2e4", "e7e5"): "g1f3",
                    ("e2e4", "e7e5", "g1f3", "b8c6"): "f1b5",
                }
            )
            experience = MockExperience()
            handler = GameHandler(client, engine, experience, "g1", "white")
            handler._thread.join(timeout=15)

            posted = [m["uci"] for m in STATE.posted_moves]
            check("game: stream finished", not handler._thread.is_alive())
            check("game: stream endpoint served g1", STATE.streams_served == ["g1"], f"got {STATE.streams_served}")
            check(
                "game: bot played move 1 on gameFull (e2e4)",
                posted[:1] == ["e2e4"],
                f"posted={posted}",
            )
            check(
                "game: bot responded to gameState events",
                posted == ["e2e4", "g1f3", "f1b5"],
                f"posted={posted}",
            )
            check(
                "game: handler tracked all moves",
                handler.moves_uci == ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5"],
                f"moves_uci={handler.moves_uci}",
            )
            check("game: finish handled", not handler._running)
            check(
                "game: experience recorded final position",
                len(experience.positions) == 1,
                f"positions={len(experience.positions)}",
            )

            # -- engine call parameters ------------------------------------
            calls = engine.calls
            check("engine: called once per our turn", len(calls) == 3, f"calls={len(calls)}")
            if len(calls) == 3:
                first, second, third = calls
                check(
                    "engine: call 1 sees empty move list and gameFull clocks",
                    first["moves"] == [] and first["wtime"] == 180000 and first["winc"] == 2000,
                    f"got {first}",
                )
                check(
                    "engine: call 2 sees 2 parsed moves and updated clock",
                    second["moves"] == ["e2e4", "e7e5"] and second["wtime"] == 179000,
                    f"got {second}",
                )
                check(
                    "engine: call 3 sees 4 parsed moves and updated clock",
                    third["moves"] == ["e2e4", "e7e5", "g1f3", "b8c6"] and third["wtime"] == 178000,
                    f"got {third}",
                )
                ours_only = all(
                    c["wtime"] is not None
                    and c["btime"] is not None
                    and c["winc"] is not None
                    and c["binc"] is not None
                    for c in calls
                )
                check(
                    "engine: called with both clocks and increments (white)",
                    ours_only,
                    f"calls={calls}",
                )
                # Both clocks must be forwarded with standard UCI semantics:
                # gameFull starts at 180000/180000 +2000, then the runner keeps
                # each side's clock in sync with the Lichess state events.
                expected_clocks = [
                    (180000, 180000, 2000, 2000),  # gameFull (moves: "")
                    (179000, 179500, 2000, 2000),  # gameState after 2 moves
                    (178000, 178500, 2000, 2000),  # gameState after 4 moves
                ]
                clocks_ok = len(calls) == len(expected_clocks) and all(
                    (c["wtime"], c["btime"], c["winc"], c["binc"]) == exp
                    for c, exp in zip(calls, expected_clocks)
                )
                check(
                    "engine: both clocks match Lichess state events (white)",
                    clocks_ok,
                    f"calls={calls}",
                )

        section("bot list", test_bot_list)
        section("challenge", test_challenge)
        section("event stream", test_event_stream)
        section("game flow", test_game_flow)
    finally:
        lichess_api.LICHESS_API = original_api
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())