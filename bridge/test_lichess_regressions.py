#!/usr/bin/env python3
"""Regression tests for engine output isolation and legal move submission."""

import importlib
import os
import sys
import tempfile
import threading
import queue
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List


BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

import lichess.api as lichess_api


class ScriptedProcess:
    def __init__(self, stdout_lines: List[str]) -> None:
        self.stdin = self
        self.stdout = iter(stdout_lines)
        self.stderr = iter(())

    def poll(self):
        return 0

    def write(self, _cmd: str) -> None:
        pass

    def flush(self) -> None:
        pass


def _make_scripted_engine(monkeypatch, stdout_lines: List[str]):
    engine_module = importlib.import_module("lichess.engine_client")
    engine_module.subprocess.Popen = lambda *args, **kwargs: ScriptedProcess(stdout_lines)
    engine = object.__new__(engine_module.Engine)
    engine.exe_path = "fake-engine"
    engine.proc = ScriptedProcess(stdout_lines)
    engine.lock = threading.Lock()
    engine._lines = queue.Queue()
    engine._alive = True
    engine._book_loaded = False
    engine._hash_mb = 1
    engine._threads = 1
    engine._last_error = None
    engine._ensure_alive = lambda: None
    engine.respawn = lambda: None
    return engine_module, engine


def test_best_move_drains_stale_output(monkeypatch):
    stale = "bestmove e7e6 ponder e7e5"
    fresh = "bestmove g1f3 ponder e7e5"
    engine_module, engine = _make_scripted_engine(monkeypatch, [stale, fresh])
    sent: List[str] = []
    engine._send = sent.append

    engine._lines.put(stale)
    move = engine.best_move(["e2e4"], movetime=10)

    expected_prefix = ["stop", "position startpos moves e2e4", "go movetime 10"]
    if move != "g1f3":
        print(f"FAIL: expected fresh move g1f3, got {move}")
        return
    if sent[:3] != expected_prefix:
        print(f"FAIL: commands should stop and drain before fresh search, got {sent[:3]}")
        return
    if engine._lines.empty() is not True:
        print("FAIL: stale queue item was not drained")
        return
    print("PASS: best_move drains stale output")


def test_multipv_deadline_is_extended(monkeypatch):
    engine_module, engine = _make_scripted_engine(monkeypatch, [])
    engine._send = lambda _cmd: None
    engine._read_line = lambda _deadline: (_ for _ in ()).throw(TimeoutError("deadline"))
    original = engine_module.SEARCH_DEADLINE_S
    try:
        engine_module.SEARCH_DEADLINE_S = 10
        try:
            engine.best_moves_multipv(["e2e4"], movetime=10, multipv=3)
        except TimeoutError:
            pass
        deadline = getattr(engine, "_last_multipv_deadline", None)
        if deadline is None:
            print("FAIL: best_moves_multipv did not record an extended deadline")
            return
        if deadline <= original:
            print(f"FAIL: expected MultiPV deadline > {original}, got {deadline}")
            return
        print(f"PASS: MultiPV deadline extended to {deadline - original}s")
    finally:
        engine_module.SEARCH_DEADLINE_S = original


class _PostCaptureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    posted_moves: List[Dict[str, str]] = []

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = __import__("json").dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        if path.startswith("/api/bot/game/") and "/move/" in path:
            rest = path[len("/api/bot/game/"):]
            game_id, uci = rest.split("/move/", 1)
            type(self).posted_moves.append({"game_id": game_id, "uci": uci})
            self._send_json({"ok": True})
            return
        self._send_json({"error": "not found"}, status=404)


class _MultipvEngine:
    def __init__(self) -> None:
        self.calls: List[List[str]] = []

    def best_moves_multipv(self, moves_uci, movetime=1500, multipv=3):
        self.calls.append(list(moves_uci))
        return [
            {"uci": "e7e6", "score_cp": 20, "rank": 1},
            {"uci": "g8f6", "score_cp": 10, "rank": 2},
        ]

    def best_move(self, moves_uci, movetime=150):
        return "e7e6"


class _NoopExperience:
    def apply_adjustments(self, _fen, candidates):
        return candidates


class _NoopClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def make_move(self, game_id: str, uci: str):
        body = __import__("json").dumps({"nominal": True}).encode("utf-8")
        req = __import__("urllib.request", fromlist=["Request"]).Request(
            f"{self.base_url}/bot/game/{game_id}/move/{uci}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with __import__("urllib.request", fromlist=["urlopen"]).urlopen(req, timeout=5) as resp:
            return __import__("json").loads(resp.read().decode("utf-8"))


def test_runner_uses_legal_alternative_before_posting(monkeypatch):
    _PostCaptureHandler.posted_moves = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PostCaptureHandler)
    host, port = server.server_address
    base_url = f"http://{host}:{port}/api"
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    runner = importlib.import_module("lichess.runner")
    original_client_cls = runner.LichessClient
    runner.LichessClient = lambda _token: _NoopClient(base_url)
    original_log = runner.GameHandler._log
    runner.GameHandler._log = lambda self, _msg: None
    try:
        engine = _MultipvEngine()
        handler = object.__new__(runner.GameHandler)
        handler.client = _NoopClient(base_url)
        handler.engine = engine
        handler.experience = _NoopExperience()
        handler.game_id = "gtest"
        handler.color = "black"
        handler.initial_fen = "startpos"
        handler.moves_uci = ["e2e4"]
        handler.wtime = 180
        handler.btime = 180
        handler.winc = 2
        handler.binc = 2
        handler.variant = "standard"
        handler.rated = False
        handler.state = None
        handler._running = True
        handler._finished = False
        handler._log = types.MethodType(original_log, handler)
        handler._thread = None

        handler._make_move()

        posted = [entry["uci"] for entry in _PostCaptureHandler.posted_moves]
        if posted != ["g8f6"]:
            print(f"FAIL: expected legal alternative g8f6 to be posted, got {posted}")
            return
        if handler.moves_uci != ["e2e4", "g8f6"]:
            print(f"FAIL: expected handler to record legal move, got {handler.moves_uci}")
            return
        print("PASS: runner posts legal alternative before submitting")
    finally:
        runner.LichessClient = original_client_cls
        runner.GameHandler._log = original_log
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def main() -> int:
    tests = [
        test_best_move_drains_stale_output,
        test_multipv_deadline_is_extended,
        test_runner_uses_legal_alternative_before_posting,
    ]
    for test in tests:
        test()
    return 0


if __name__ == "__main__":
    sys.exit(main())