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
        """POST /bot/account/upgrade - convert account to bot."""
        resp = self._request("POST", "/bot/account/upgrade")
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


# ---- Bot Main Loop ----------------------------------------------------------

class LichessBot:
    """Main bot orchestrator: event stream, challenge handling, game spawning."""

    def __init__(self, token: str):
        self.client = LichessClient(token)
        self.engine = Engine(ENGINE_PATH)
        self.experience = ExperienceStore(DEFAULT_DB_PATH)
        self.games: Dict[str, GameHandler] = {}
        self._running = True

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
        try:
            result = self.client.challenge_user(username, clock_limit=180, clock_increment=2, rated=rated)
            print(f"Challenge sent: {json.dumps(result, indent=2)}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"Challenge failed: {e.code} - {e.read().decode('utf-8')}", flush=True)
        except Exception as e:
            print(f"Challenge error: {e}", flush=True)

    def run(self) -> None:
        print("Starting Lichess Bot event stream...", flush=True)
        print(f"Engine: {self.engine.health()}", flush=True)
        
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

    def _handle_event(self, event: Dict[str, Any]) -> None:
        typ = event.get("type")
        if typ == "challenge":
            self._handle_challenge(event.get("challenge", {}))
        elif typ == "gameStart":
            self._handle_game_start(event.get("game", {}))
        # Ignore other events (gameFinish handled by GameHandler)

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

        print(f"Game started: {game_id} as {color} ({variant} rated={rated})", flush=True)

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

    def shutdown(self) -> None:
        self._running = False
        for handler in self.games.values():
            handler.stop()
        self.engine.shutdown()
        self.experience.close()


# ---- CLI --------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Duckish Official Lichess Bot")
    parser.add_argument("--token", help="Lichess Bot API token (or set LICHESS_TOKEN env var)")
    parser.add_argument("--upgrade", action="store_true", help="Upgrade account to BOT (one-time, irreversible)")
    parser.add_argument("--challenge", metavar="USERNAME", help="Send a 3+2 challenge to a user")
    parser.add_argument("--casual", action="store_true", help="Make challenge casual (default: rated)")
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

    try:
        bot.run()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
    finally:
        bot.shutdown()


if __name__ == "__main__":
    main()