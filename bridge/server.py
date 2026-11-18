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
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Optional, List, Dict, Any, Tuple
import chess

# ---- Configuration ----------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_PATH = os.path.join(REPO_ROOT, "target", "release", "duckish.exe")
DEFAULT_MULTIPV = int(os.environ.get("DUCKISH_MULTIPV", "3"))
DEFAULT_MULTIPV = max(1, min(10, DEFAULT_MULTIPV))
DEFAULT_MOVETIME = 400  # ms
HANDSHAKE_TIMEOUT = 10  # seconds
ANALYSIS_TIMEOUT = 15   # seconds

# ---- Engine Subprocess Management -------------------------------------------

class Engine:
    """Manages the UCI engine subprocess with thread-safe I/O."""

    def __init__(self, exe_path: str, default_multipv: int):
        self.exe_path = exe_path
        self.default_multipv = default_multipv
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._lines = queue.Queue()
        self._alive = False
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
        self._send(f"setoption name Hash value 256")
        self._send(f"setoption name MultiPV value {self.default_multipv}")
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

            return {
                "ok": True,
                "depth": complete_depth,
                "bestmove": {"uci": bestmove_uci, "san": bestmove_san},
                "multipv": multipv_results,
                **overall,
            }

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
                result["score_mate"] = int(parts[score_idx + 2])
                result["score_cp"] = None
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

    def health(self) -> Dict[str, Any]:
        """Return health status."""
        alive = self.proc is not None and self.proc.poll() is None
        pid = self.proc.pid if self.proc else None
        return {
            "status": "ok" if alive else "dead",
            "engine": "duckish",
            "pid": pid,
            "multipv_default": self.default_multipv,
            "alive": alive,
        }

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
                data = self.engine.health()
                self._send_json(200, data)
            else:
                self._send_json(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
        finally:
            self._log_request(start)

    def do_POST(self) -> None:
        start = time.monotonic()
        path = urlparse(self.path).path
        try:
            if path != "/analyze":
                self._send_json(404, {"ok": False, "error": "not found"})
                return

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
            self._send_json(200, result)

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

def run_server(port: int, default_movetime: int) -> None:
    engine = Engine(ENGINE_PATH, DEFAULT_MULTIPV)
    handler_class = type(
        "Handler",
        (BridgeHandler,),
        {"engine": engine, "default_movetime": default_movetime},
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_class)
    print(f"Bridge server listening on http://0.0.0.0:{port}", flush=True)
    print(f"Engine: {ENGINE_PATH}", flush=True)
    print(f"Default MultiPV: {DEFAULT_MULTIPV}, Default movetime: {default_movetime}ms", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
    finally:
        engine.shutdown()
        server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Duckish Bridge Server")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    parser.add_argument("--movetime", type=int, default=400, help="Default movetime in ms (default: 400)")
    args = parser.parse_args()
    run_server(args.port, args.movetime)