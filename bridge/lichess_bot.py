#!/usr/bin/env python3
"""
Official Lichess Bot API client for Duckish.
Pure stdlib (urllib, threading, json, sqlite3). No external deps beyond python-chess.
"""
import os
import sys
import json
import time
import threading
import subprocess
import queue
import logging
import argparse
import urllib.request
import urllib.error
import urllib.parse
import http.server
import random
from typing import Optional, List, Dict, Any, Tuple
from http.client import HTTPResponse
import chess

# Add bridge directory to path for local imports
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BRIDGE_DIR)

from experience import ExperienceStore, DEFAULT_DB_PATH, BLUNDER_THRESHOLD_CP, LEARNING_BONUS_CP, mate_score_to_cp

# ---- Configuration ----------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_PATH = os.path.join(REPO_ROOT, "target", "release", "duckish.exe")
DASHBOARD_PORT = 5001
DASHBOARD_HOST = "127.0.0.1"


def load_dotenv_token(path: str = os.path.join(REPO_ROOT, ".env")) -> Optional[str]:
    """Load LICHESS_TOKEN from a .env file at the given path."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "LICHESS_TOKEN":
                    # Strip surrounding quotes if present
                    value = value.strip()
                    if (value.startswith('"') and value.endswith('"')) or (
                        value.startswith("'") and value.endswith("'")
                    ):
                        value = value[1:-1]
                    return value
    except OSError:
        pass
    return None
DEFAULT_HASH_MB = 256
DEFAULT_THREADS = max(1, (os.cpu_count() or 2) // 2)
BOOK_PATH = os.environ.get("DUCKISH_BOOK_PATH", "")
if not BOOK_PATH:
    BOOK_PATH = os.path.join(REPO_ROOT, "Performance.bin")

LICHESS_API = "https://lichess.org/api"
MOVE_OVERHEAD_MS = 100  # safety buffer so we never flag on network latency

# Supported time controls (standard variants only)
SUPPORTED_VARIANTS = {"standard"}
SUPPORTED_SPEEDS = {"bullet", "blitz", "rapid"}  # real-time only, no correspondence

# ---- Engine Management (mirrors bridge/server.py) ---------------------------

class Engine:
    """Manages the UCI engine subprocess with thread-safe I/O."""

    def __init__(self, exe_path: str, hash_mb: int = DEFAULT_HASH_MB, threads: int = DEFAULT_THREADS):
        self.exe_path = exe_path
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self._lines = queue.Queue()
        self._alive = False
        self._book_loaded = False
        self._hash_mb = hash_mb
        self._threads = threads
        self._last_error: Optional[str] = None
        self._start_engine()

    def _start_engine(self) -> None:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        self.proc = subprocess.Popen(
            [self.exe_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=REPO_ROOT,
            creationflags=creationflags,
        )
        self._alive = True

        self._lines = queue.Queue()
        self._reader_thread = threading.Thread(target=self._read_stdout_loop, daemon=True)
        self._reader_thread.start()

        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

        # UCI handshake
        self._send("uci")
        self._read_until(lambda line: line.strip() == "uciok", 10)
        self._send("isready")
        self._read_until(lambda line: line.strip() == "readyok", 10)
        self._send(f"setoption name Hash value {self._hash_mb}")
        self._send(f"setoption name MultiPV value 1")  # Lazy SMP strength
        self._send(f"setoption name Threads value {self._threads}")
        if BOOK_PATH and os.path.isfile(BOOK_PATH):
            self._send(f"setoption name BookPath value {BOOK_PATH}")
            self._book_loaded = True
        else:
            self._book_loaded = False

    def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for _ in self.proc.stderr:
            pass

    def _read_stdout_loop(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            for line in self.proc.stdout:
                self._lines.put(line.rstrip("\n"))
        finally:
            self._lines.put(None)

    def _send(self, cmd: str) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def _read_line(self, deadline: float) -> Optional[str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("read deadline exceeded")
        try:
            return self._lines.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("read deadline exceeded") from None

    def _read_until(self, predicate, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            line = self._read_line(deadline)
            if line is None:
                if self.proc and self.proc.poll() is not None:
                    raise RuntimeError("engine process died")
                raise RuntimeError("engine stdout closed")
            if predicate(line):
                return line

    def _ensure_alive(self) -> None:
        if self.proc and self.proc.poll() is not None:
            self._alive = False
            self._start_engine()

    def respawn(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self._send("quit")
                except Exception:
                    pass
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self._alive = False
            self._start_engine()

    def get_best_move(
        self,
        moves: List[str],
        wtime: Optional[int] = None,
        btime: Optional[int] = None,
        winc: Optional[int] = None,
        binc: Optional[int] = None,
        movetime: Optional[int] = None,
    ) -> Optional[str]:
        """
        Run search and return bestmove UCI. Uses wtime/btime/winc/binc with overhead buffer.
        """
        for attempt in range(2):
            try:
                with self.lock:
                    self._ensure_alive()

                    board = chess.Board()
                    uci_moves = []
                    for san in moves:
                        try:
                            move = board.parse_san(san)
                        except ValueError as e:
                            raise ValueError(f"illegal move: {san}") from e
                        uci_moves.append(move.uci())
                        board.push(move)

                    pos_cmd = "position startpos"
                    if uci_moves:
                        pos_cmd += " moves " + " ".join(uci_moves)
                    self._send(pos_cmd)

                    # Build go command with time management
                    if movetime is not None:
                        go_cmd = f"go movetime {movetime}"
                    else:
                        parts = ["go"]
                        if wtime is not None:
                            parts.append(f"wtime {max(0, wtime - MOVE_OVERHEAD_MS)}")
                        if btime is not None:
                            parts.append(f"btime {max(0, btime - MOVE_OVERHEAD_MS)}")
                        if winc is not None:
                            parts.append(f"winc {winc}")
                        if binc is not None:
                            parts.append(f"binc {binc}")
                        go_cmd = " ".join(parts)
                    self._send(go_cmd)

                    deadline = time.monotonic() + 30
                    bestmove_uci = ""
                    while True:
                        line = self._read_line(deadline)
                        if line is None:
                            raise RuntimeError("engine process died during search")
                        if line.startswith("bestmove"):
                            parts = line.split()
                            if len(parts) >= 2:
                                bestmove_uci = parts[1]
                            break

                    self._last_error = None
                    return bestmove_uci if bestmove_uci else None
            except (RuntimeError, EOFError, BrokenPipeError, TimeoutError) as e:
                self._last_error = str(e)
                if attempt == 0:
                    self.respawn()
                    continue
                raise

    def health(self) -> Dict[str, Any]:
        alive = self.proc is not None and self.proc.poll() is None
        return {
            "status": "ok" if alive else "dead",
            "engine": "duckish",
            "pid": self.proc.pid if self.proc else None,
            "alive": alive,
            "book_loaded": self._book_loaded,
            "threads": self._threads,
            "hash_mb": self._hash_mb,
            "last_error": self._last_error,
        }

    def shutdown(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self._send("quit")
                except Exception:
                    pass
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self._alive = False


# ---- Dashboard HTTP Server --------------------------------------------------

class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def __init__(self, bot_ref, *args, **kwargs):
        self.bot_ref = bot_ref
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
        elif self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            status = self.bot_ref.get_dashboard_status()
            self.wfile.write(json.dumps(status).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/auto-match/start":
            self.bot_ref.start_auto_match()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        elif self.path == "/api/auto-match/stop":
            self.bot_ref.stop_auto_match()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        elif self.path == "/api/challenge":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body)
                username = data.get("username", "").strip()
                rated = data.get("rated", True)
                if username:
                    self.bot_ref.challenge_user(username, rated=rated)
                    self.send_response(200)
                    self.wfile.write(b'{"ok":true}')
                else:
                    self.send_response(400)
                    self.wfile.write(b'{"error":"username required"}')
            except Exception as e:
                self.send_response(400)
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress default logging


class DashboardServer:
    def __init__(self, bot_ref, host: str = DASHBOARD_HOST, port: int = DASHBOARD_PORT):
        self.bot_ref = bot_ref
        self.host = host
        self.port = port
        self.server: Optional[http.server.HTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self):
        handler = lambda *args, **kwargs: DashboardHandler(self.bot_ref, *args, **kwargs)
        self.server = http.server.HTTPServer((self.host, self.port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        print(f"Dashboard started at http://{self.host}:{self.port}", flush=True)

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


# ---- HTTP Client for Lichess API --------------------------------------------

class LichessClient:
    """Minimal Lichess Bot API client using stdlib urllib."""

    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/x-ndjson",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        stream: bool = False,
    ) -> HTTPResponse:
        url = f"{LICHESS_API}{endpoint}"
        body = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=body, method=method, headers=self.headers)
        return urllib.request.urlopen(req, timeout=30)

    def upgrade_account(self) -> Dict[str, Any]:
        """POST https://lichess.org - convert account to bot (per official docs)."""
        url = "https://lichess.org"
        body = b"{}"
        req = urllib.request.Request(url, data=body, method="POST", headers=self.headers)
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read().decode("utf-8"))

    def stream_events(self) -> HTTPResponse:
        """GET /stream/event - NDJSON event stream."""
        return self._request("GET", "/stream/event", stream=True)

    def stream_game(self, game_id: str) -> HTTPResponse:
        """GET /bot/game/stream/{gameId} - NDJSON game stream."""
        return self._request("GET", f"/bot/game/stream/{game_id}", stream=True)

    def accept_challenge(self, challenge_id: str) -> Dict[str, Any]:
        resp = self._request("POST", f"/challenge/{challenge_id}/accept")
        return json.loads(resp.read().decode("utf-8"))

    def decline_challenge(self, challenge_id: str, reason: str = "generic") -> Dict[str, Any]:
        resp = self._request("POST", f"/challenge/{challenge_id}/decline", {"reason": reason})
        return json.loads(resp.read().decode("utf-8"))

    def make_move(self, game_id: str, move_uci: str) -> Dict[str, Any]:
        resp = self._request("POST", f"/bot/game/{game_id}/move/{move_uci}")
        return json.loads(resp.read().decode("utf-8"))

    def challenge_user(
        self,
        username: str,
        clock_limit: int = 180,
        clock_increment: int = 2,
        variant: str = "standard",
        rated: bool = True,
    ) -> Dict[str, Any]:
        data = {
            "clock.limit": clock_limit,
            "clock.increment": clock_increment,
            "variant": variant,
            "rated": rated,
        }
        resp = self._request("POST", f"/challenge/{username}", data)
        return json.loads(resp.read().decode("utf-8"))

    def get_online_bots(self) -> List[Dict[str, Any]]:
        """GET /api/bot/online - fetch list of online bots."""
        resp = self._request("GET", "/api/bot/online")
        data = resp.read().decode("utf-8")
        return json.loads(data)

    def get_account_info(self) -> Dict[str, Any]:
        """GET /account - fetch account info including perfs."""
        resp = self._request("GET", "/account")
        return json.loads(resp.read().decode("utf-8"))

    def resign_game(self, game_id: str) -> Dict[str, Any]:
        resp = self._request("POST", f"/bot/game/{game_id}/resign")
        return json.loads(resp.read().decode("utf-8"))


# ---- Game Handler -----------------------------------------------------------

class GameHandler:
    """Handles a single bot game: streams moves, drives engine, records experience."""

    def __init__(
        self,
        client: LichessClient,
        engine: Engine,
        experience: ExperienceStore,
        game_id: str,
        color: str,  # "white" or "black"
        initial_fen: str = "startpos",
        moves: str = "",
        wtime: Optional[int] = None,
        btime: Optional[int] = None,
        winc: Optional[int] = None,
        binc: Optional[int] = None,
        variant: str = "standard",
        rated: bool = False,
    ):
        self.client = client
        self.engine = engine
        self.experience = experience
        self.game_id = game_id
        self.color = color
        self.moves_san: List[str] = []
        self.moves_uci: List[str] = []
        self.wtime = wtime
        self.btime = btime
        self.winc = winc
        self.binc = binc
        self.variant = variant
        self.rated = rated
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _parse_initial_state(self, initial_fen: str, moves: str) -> None:
        board = chess.Board(initial_fen) if initial_fen != "startpos" else chess.Board()
        if moves:
            for san in moves.split():
                try:
                    move = board.parse_san(san)
                    self.moves_san.append(san)
                    self.moves_uci.append(move.uci())
                    board.push(move)
                except ValueError:
                    break

    def _run(self) -> None:
        try:
            resp = self.client.stream_game(self.game_id)
            for line in resp:
                if not self._running:
                    break
                line = line.decode("utf-8").strip()
                if not line:
                    continue
                event = json.loads(line)
                self._handle_event(event)
        except Exception as e:
            print(f"[Game {self.game_id}] Stream error: {e}", flush=True)
        finally:
            self._running = False

    def _handle_event(self, event: Dict[str, Any]) -> None:
        typ = event.get("type")
        if typ == "gameFull":
            # Initial full game state
            self._parse_initial_state(event.get("initialFen", "startpos"), event.get("state", {}).get("moves", ""))
            self.wtime = event.get("state", {}).get("wtime")
            self.btime = event.get("state", {}).get("btime")
            self.winc = event.get("winc")
            self.binc = event.get("binc")
            # If it's our turn, make a move
            if self._is_our_turn(event.get("state", {})):
                self._make_move()
        elif typ == "gameState":
            # Incremental state update
            moves = event.get("moves", "")
            if moves:
                # Parse new moves only
                self._parse_incremental_moves(moves)
            self.wtime = event.get("wtime", self.wtime)
            self.btime = event.get("btime", self.btime)
            if self._is_our_turn_from_state(event):
                self._make_move()
        elif typ == "chatLine":
            pass  # ignore chat
        elif typ in ("gameFinish",):
            self._handle_game_finish(event)

    def _parse_incremental_moves(self, moves_str: str) -> None:
        board = chess.Board()
        for uci in self.moves_uci:
            try:
                board.push_uci(uci)
            except ValueError:
                break
        for san in moves_str.split():
            if len(self.moves_san) >= len(self.moves_uci) + 1:
                # Already have this move, skip
                continue
            try:
                move = board.parse_san(san)
                self.moves_san.append(san)
                self.moves_uci.append(move.uci())
                board.push(move)
            except ValueError:
                break

    def _is_our_turn(self, state: Dict[str, Any]) -> bool:
        # state has "moves" string; our turn if move count parity matches our color
        moves_str = state.get("moves", "")
        move_count = len(moves_str.split()) if moves_str else 0
        return (move_count % 2 == 0 and self.color == "white") or (move_count % 2 == 1 and self.color == "black")

    def _is_our_turn_from_state(self, event: Dict[str, Any]) -> bool:
        # For gameState events, check if the last move was opponent's
        moves_str = event.get("moves", "")
        if not moves_str and not self.moves_san:
            return self.color == "white"
        # Count total moves
        board = chess.Board()
        for uci in self.moves_uci:
            try:
                board.push_uci(uci)
            except ValueError:
                break
        for san in moves_str.split():
            try:
                move = board.parse_san(san)
                board.push(move)
            except ValueError:
                break
        return board.turn == (chess.WHITE if self.color == "white" else chess.BLACK)

    def _make_move(self) -> None:
        if not self._running:
            return
        my_wtime = self.wtime if self.color == "white" else None
        my_btime = self.btime if self.color == "black" else None
        my_winc = self.winc if self.color == "white" else None
        my_binc = self.binc if self.color == "black" else None

        # Opponent's time (for pondering context, not used in go cmd but good to track)
        opp_wtime = self.btime if self.color == "white" else self.wtime
        opp_btime = self.wtime if self.color == "white" else self.btime

        bestmove = self.engine.get_best_move(
            self.moves_san,
            wtime=my_wtime,
            btime=my_btime,
            winc=my_winc,
            binc=my_binc,
        )
        if bestmove:
            try:
                self.client.make_move(self.game_id, bestmove)
                self.moves_san.append(chess.Board(" ".join(self.moves_uci)).san(chess.Move.from_uci(bestmove)))
                self.moves_uci.append(bestmove)
                print(f"[Game {self.game_id}] Played {bestmove}", flush=True)
            except Exception as e:
                print(f"[Game {self.game_id}] Move failed: {e}", flush=True)

    def _handle_game_finish(self, event: Dict[str, Any]) -> None:
        result = event.get("result")  # "white", "black", "draw"
        winner = event.get("winner")  # "white", "black", or None for draw
        status = event.get("status")  # "mate", "resign", "timeout", etc.
        
        game_result = "draw"
        game_winner = "draw"
        if winner == "white":
            game_result = "win" if self.color == "white" else "loss"
            game_winner = "white"
        elif winner == "black":
            game_result = "win" if self.color == "black" else "loss"
            game_winner = "black"

        print(f"[Game {self.game_id}] Finished: {game_result} ({status})", flush=True)
        self._record_experience(game_result, game_winner)
        self._running = False

    def _record_experience(self, result: str, winner: str) -> None:
        """Post-mortem learning: record blunders and corrective bonuses."""
        if len(self.moves_uci) < 2:
            return

        # Replay and collect evaluations
        eval_history = []  # (fen_before, move_uci, score_cp_after)
        temp_board = chess.Board()
        
        for i, uci in enumerate(self.moves_uci):
            fen_before = temp_board.fen()
            try:
                move = chess.Move.from_uci(uci)
                temp_board.push(move)
            except ValueError:
                break
            fen_after = temp_board.fen()
            
            # Quick shallow analysis for learning signal
            try:
                analysis = self.engine.get_best_move(
                    self.moves_san[:i+1],
                    movetime=100,
                )
                if analysis:
                    # We don't have score from get_best_move, need a different approach
                    # For simplicity, skip deep eval and just use move count heuristics
                    pass
            except Exception:
                pass

        # Simplified: just record final position with result
        final_fen = temp_board.fen()
        self.experience.record_position(final_fen, None, result)
        
        # For losses, penalize last move
        if result == "loss" and self.moves_uci:
            board = chess.Board()
            for uci in self.moves_uci[:-1]:
                try:
                    board.push_uci(uci)
                except ValueError:
                    break
            fen_before = board.fen()
            last_move = self.moves_uci[-1]
            self.experience.record_move(fen_before, last_move, penalty_cp=BLUNDER_THRESHOLD_CP, bonus_cp=0.0)

    def stop(self) -> None:
        self._running = False


# ---- Dashboard HTML ---------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Duckish Bot Dashboard</title>
<style>
:root { --bg:#0d1117; --panel:#161b22; --panel2:#21262d; --fg:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --accent2:#a371f7; --ok:#3fb950; --warn:#d29922; --err:#f85149; --border:#30363d; }
*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
body{background:var(--bg);color:var(--fg);min-height:100vh;display:flex;flex-direction:column}
header{background:var(--panel);border-bottom:1px solid var(--border);padding:16px 24px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:10}
h1{font-size:1.25rem;font-weight:600;letter-spacing:-0.02em}
.status-badge{padding:4px 10px;border-radius:999px;font-size:0.75rem;font-weight:600;text-transform:uppercase}
.status-online{background:rgba(63,185,80,.15);color:var(--ok);border:1px solid var(--ok)}
.status-offline{background:rgba(248,81,73,.15);color:var(--err);border:1px solid var(--err)}
.status-auto{background:rgba(210,153,34,.15);color:var(--warn);border:1px solid var(--warn)}
main{flex:1;padding:24px;display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;max-width:1400px;margin:0 auto;width:100%}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:20px;display:flex;flex-direction:column}
.card h2{font-size:0.875rem;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;color:var(--muted);margin-bottom:16px;border-bottom:1px solid var(--border);padding-bottom:12px}
.stat-row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border)}
.stat-row:last-child{border-bottom:none}
.stat-label{color:var(--muted);font-size:0.875rem}
.stat-value{font-weight:600;font-size:0.875rem;text-align:right}
.game-card{flex:1;display:flex;flex-direction:column}
.game-info{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}
.info-item{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:12px}
.info-label{font-size:0.7rem;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
.info-value{font-weight:600;font-size:0.95rem}
.move-list{flex:1;overflow-y:auto;font-family:monospace;font-size:0.8rem;line-height:1.8;color:var(--muted);background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:12px;max-height:200px}
.move-list .eval{color:var(--accent);font-weight:600}
.watch-link{display:inline-block;margin-top:12px;padding:10px 16px;background:var(--accent);color:#0d1117;border-radius:8px;font-weight:600;text-decoration:none;text-align:center;transition:opacity .2s}
.watch-link:hover{opacity:.85}
.btn{padding:10px 16px;border-radius:8px;border:1px solid var(--border);background:var(--panel2);color:var(--fg);font-weight:600;font-size:0.875rem;cursor:pointer;transition:all .15s}
.btn:hover{border-color:var(--accent);background:var(--panel)}
.btn-primary{background:var(--accent);color:#0d1117;border-color:var(--accent)}
.btn-primary:hover{filter:brightness(1.1)}
.btn-danger{background:rgba(248,81,73,.15);color:var(--err);border-color:var(--err)}
.btn-danger:hover{background:rgba(248,81,73,.25)}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.control-group{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.toggle{display:flex;align-items:center;gap:6px}
.toggle input{width:16px;height:16px;accent-color:var(--accent)}
.log-container{flex:1;overflow-y:auto;font-family:monospace;font-size:0.75rem;line-height:1.6;background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:12px;max-height:300px}
.log-entry{padding:4px 0;border-bottom:1px solid var(--border);display:flex;gap:8px}
.log-time{color:var(--muted);white-space:nowrap;min-width:70px}
.log-msg{color:var(--fg);flex:1}
.log-success{color:var(--ok)}
.log-error{color:var(--err)}
.log-warn{color:var(--warn)}
.manual-challenge{display:flex;gap:8px;margin-top:12px}
.manual-challenge input{flex:1;padding:8px 12px;border:1px solid var(--border);background:var(--panel);color:var(--fg);border-radius:6px;font-size:0.875rem}
.manual-challenge input:focus{outline:none;border-color:var(--accent)}
.no-game{text-align:center;color:var(--muted);padding:40px 20px;font-size:0.9rem}
.refresh-indicator{height:3px;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:0 0 12px 12px;transform-origin:left;transform:scaleX(0);animation:pulse 1.5s linear infinite}
@keyframes pulse{0%{transform:scaleX(0)}100%{transform:scaleX(1)}}
.rating-badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:0.7rem;font-weight:600;background:var(--panel2);border:1px solid var(--border)}
</style>
</head>
<body>
<header>
<h1>🦆 Duckish Bot Dashboard</h1>
<span id="statusBadge" class="status-badge status-offline">Offline</span>
</header>
<main>
<div class="card">
<h2>Bot Status</h2>
<div id="botStatus">
<div class="stat-row"><span class="stat-label">Username</span><span class="stat-value" id="username">-</span></div>
<div class="stat-row"><span class="stat-label">Blitz Rating</span><span class="stat-value" id="blitzRating">-</span></div>
<div class="stat-row"><span class="stat-label">Rapid Rating</span><span class="stat-value" id="rapidRating">-</span></div>
<div class="stat-row"><span class="stat-label">Session W/L/D</span><span class="stat-value" id="sessionRecord">0/0/0</span></div>
<div class="stat-row"><span class="stat-label">Performance.bin</span><span class="stat-value" id="perfBook">-</span></div>
<div class="stat-row"><span class="stat-label">Experience Book</span><span class="stat-value" id="expBook">-</span></div>
</div>
</div>
<div class="card game-card">
<h2>Active Game</h2>
<div id="activeGame">
<div class="no-game">No active game</div>
</div>
</div>
<div class="card">
<h2>Auto-Match Controls</h2>
<div class="controls">
<div class="control-group">
<button id="btnStart" class="btn btn-primary">Start Auto-Match</button>
<button id="btnStop" class="btn btn-danger" disabled>Stop Auto-Match</button>
</div>
<div class="control-group toggle">
<input type="checkbox" id="toggleRated" checked>
<label for="toggleRated">Rated</label>
</div>
</div>
<div class="manual-challenge">
<input type="text" id="manualUser" placeholder="Username to challenge...">
<button id="btnManual" class="btn">Challenge</button>
</div>
<div class="log-container" id="challengeLog"></div>
</div>
</main>
<div class="refresh-indicator" id="refreshBar"></div>
<script>
const $ = (s) => document.querySelector(s);
const log = (msg, cls='') => { const el=document.createElement('div'); el.className='log-entry'+(cls?' '+cls:''); el.innerHTML=`<span class="log-time">${new Date().toLocaleTimeString()}</span><span class="log-msg">${msg}</span>`; $('#challengeLog').prepend(el); };
let autoMatchActive = false;
async function fetchStatus(){
try{const r=await fetch('/api/status');if(!r.ok)throw 0;const d=await r.json();updateUI(d);}catch{$('#statusBadge').textContent='Error';$('#statusBadge').className='status-badge status-offline';}
}
function updateUI(d){
$('#statusBadge').textContent=d.auto_match?'Auto-Match':(d.online?'Online':'Offline');
$('#statusBadge').className='status-badge '+(d.auto_match?'status-auto':(d.online?'status-online':'status-offline'));
$('#username').textContent=d.username||'-';
$('#blitzRating').innerHTML=d.blitz_rating?`${d.blitz_rating}<span class="rating-badge">blitz</span>`:'-';
$('#rapidRating').innerHTML=d.rapid_rating?`${d.rapid_rating}<span class="rating-badge">rapid</span>`:'-';
$('#sessionRecord').textContent=`${d.session_wins||0}/${d.session_losses||0}/${d.session_draws||0}`;
$('#perfBook').textContent=d.perf_book_loaded?'Loaded':'Missing';
$('#expBook').textContent=d.exp_book_loaded?'Loaded':'Missing';
$('#btnStart').disabled=d.auto_match;$('#btnStop').disabled=!d.auto_match;
autoMatchActive=d.auto_match;
const g=d.active_game;
if(g){
$('#activeGame').innerHTML=`
<div class="game-info">
<div class="info-item"><div class="info-label">Opponent</div><div class="info-value">${g.opponent_name} <span class="rating-badge">${g.opponent_rating||'-'}</span></div></div>
<div class="info-item"><div class="info-label">Our Color</div><div class="info-value">${g.our_color}</div></div>
<div class="info-item"><div class="info-label">Moves</div><div class="info-value">${g.move_count}</div></div>
<div class="info-item"><div class="info-label">Last Eval</div><div class="info-value"><span class="eval">${g.last_eval||'?'}</span></div></div>
</div>
<div class="move-list" id="moveList"></div>
<a class="watch-link" href="https://lichess.org${g.game_url}" target="_blank">Watch Live on Lichess</a>
`;
if(g.moves){
const ml=$('#moveList');
ml.innerHTML=g.moves.map((m,i)=>`<div>${i+1}. ${m.san} <span class="eval">(${m.eval||'?'})</span></div>`).join('');
}
}else{
$('#activeGame').innerHTML='<div class="no-game">No active game</div>';
}
}
$('#btnStart').onclick=async()=>{await fetch('/api/auto-match/start',{method:'POST'});log('Auto-match started','log-success');fetchStatus();};
$('#btnStop').onclick=async()=>{await fetch('/api/auto-match/stop',{method:'POST'});log('Auto-match stopped','log-warn');fetchStatus();};
$('#btnManual').onclick=async()=>{const u=$('#manualUser').value.trim();if(!u)return;$('#manualUser').value='';log(`Challenging ${u}...`);try{const r=await fetch('/api/challenge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,rated:$('#toggleRated').checked})});const d=await r.json();if(r.ok)log(`Challenge sent to ${u}`,'log-success');else log(`Failed: ${d.error||'unknown'}`,'log-error');}catch(e){log('Request failed','log-error');}fetchStatus();};
fetchStatus();setInterval(fetchStatus,1500);
</script>
</body>
</html>
"""

# ---- Bot Main Loop ----------------------------------------------------------

class LichessBot:
    """Main bot orchestrator: event stream, challenge handling, game spawning, auto-match, dashboard."""

    def __init__(self, token: str):
        self.client = LichessClient(token)
        self.engine = Engine(ENGINE_PATH)
        self.experience = ExperienceStore(DEFAULT_DB_PATH)
        self.games: Dict[str, GameHandler] = {}
        self._running = True
        self.dashboard = DashboardServer(self)
        self._username: Optional[str] = None
        self._blitz_rating: Optional[int] = None
        self._rapid_rating: Optional[int] = None
        self._session_wins = 0
        self._session_losses = 0
        self._session_draws = 0
        self._auto_match_active = False
        self._auto_match_rated = True
        self._auto_match_thread: Optional[threading.Thread] = None
        self._current_game_id: Optional[str] = None
        self._current_game_info: Optional[Dict[str, Any]] = None
        self._challenge_log: List[Dict[str, Any]] = []
        self._perf_book_loaded = os.path.isfile(BOOK_PATH)
        self._exp_book_loaded = os.path.isfile(DEFAULT_DB_PATH)

    def upgrade_account(self) -> None:
        print("Upgrading account to BOT...", flush=True)
        print("WARNING: This permanently converts a 0-game account into a BOT account.", flush=True)
        try:
            result = self.client.upgrade_account()
            print(f"Upgrade result: {json.dumps(result, indent=2)}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"Upgrade failed: {e.code} - {e.read().decode('utf-8')}", flush=True)
        except Exception as e:
            print(f"Upgrade error: {e}", flush=True)

    def challenge_user(self, username: str, rated: bool = True) -> None:
        print(f"Challenging {username} to 3+2 {'rated' if rated else 'casual'}...", flush=True)
        self._log_challenge(f"Challenging {username} ({'rated' if rated else 'casual'})")
        try:
            result = self.client.challenge_user(username, clock_limit=180, clock_increment=2, rated=rated)
            print(f"Challenge sent: {json.dumps(result, indent=2)}", flush=True)
            self._log_challenge(f"Challenge sent to {username}", success=True)
        except urllib.error.HTTPError as e:
            err = e.read().decode('utf-8')
            print(f"Challenge failed: {e.code} - {err}", flush=True)
            self._log_challenge(f"Failed: {err}", success=False)
        except Exception as e:
            print(f"Challenge error: {e}", flush=True)
            self._log_challenge(f"Error: {e}", success=False)

    def _log_challenge(self, msg: str, success: Optional[bool] = None) -> None:
        entry = {"time": time.time(), "msg": msg}
        if success is not None:
            entry["success"] = success
        self._challenge_log.append(entry)
        if len(self._challenge_log) > 100:
            self._challenge_log = self._challenge_log[-100:]

    def run(self) -> None:
        self.dashboard.start()
        self._fetch_account_info()
        print("Starting Lichess Bot event stream...", flush=True)
        print(f"Engine: {self.engine.health()}", flush=True)
        print(f"Dashboard: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}", flush=True)
        
        while self._running:
            try:
                resp = self.client.stream_events()
                for line in resp:
                    if not self._running:
                        break
                    line = line.decode("utf-8").strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    self._handle_event(event)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    print("Authentication failed: invalid token", flush=True)
                    break
                print(f"HTTP error: {e.code}, reconnecting in 5s...", flush=True)
                time.sleep(5)
            except Exception as e:
                print(f"Stream error: {e}, reconnecting in 5s...", flush=True)
                time.sleep(5)

    def _fetch_account_info(self) -> None:
        try:
            info = self.client.get_account_info()
            self._username = info.get("username") or info.get("id")
            perfs = info.get("perfs", {})
            self._blitz_rating = perfs.get("blitz", {}).get("rating")
            self._rapid_rating = perfs.get("rapid", {}).get("rating")
        except Exception as e:
            print(f"Failed to fetch account info: {e}", flush=True)

    def _handle_event(self, event: Dict[str, Any]) -> None:
        typ = event.get("type")
        if typ == "challenge":
            self._handle_challenge(event.get("challenge", {}))
        elif typ == "gameStart":
            self._handle_game_start(event.get("game", {}))
        elif typ == "gameFinish":
            self._handle_game_finish(event.get("game", {}))
        # Ignore other events

    def _handle_challenge(self, challenge: Dict[str, Any]) -> None:
        challenge_id = challenge.get("id")
        variant = challenge.get("variant", {}).get("key", "standard")
        speed = challenge.get("speed", "unknown")
        time_control = challenge.get("timeControl", {})
        rated = challenge.get("rated", False)
        challenger = challenge.get("challenger", {}).get("name", "unknown")

        print(f"Challenge from {challenger}: {variant} {speed} rated={rated} tc={time_control}", flush=True)

        # Only accept standard variant real-time games
        if variant not in SUPPORTED_VARIANTS:
            print(f"  -> Declining: unsupported variant {variant}", flush=True)
            self.client.decline_challenge(challenge_id, "variant")
            return
        if speed not in SUPPORTED_SPEEDS:
            print(f"  -> Declining: unsupported speed {speed} (correspondence not supported)", flush=True)
            self.client.decline_challenge(challenge_id, "timecontrol")
            return

        # Accept!
        print(f"  -> Accepting challenge", flush=True)
        try:
            self.client.accept_challenge(challenge_id)
        except Exception as e:
            print(f"  -> Accept failed: {e}", flush=True)

    def _handle_game_start(self, game: Dict[str, Any]) -> None:
        game_id = game.get("id")
        color = game.get("color")  # "white" or "black"
        initial_fen = game.get("initialFen", "startpos")
        moves = game.get("state", {}).get("moves", "")
        wtime = game.get("state", {}).get("wtime")
        btime = game.get("state", {}).get("btime")
        winc = game.get("winc")
        binc = game.get("binc")
        variant = game.get("variant", "standard")
        rated = game.get("rated", False)
        opponent = game.get("opponent", {}).get("name", "unknown")
        opponent_rating = game.get("opponent", {}).get("rating")

        print(f"Game started: {game_id} as {color} vs {opponent} ({variant} rated={rated})", flush=True)

        handler = GameHandler(
            self.client,
            self.engine,
            self.experience,
            game_id,
            color,
            initial_fen,
            moves,
            wtime,
            btime,
            winc,
            binc,
            variant,
            rated,
        )
        self.games[game_id] = handler
        self._current_game_id = game_id
        self._current_game_info = {
            "game_id": game_id,
            "opponent_name": opponent,
            "opponent_rating": opponent_rating,
            "our_color": color,
            "variant": variant,
            "rated": rated,
            "moves": [],
            "move_count": 0,
            "last_eval": None,
            "game_url": f"/{game_id}",
        }
        # Patch handler to update our game info
        original_handle_event = handler._handle_event
        def wrapped_handle_event(event):
            original_handle_event(event)
            self._update_game_info(handler, event)
        handler._handle_event = wrapped_handle_event

    def _update_game_info(self, handler: GameHandler, event: Dict[str, Any]) -> None:
        if not self._current_game_info or handler.game_id != self._current_game_id:
            return
        typ = event.get("type")
        if typ == "gameState":
            moves_str = event.get("moves", "")
            if moves_str:
                move_list = moves_str.split()
                self._current_game_info["move_count"] = len(move_list)
                # Build move list with SAN and eval placeholder
                self._current_game_info["moves"] = [
                    {"san": san, "eval": "?"} for san in move_list
                ]
        elif typ == "gameFull":
            moves_str = event.get("state", {}).get("moves", "")
            if moves_str:
                move_list = moves_str.split()
                self._current_game_info["move_count"] = len(move_list)
                self._current_game_info["moves"] = [
                    {"san": san, "eval": "?"} for san in move_list
                ]

    def _handle_game_finish(self, game: Dict[str, Any]) -> None:
        game_id = game.get("id")
        winner = game.get("winner")  # "white", "black", or None for draw
        status = game.get("status")
        
        if game_id in self.games:
            handler = self.games[game_id]
            our_color = handler.color
            if winner == "white":
                result = "win" if our_color == "white" else "loss"
            elif winner == "black":
                result = "win" if our_color == "black" else "loss"
            else:
                result = "draw"
            
            if result == "win":
                self._session_wins += 1
            elif result == "loss":
                self._session_losses += 1
            else:
                self._session_draws += 1
            
            print(f"[Game {game_id}] Finished: {result} ({status})", flush=True)
            self._log_challenge(f"Game vs {self._current_game_info['opponent_name'] if self._current_game_info else 'unknown'}: {result} ({status})", success=(result=="win"))
            
            del self.games[game_id]
            if self._current_game_id == game_id:
                self._current_game_id = None
                self._current_game_info = None
            
            # If auto-match is active, schedule next challenge
            if self._auto_match_active:
                threading.Timer(5.0, self._auto_match_next).start()

    def get_dashboard_status(self) -> Dict[str, Any]:
        active_game = None
        if self._current_game_info:
            active_game = dict(self._current_game_info)
        return {
            "online": True,
            "auto_match": self._auto_match_active,
            "username": self._username,
            "blitz_rating": self._blitz_rating,
            "rapid_rating": self._rapid_rating,
            "session_wins": self._session_wins,
            "session_losses": self._session_losses,
            "session_draws": self._session_draws,
            "perf_book_loaded": self._perf_book_loaded,
            "exp_book_loaded": self._exp_book_loaded,
            "active_game": active_game,
            "challenge_log": self._challenge_log[-20:],  # Last 20 entries
        }

    def start_auto_match(self) -> None:
        if self._auto_match_active:
            return
        self._auto_match_active = True
        self._auto_match_thread = threading.Thread(target=self._auto_match_loop, daemon=True)
        self._auto_match_thread.start()
        print("Auto-match started", flush=True)
        self._log_challenge("Auto-match started", success=True)

    def stop_auto_match(self) -> None:
        self._auto_match_active = False
        print("Auto-match stopped", flush=True)
        self._log_challenge("Auto-match stopped", success=False)

    def _auto_match_loop(self) -> None:
        self._auto_match_next()

    def _auto_match_next(self) -> None:
        if not self._auto_match_active:
            return
        if self._current_game_id:
            # Wait for current game to finish
            return
        
        try:
            bots = self.client.get_online_bots()
            # Filter: skip self, skip bots with 0 blitz games
            candidates = []
            for bot in bots:
                name = bot.get("username") or bot.get("id")
                if name == self._username:
                    continue
                perfs = bot.get("perfs", {})
                blitz_games = perfs.get("blitz", {}).get("games", 0)
                if blitz_games == 0:
                    continue
                candidates.append(name)
            
            if not candidates:
                self._log_challenge("No suitable bots found, retrying in 30s", success=False)
                threading.Timer(30.0, self._auto_match_next).start()
                return
            
            random.shuffle(candidates)
            for opponent in candidates:
                if not self._auto_match_active:
                    return
                if self._current_game_id:
                    return  # Game started
                self._log_challenge(f"Challenging {opponent} ({'rated' if self._auto_match_rated else 'casual'})")
                try:
                    self.client.challenge_user(opponent, clock_limit=180, clock_increment=2, rated=self._auto_match_rated)
                    self._log_challenge(f"Challenge sent to {opponent}", success=True)
                except Exception as e:
                    self._log_challenge(f"Challenge to {opponent} failed: {e}", success=False)
                # Wait ~10s before next challenge if declined/timed out
                time.sleep(10)
            
            # If we exhausted candidates, wait and retry
            if self._auto_match_active:
                self._log_challenge("Exhausted candidates, retrying in 30s", success=False)
                threading.Timer(30.0, self._auto_match_next).start()
        except Exception as e:
            self._log_challenge(f"Auto-match error: {e}", success=False)
            if self._auto_match_active:
                threading.Timer(30.0, self._auto_match_next).start()

    def shutdown(self) -> None:
        self._running = False
        self._auto_match_active = False
        for handler in self.games.values():
            handler.stop()
        self.engine.shutdown()
        self.experience.close()
        self.dashboard.stop()


# ---- CLI --------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Duckish Official Lichess Bot")
    parser.add_argument("--token", help="Lichess Bot API token (or set LICHESS_TOKEN env var)")
    parser.add_argument("--upgrade", action="store_true", help="Upgrade account to BOT (one-time, irreversible)")
    parser.add_argument("--challenge", metavar="USERNAME", help="Send a 3+2 challenge to a user")
    parser.add_argument("--casual", action="store_true", help="Make challenge casual (default: rated)")
    parser.add_argument("--auto-match", action="store_true", help="Enable autonomous bot matchmaking")
    parser.add_argument("--open-browser", action="store_true", help="Open dashboard in browser on start")
    args = parser.parse_args()

    token = args.token or os.environ.get("LICHESS_TOKEN") or load_dotenv_token()
    if not token:
        print("Error: Lichess token required. Set LICHESS_TOKEN env var, use --token, or add to .env", flush=True)
        sys.exit(1)

    bot = LichessBot(token)

    if args.upgrade:
        bot.upgrade_account()
        return

    if args.challenge:
        bot.challenge_user(args.challenge, rated=not args.casual)
        return

    if args.auto_match:
        bot.start_auto_match()

    if args.open_browser:
        import webbrowser
        webbrowser.open(f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")

    try:
        bot.run()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
    finally:
        bot.shutdown()


if __name__ == "__main__":
    main()