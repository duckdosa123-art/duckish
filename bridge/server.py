#!/usr/bin/env python3
"""
Bridge HTTP server for Duckish chess engine.
Pure stdlib + python-chess. No Flask.
"""

import os
import sys
import json
import time
import queue
import threading
import subprocess
import logging
import re
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Optional, List, Dict, Any, Tuple
import chess

# Add bridge directory to path for local imports
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BRIDGE_DIR)

from experience import ExperienceStore, BLUNDER_THRESHOLD_CP, LEARNING_BONUS_CP, DEFAULT_DB_PATH, mate_score_to_cp

# ---- Configuration ----------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_PATH = os.path.join(REPO_ROOT, "target", "release", "duckish.exe")
DEFAULT_MULTIPV = int(os.environ.get("DUCKISH_MULTIPV", "3"))
DEFAULT_MULTIPV = max(1, min(10, DEFAULT_MULTIPV))
DEFAULT_MOVETIME = 400  # ms
HANDSHAKE_TIMEOUT = 10  # seconds
ANALYSIS_TIMEOUT = 15   # seconds
BOOK_PATH = os.environ.get("DUCKISH_BOOK_PATH", "")
if not BOOK_PATH:
    BOOK_PATH = os.path.join(REPO_ROOT, "Performance.bin")

# ---- Engine Subprocess Management -------------------------------------------

class Engine:
    """Manages the UCI engine subprocess with thread-safe I/O."""

    def __init__(self, exe_path: str, default_multipv: int, threads: Optional[int] = None):
        self.exe_path = exe_path
        self.default_multipv = default_multipv
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._lines = queue.Queue()
        self._alive = False
        self._book_loaded = False
        self._book_hits = 0
        self._hash_mb = 256
        self._threads = threads if threads is not None else max(1, (os.cpu_count() or 2) // 2)
        self._last_error: Optional[str] = None
        self._start_engine()

    def _start_engine(self) -> None:
        """Start the engine process and perform UCI handshake."""
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

        # Drain stderr in background
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

        # UCI handshake
        self._send("uci")
        self._read_until(lambda line: line.strip() == "uciok", HANDSHAKE_TIMEOUT)
        self._send("isready")
        self._read_until(lambda line: line.strip() == "readyok", HANDSHAKE_TIMEOUT)
        self._send(f"setoption name Hash value {self._hash_mb}")
        self._send(f"setoption name MultiPV value {self.default_multipv}")
        self._send(f"setoption name Threads value {self._threads}")
        if BOOK_PATH and os.path.isfile(BOOK_PATH):
            self._send(f"setoption name BookPath value {BOOK_PATH}")
            self._book_loaded = True
        else:
            self._book_loaded = False
        # No need to wait for setoption ack

    def _drain_stderr(self) -> None:
        """Continuously read stderr to prevent pipe blocking."""
        assert self.proc and self.proc.stderr
        for _ in self.proc.stderr:
            pass  # discard

    def _read_stdout_loop(self) -> None:
        """Continuously read engine stdout. Windows pipes are not selectable."""
        assert self.proc and self.proc.stdout
        try:
            for line in self.proc.stdout:
                self._lines.put(line.rstrip("\n"))
        finally:
            self._lines.put(None)  # EOF sentinel

    def _send(self, cmd: str) -> None:
        """Send a command to the engine (must hold lock)."""
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def _read_line(self, deadline: float) -> Optional[str]:
        """Read one line from the stdout queue with deadline. Returns None on EOF."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("read deadline exceeded")
        try:
            return self._lines.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("read deadline exceeded") from None

    def _read_until(self, predicate, timeout: float) -> str:
        """Read lines until predicate(line) is True. Returns the matching line."""
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
        """Restart engine if process died."""
        if self.proc and self.proc.poll() is not None:
            self._alive = False
            self._start_engine()

    def respawn(self) -> None:
        """Force respawn the engine and re-apply settings."""
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

    def analyze(
        self,
        moves: List[str],
        movetime: int,
        multipv: int,
        depth: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Run analysis on a position. Returns parsed result dict.
        Raises ValueError on bad moves, TimeoutError on timeout, RuntimeError on engine death.
        """
        for attempt in range(2):  # try once, then respawn and retry once
            try:
                with self.lock:
                    self._ensure_alive()

                    # Build position command
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

                    # Build go command
                    if depth is not None:
                        go_cmd = f"go depth {depth}"
                    else:
                        go_cmd = f"go movetime {movetime}"
                    if multipv != self.default_multipv:
                        # Set MultiPV for this search only (if engine supports dynamic)
                        self._send(f"setoption name MultiPV value {multipv}")
                    self._send(go_cmd)

                    # Read until bestmove
                    deadline = time.monotonic() + ANALYSIS_TIMEOUT
                    info_lines_by_depth: Dict[int, Dict[int, str]] = {}
                    bestmove_line = ""
                    last_depth_seen = 0

                    while True:
                        line = self._read_line(deadline)
                        if line is None:
                            raise RuntimeError("engine process died during analysis")
                        if line.startswith("info "):
                            # Parse depth and multipv rank; keep only the LAST line per (depth, rank),
                            # because aspiration re-searches reprint rank 1 several times at one depth.
                            parts = line.split()
                            try:
                                d = int(parts[parts.index("depth") + 1])
                                try:
                                    rank = int(parts[parts.index("multipv") + 1])
                                except (ValueError, IndexError):
                                    rank = 1
                                last_depth_seen = max(last_depth_seen, d)
                                info_lines_by_depth.setdefault(d, {})[rank] = line
                            except (ValueError, IndexError):
                                pass
                        elif line.startswith("bestmove"):
                            bestmove_line = line
                            break

                    # Determine last complete depth
                    requested_mpv = multipv
                    complete_depth = 0
                    for d in sorted(info_lines_by_depth.keys(), reverse=True):
                        ranks = info_lines_by_depth[d]
                        if all(k in ranks for k in range(1, requested_mpv + 1)):
                            complete_depth = d
                            break
                    if complete_depth == 0:
                        complete_depth = last_depth_seen

                    # Parse bestmove
                    bestmove_uci = ""
                    if bestmove_line:
                        parts = bestmove_line.split()
                        if len(parts) >= 2:
                            bestmove_uci = parts[1]

                    # Parse multipv lines at complete_depth
                    multipv_results = []
                    if complete_depth in info_lines_by_depth:
                        for rank in sorted(info_lines_by_depth[complete_depth].keys()):
                            if rank > requested_mpv:
                                continue
                            info_line = info_lines_by_depth[complete_depth][rank]
                            parsed = self._parse_info_line(info_line, board, rank)
                            if parsed:
                                multipv_results.append(parsed)

                    # Sort by rank
                    multipv_results.sort(key=lambda x: x["rank"])

                    # Convert bestmove to SAN (do NOT mutate the analysis board)
                    bestmove_san = ""
                    if bestmove_uci:
                        try:
                            bestmove_san = board.san(chess.Move.from_uci(bestmove_uci))
                        except ValueError:
                            bestmove_san = bestmove_uci

                    # Convert PV UCI to SAN for each multipv line
                    for mpv in multipv_results:
                        pv_uci = mpv["pv_uci"]
                        pv_san = []
                        temp_board = board.copy()  # analysis position, unmodified
                        for uci in pv_uci:
                            try:
                                move = chess.Move.from_uci(uci)
                                pv_san.append(temp_board.san(move))
                                temp_board.push(move)
                            except ValueError:
                                pv_san.append(uci)
                        mpv["uci"] = pv_uci[0] if pv_uci else ""
                        mpv["san"] = pv_san[0] if pv_san else ""
                        mpv["pv_san"] = pv_san
                        mpv["pv_text"] = " ".join(pv_san)

                    # Overall stats from the first (rank 1) line at complete_depth
                    overall = {}
                    if multipv_results:
                        first = multipv_results[0]
                        overall = {
                            "nodes": first.get("nodes", 0),
                            "nps": first.get("nps", 0),
                            "time_ms": first.get("time_ms", 0),
                            "hashfull": first.get("hashfull", 0),
                        }

                    self._last_error = None
                    return {
                        "ok": True,
                        "depth": complete_depth,
                        "bestmove": {"uci": bestmove_uci, "san": bestmove_san},
                        "multipv": multipv_results,
                        **overall,
                    }
            except (RuntimeError, EOFError, BrokenPipeError, TimeoutError) as e:
                self._last_error = str(e)
                if attempt == 0:
                    self.respawn()
                    continue
                raise

    def _parse_info_line(self, line: str, board: chess.Board, rank: int) -> Optional[Dict[str, Any]]:
        """Parse a single UCI info line into a dict."""
        parts = line.split()
        result = {"rank": rank}

        # score
        try:
            score_idx = parts.index("score")
            score_type = parts[score_idx + 1]
            if score_type == "cp":
                result["score_cp"] = int(parts[score_idx + 2])
                result["score_mate"] = None
            elif score_type == "mate":
                mate_value = int(parts[score_idx + 2])
                result["score_mate"] = mate_value
                result["score_cp"] = mate_score_to_cp(mate_value)
        except (ValueError, IndexError):
            result["score_cp"] = None
            result["score_mate"] = None

        # wdl
        result["wdl"] = None
        try:
            wdl_idx = parts.index("wdl")
            w = int(parts[wdl_idx + 1])
            d = int(parts[wdl_idx + 2])
            l = int(parts[wdl_idx + 3])
            result["wdl"] = [w, d, l]
        except (ValueError, IndexError):
            pass

        # seldepth
        try:
            sd_idx = parts.index("seldepth")
            result["seldepth"] = int(parts[sd_idx + 1])
        except (ValueError, IndexError):
            result["seldepth"] = 0

        # nodes
        try:
            n_idx = parts.index("nodes")
            result["nodes"] = int(parts[n_idx + 1])
        except (ValueError, IndexError):
            result["nodes"] = 0

        # nps
        try:
            nps_idx = parts.index("nps")
            result["nps"] = int(parts[nps_idx + 1])
        except (ValueError, IndexError):
            result["nps"] = 0

        # time
        try:
            t_idx = parts.index("time")
            result["time_ms"] = int(parts[t_idx + 1])
        except (ValueError, IndexError):
            result["time_ms"] = 0

        # hashfull
        try:
            hf_idx = parts.index("hashfull")
            result["hashfull"] = int(parts[hf_idx + 1])
        except (ValueError, IndexError):
            result["hashfull"] = 0

        # pv
        result["pv_uci"] = []
        try:
            pv_idx = parts.index("pv")
            result["pv_uci"] = parts[pv_idx + 1:]
        except ValueError:
            pass

        # Only return if we have a PV or score
        if not result["pv_uci"] and result["score_cp"] is None and result["score_mate"] is None:
            return None
        return result

    def health(self, experience: Optional[ExperienceStore] = None) -> Dict[str, Any]:
        """Return health status."""
        alive = self.proc is not None and self.proc.poll() is None
        pid = self.proc.pid if self.proc else None
        h = {
            "status": "ok" if alive else "dead",
            "engine": "duckish",
            "pid": pid,
            "multipv_default": self.default_multipv,
            "alive": alive,
            "book_path": BOOK_PATH if self._book_loaded else "",
            "book_loaded": self._book_loaded,
            "book_hits": self._book_hits,
            "threads": self._threads,
            "hash_mb": self._hash_mb,
            "multipv": self.default_multipv,
            "last_error": self._last_error,
        }
        if experience:
            exp_stats = experience.get_stats()
            h.update({
                "experience_positions": exp_stats.get("positions_count", 0),
                "experience_moves": exp_stats.get("moves_count", 0),
                "experience_games_won": exp_stats.get("games_won", 0),
                "experience_games_lost": exp_stats.get("games_lost", 0),
                "experience_games_drawn": exp_stats.get("games_drawn", 0),
                "experience_mistakes": exp_stats.get("mistakes_corrected", 0),
                "experience_recent_lessons": exp_stats.get("recent_lessons", []),
            })
        return h

    def shutdown(self) -> None:
        """Terminate the engine process."""
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


# ---- HTTP Request Handler ---------------------------------------------------

class BridgeHandler(BaseHTTPRequestHandler):
    engine: Engine  # set by server
    default_movetime: int
    experience: ExperienceStore  # set by server
    _current_game_moves: List[str] = []  # track moves for post-mortem

    def do_OPTIONS(self) -> None:
        self._send_cors()
        self.response_code = 204
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        start = time.monotonic()
        path = urlparse(self.path).path
        try:
            if path == "/health":
                data = self.engine.health(self.experience)
                self._send_json(200, data)
            elif path == "/":
                self._send_html(200, self._dashboard_html(self.engine.health(self.experience)))
            else:
                self._send_json(404, {"ok": False, "error": "not found"})
        except (ConnectionAbortedError, BrokenPipeError):
            # Client disconnected - ignore silently
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
        finally:
            self._log_request(start)

    def _dashboard_html(self, h: Dict[str, Any]) -> str:
        status = "ALIVE" if h.get("alive") else "DEAD"
        color = "#22c55e" if h.get("alive") else "#ef4444"
        
        # Format recent lessons readably
        recent = h.get("experience_recent_lessons") or []
        if recent:
            lesson_strs = []
            for lesson in recent:
                move_uci = lesson.get("move_uci", "?")
                penalty = lesson.get("penalty_cp", 0.0)
                bonus = lesson.get("bonus_cp", 0.0)
                visits = lesson.get("visits", 0)
                lesson_strs.append(f"{move_uci} | pen {penalty:.0f} | bon {bonus:.0f} | visits {visits}")
            recent_formatted = "; ".join(lesson_strs)
        else:
            recent_formatted = "none"
        
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Duckish Bridge Status</title>
<style>
body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:2rem}}
.card{{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:1.5rem;max-width:600px;margin:auto}}
h1{{margin:0 0 1rem;font-size:1.5rem}}
h2{{margin:1.5rem 0 0.5rem;font-size:1.1rem;color:#94a3b8;border-top:1px solid #334155;padding-top:1rem}}
.row{{display:flex;justify-content:space-between;padding:.4rem 0;border-bottom:1px solid #334155}}
.row:last-child{{border-bottom:none}}
.label{{color:#94a3b8}}
.value{{font-family:ui-monospace,monospace;color:#fff}}
.status{{color:{color};font-weight:600}}
</style></head><body>
<div class="card">
<h1>Duckish Bridge</h1>
<div class="row"><span class="label">Status</span><span class="value status">{status}</span></div>
<div class="row"><span class="label">PID</span><span class="value">{h.get('pid','-')}</span></div>
<div class="row"><span class="label">Book</span><span class="value">{h.get('book_path','-') or 'none'}</span></div>
<div class="row"><span class="label">Book Loaded</span><span class="value">{h.get('book_loaded',False)}</span></div>
<div class="row"><span class="label">Book Hits</span><span class="value">{h.get('book_hits',0)}</span></div>
<div class="row"><span class="label">Threads</span><span class="value">{h.get('threads',1)}</span></div>
<div class="row"><span class="label">Hash (MB)</span><span class="value">{h.get('hash_mb',256)}</span></div>
<div class="row"><span class="label">MultiPV</span><span class="value">{h.get('multipv',3)}</span></div>
<div class="row"><span class="label">Last Error</span><span class="value">{h.get('last_error') or 'none'}</span></div>
<h2>Experience Book</h2>
<div class="row"><span class="label">Positions</span><span class="value">{h.get('experience_positions',0)}</span></div>
<div class="row"><span class="label">Move Records</span><span class="value">{h.get('experience_moves',0)}</span></div>
<div class="row"><span class="label">Games Won</span><span class="value">{h.get('experience_games_won',0)}</span></div>
<div class="row"><span class="label">Games Lost</span><span class="value">{h.get('experience_games_lost',0)}</span></div>
<div class="row"><span class="label">Games Drawn</span><span class="value">{h.get('experience_games_drawn',0)}</span></div>
<div class="row"><span class="label">Mistakes Corrected</span><span class="value">{h.get('experience_mistakes',0)}</span></div>
<div class="row"><span class="label">Recent Lessons</span><span class="value">{recent_formatted}</span></div>
</div></body></html>"""

    def do_POST(self) -> None:
        start = time.monotonic()
        path = urlparse(self.path).path
        try:
            if path == "/analyze":
                self._handle_analyze()
            elif path == "/game-over":
                self._handle_game_over()
            else:
                self._send_json(404, {"ok": False, "error": "not found"})

        except (ConnectionAbortedError, BrokenPipeError):
            # Client disconnected - ignore silently
            return
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
        except TimeoutError:
            self._send_json(500, {"ok": False, "error": "engine timeout"})
        except RuntimeError as e:
            self._send_json(500, {"ok": False, "error": str(e)})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"internal error: {e}"})
        finally:
            self._log_request(start)

    def _handle_analyze(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return

        moves = req.get("moves", [])
        if not isinstance(moves, list):
            self._send_json(400, {"ok": False, "error": "moves must be a list"})
            return

        # Normalize: strip all whitespace from each SAN move (e.g. "N f3" -> "Nf3")
        moves = [re.sub(r'\s+', '', m) for m in moves]

        # Track moves for post-mortem (only if new moves appended)
        if len(moves) > len(self._current_game_moves):
            self._current_game_moves = moves[:]

        movetime = req.get("movetime", self.default_movetime)
        movetime = max(50, min(5000, int(movetime)))

        multipv = req.get("multipv", self.engine.default_multipv)
        multipv = max(1, min(10, int(multipv)))

        depth = req.get("depth")
        if depth is not None:
            depth = int(depth)
            if depth < 1:
                depth = None

        result = self.engine.analyze(moves, movetime, multipv, depth)

        # Apply learned adjustments
        if result.get("ok") and result.get("multipv"):
            board = self._moves_to_board(moves)
            fen = board.fen()
            # Record position with deep score (from best line)
            best_score = result["multipv"][0].get("score_cp")
            self.experience.record_position(fen, best_score, None)
            # Apply adjustments and re-rank
            adjusted = self.experience.apply_adjustments(fen, result["multipv"])
            result["multipv"] = adjusted
            result["learned"] = any(c.get("learned", False) for c in adjusted)

        # Inference fallback: if game is over, run post-mortem once
        board = self._moves_to_board(moves)
        if board.is_game_over():
            self._run_post_mortem_if_needed(moves, board)

        self._send_json(200, result)

    def _handle_game_over(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return

        result = req.get("result")  # "win" | "loss" | "draw" | null
        winner = req.get("winner")  # "white" | "black" | "draw" | null
        moves = req.get("moves", [])
        if not isinstance(moves, list):
            self._send_json(400, {"ok": False, "error": "moves must be a list"})
            return

        # Run post-mortem with explicit result
        board = self._moves_to_board(moves)
        self._run_post_mortem(moves, board, result, winner)

        # Clear tracked game
        self._current_game_moves = []

        self._send_json(200, {"ok": True})

    def _moves_to_board(self, moves: List[str]) -> chess.Board:
        """Convert SAN moves list to a chess.Board."""
        board = chess.Board()
        for san in moves:
            try:
                move = board.parse_san(san)
                board.push(move)
            except ValueError:
                break
        return board

    def _run_post_mortem_if_needed(self, moves: List[str], board: chess.Board) -> None:
        """Run post-mortem if game just ended (inference fallback)."""
        # Only run once per game end - check if we haven't recorded this game yet
        result = None
        winner = None
        if board.is_checkmate():
            result = "loss"  # side to move lost
            winner = "black" if board.turn == chess.WHITE else "white"
        elif board.is_stalemate() or board.is_insufficient_material() or board.is_seventyfive_moves() or board.is_fivefold_repetition():
            result = "draw"
            winner = "draw"
        else:
            return
        self._run_post_mortem(moves, board, result, winner)
        self._current_game_moves = []

    def _run_post_mortem(self, moves: List[str], board: chess.Board, result: Optional[str], winner: Optional[str]) -> None:
        """
        Post-mortem analysis:
        - For each move with eval drop > BLUNDER_THRESHOLD_CP, record penalty
        - If loss, penalize final losing move
        - Record corrective bonus for engine best move at blunder positions
        """
        if len(moves) < 2:
            return

        # Replay game and collect evaluations at each position
        # We need to re-analyze each position to get scores
        # For MVP, we'll use a simplified approach: analyze each position with the engine
        # but to avoid too many engine calls, we'll only check positions where we have moves
        
        eval_history = []  # list of (fen_before, move_uci, score_cp_after)
        temp_board = chess.Board()
        
        for i, san in enumerate(moves):
            try:
                fen_before = temp_board.fen()
                move = temp_board.parse_san(san)
                move_uci = move.uci()
                temp_board.push(move)
                fen_after = temp_board.fen()
                
                # Analyze position after move to get score from opponent's perspective
                # We'll do a quick shallow analysis
                try:
                    analysis = self.engine.analyze(moves[:i+1], 100, 1, depth=10)
                    if analysis.get("ok") and analysis.get("multipv"):
                        score_cp = analysis["multipv"][0].get("score_cp")
                        if score_cp is not None:
                            # Score is from side to move's perspective
                            eval_history.append((fen_before, move_uci, score_cp))
                except Exception:
                    pass  # Skip if analysis fails
                    
            except ValueError:
                break

        # Find blunders: eval drops > threshold between consecutive positions
        for i in range(1, len(eval_history)):
            fen_before, move_uci, score_after = eval_history[i]
            _, _, score_before = eval_history[i-1]
            # Score is from side to move perspective, so a drop for the player who just moved
            # means score_before (their perspective before move) vs -score_after (their perspective after opponent's reply)
            # Actually, score_before is from player A's perspective before move
            # score_after is from player B's perspective after move
            # The eval change for player A is -score_after - score_before
            drop = -score_after - score_before
            if drop > BLUNDER_THRESHOLD_CP:
                # Record penalty for the move that caused the blunder
                self.experience.record_move(fen_before, move_uci, penalty_cp=drop, bonus_cp=0.0)
                # Record corrective bonus for engine's best move at that position
                try:
                    analysis = self.engine.analyze(moves[:i], 200, 1, depth=12)
                    if analysis.get("ok") and analysis.get("multipv"):
                        best_uci = analysis["multipv"][0].get("uci")
                        if best_uci and best_uci != move_uci:
                            self.experience.record_move(fen_before, best_uci, penalty_cp=0.0, bonus_cp=LEARNING_BONUS_CP)
                except Exception:
                    pass

        # If loss, also penalize the final move
        if result == "loss" and eval_history:
            fen_before, move_uci, _ = eval_history[-1]
            self.experience.record_move(fen_before, move_uci, penalty_cp=BLUNDER_THRESHOLD_CP, bonus_cp=0.0)

        # Record final position with result
        final_fen = board.fen()
        # Get deep score for final position if available
        deep_score = None
        if eval_history:
            deep_score = eval_history[-1][2]
        self.experience.record_position(final_fen, deep_score, result)

    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status: int, data: Dict[str, Any]) -> None:
        self.response_code = status
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._send_cors()
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _send_html(self, status: int, html: str) -> None:
        self.response_code = status
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._send_cors()
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _log_request(self, start: float) -> None:
        elapsed = int((time.monotonic() - start) * 1000)
        code = getattr(self, "response_code", "-")
        try:
            print(f"{self.command} {self.path} -> {code} ({elapsed}ms)", flush=True)
        except Exception:
            pass

    def log_message(self, format: str, *args) -> None:
        # Suppress default log_message
        pass


# ---- Main -------------------------------------------------------------------

def run_server(port: int, default_movetime: int, threads: Optional[int] = None) -> None:
    engine = Engine(ENGINE_PATH, DEFAULT_MULTIPV, threads=threads)
    experience = ExperienceStore(DEFAULT_DB_PATH)
    handler_class = type(
        "Handler",
        (BridgeHandler,),
        {"engine": engine, "default_movetime": default_movetime, "experience": experience},
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_class)
    print(f"Bridge server listening on http://0.0.0.0:{port}", flush=True)
    print(f"Engine: {ENGINE_PATH}", flush=True)
    print(f"Default MultiPV: {DEFAULT_MULTIPV}, Default movetime: {default_movetime}ms", flush=True)
    print(f"Experience DB: {DEFAULT_DB_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
    finally:
        engine.shutdown()
        experience.close()
        server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Duckish Bridge Server")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    parser.add_argument("--movetime", type=int, default=400, help="Default movetime in ms (default: 400)")
    parser.add_argument("--threads", type=int, default=None, help="Engine threads (default: auto-detect half of CPU cores)")
    args = parser.parse_args()
    threads = args.threads if args.threads is not None else (int(os.environ.get("DUCKISH_THREADS") or 0) or None)
    run_server(args.port, args.movetime, threads)