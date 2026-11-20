#!/usr/bin/env python3
"""
UCI engine subprocess management for the Duckish Lichess bot.
Pure stdlib (os, sys, time, threading, subprocess, queue, typing).

Mirrors the engine wrapper used by bridge/server.py but speaks in UCI move
coordinates only; SAN->UCI conversion lives in the runner.
"""
import os
import sys
import time
import queue
import threading
import subprocess
from typing import Any, Dict, List, Optional

from .config import (
    REPO_ROOT,
    BOOK_PATH,
    DEFAULT_HASH_MB,
    DEFAULT_THREADS,
    MOVE_OVERHEAD_MS,
    CONTEMPT,
)

SEARCH_DEADLINE_S = 10  # hard cap on a single search

# Syzygy tablebase directory (repo root /syzygy). Optional: created by
# bridge/download_syzygy.py. The engine probes it only when <= N pieces remain.
SYZYGY_PATH = os.path.join(REPO_ROOT, "syzygy")


class Engine:
    """Manages the UCI engine subprocess with thread-safe I/O."""

    def __init__(
        self,
        exe_path: str,
        hash_mb: int = DEFAULT_HASH_MB,
        threads: int = DEFAULT_THREADS,
    ):
        self.exe_path = exe_path
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._alive = False
        self._book_loaded = False
        self._tb_loaded = False
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
        proc = self.proc
        lines = self._lines
        self._reader_thread = threading.Thread(target=self._read_stdout_loop, args=(proc, lines), daemon=True)
        self._reader_thread.start()

        self._stderr_thread = threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True)
        self._stderr_thread.start()

        # UCI handshake
        self._send("uci")
        self._read_until(lambda line: line.strip() == "uciok", 10)
        self._send("isready")
        self._read_until(lambda line: line.strip() == "readyok", 10)
        self._send(f"setoption name Hash value {self._hash_mb}")
        self._send("setoption name MultiPV value 1")  # Lazy SMP strength
        self._send(f"setoption name Threads value {self._threads}")
        self._send(f"setoption name Contempt value {CONTEMPT}")
        if BOOK_PATH and os.path.isfile(BOOK_PATH):
            self._send(f"setoption name BookPath value {BOOK_PATH}")
            self._book_loaded = True
        else:
            self._book_loaded = False
        if os.path.isdir(SYZYGY_PATH):
            # Only set SyzygyPath if tablebase files are actually present
            try:
                has_tb = any(f.lower().endswith((".rtbw", ".rtbz")) for f in os.listdir(SYZYGY_PATH))
            except OSError:
                has_tb = False
            if has_tb:
                self._send(f"setoption name SyzygyPath value {SYZYGY_PATH}")
                self._tb_loaded = True
            else:
                self._tb_loaded = False
        else:
            self._tb_loaded = False

    def _drain_stderr(self, proc) -> None:
        try:
            if proc and proc.stderr:
                for _ in proc.stderr:
                    pass
        except Exception:
            pass

    def _read_stdout_loop(self, proc, lines) -> None:
        try:
            if proc and proc.stdout:
                for line in proc.stdout:
                    lines.put(line.rstrip("\n"))
        except Exception:
            pass
        finally:
            lines.put(None)  # sentinel into THIS thread's captured queue

    def _send(self, cmd: str) -> None:
        p = self.proc
        if p is None or p.stdin is None or p.poll() is not None:
            raise RuntimeError("engine stdin not available")
        try:
            p.stdin.write(cmd + "\n")
            p.stdin.flush()
        except OSError as e:
            raise BrokenPipeError(str(e)) from e

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

    def best_move(
        self,
        moves_uci: List[str],
        wtime: Optional[int] = None,  # our clock in ms (with btime activates book)
        btime: Optional[int] = None,  # opponent clock in ms
        winc: Optional[int] = None,   # white increment in ms
        binc: Optional[int] = None,   # black increment in ms
        movetime: Optional[int] = None,
    ) -> Optional[str]:
        """Run search and return bestmove UCI.

        A movetime cap is always applied, whether or not real clocks are sent.
        When both wtime and btime are supplied, real clocks are sent too, so the
        engine's Polyglot book and its own time manager activate; the movetime cap
        then hard-caps the search (engine limits are conjunctive). Without real
        clocks a fixed movetime is used. wtime/btime/winc/binc are buffered by
        MOVE_OVERHEAD_MS so we never flag on network latency. One respawn retry
        on transport failure.
        """
        for attempt in range(2):
            try:
                with self.lock:
                    self._ensure_alive()

                    pos_cmd = "position startpos"
                    if moves_uci:
                        pos_cmd += " moves " + " ".join(moves_uci)
                    self._send(pos_cmd)

                    # Use real clocks when available: this activates Polyglot book
                    # probing and the engine's own time manager. `go movetime` skips the book.
                    cap_ms = movetime if movetime is not None else 1000
                    if wtime is not None and btime is not None:
                        # Real clocks keep Limits::Dynamic alive so the Polyglot book is
                        # probed (src/search.rs:242), while movetime hard-caps the search
                        # (limits are conjunctive, timemgmt.rs:296-299). Without the movetime
                        # cap the engine's own time manager burns the whole clock.
                        wt = max(0, wtime - MOVE_OVERHEAD_MS)
                        bt = max(0, btime - MOVE_OVERHEAD_MS)
                        go_cmd = f"go wtime {wt} btime {bt} movetime {cap_ms}"
                        if winc is not None:
                            go_cmd += f" winc {winc}"
                        if binc is not None:
                            go_cmd += f" binc {binc}"
                        self._send(go_cmd)
                        deadline = time.monotonic() + (cap_ms / 1000.0) + 5.0
                    else:
                        self._send(f"go movetime {cap_ms}")
                        deadline = time.monotonic() + max(SEARCH_DEADLINE_S, (cap_ms / 1000.0) + 5.0)

                    bestmove_uci = ""
                    while True:
                        line = self._read_line(deadline)
                        if line is None:
                            raise RuntimeError("engine process died during search")
                        if line.startswith("bestmove"):
                            pieces = line.split()
                            if len(pieces) >= 2:
                                bestmove_uci = pieces[1]
                            break

                    self._last_error = None
                    return bestmove_uci if bestmove_uci else None
            except (RuntimeError, EOFError, OSError, TimeoutError) as e:
                self._last_error = str(e)
                if attempt == 0:
                    self.respawn()
                    continue
                raise

    def best_moves_multipv(
        self,
        moves_uci: List[str],
        movetime: int = 1500,
        multipv: int = 3,
    ) -> List[Dict[str, Any]]:
        """Run MultiPV search and return list of {uci, score_cp, rank} dicts.
        
        Returns moves sorted by rank (1 = best). Each dict has:
        - uci: move in UCI notation
        - score_cp: centipawn score from side-to-move perspective
        - rank: 1-based rank
        """
        results: List[Dict[str, Any]] = []
        for attempt in range(2):
            try:
                with self.lock:
                    self._ensure_alive()

                    pos_cmd = "position startpos"
                    if moves_uci:
                        pos_cmd += " moves " + " ".join(moves_uci)
                    self._send(pos_cmd)

                    self._send(f"setoption name MultiPV value {multipv}")
                    self._send(f"go movetime {movetime}")

                    deadline = time.monotonic() + max(SEARCH_DEADLINE_S * 2, (movetime / 1000.0) + 5.0)
                    # Parse info lines with score and pv
                    info_by_depth: Dict[int, Dict[int, Dict]] = {}  # depth -> rank -> info
                    while True:
                        line = self._read_line(deadline)
                        if line is None:
                            raise RuntimeError("engine process died during search")
                        if line.startswith("bestmove"):
                            pieces = line.split()
                            bestmove = pieces[1] if len(pieces) >= 2 else ""
                            break
                        if line.startswith("info") and "score" in line and "pv" in line:
                            # Parse: info depth 20 seldepth 25 multipv 1 score cp 35 nodes 1234 pv e2e4 e7e5
                            parts = line.split()
                            depth = 0
                            rank = 1
                            score_cp = 0
                            pv_move = ""
                            for i, p in enumerate(parts):
                                if p == "depth" and i + 1 < len(parts):
                                    depth = int(parts[i + 1])
                                elif p == "multipv" and i + 1 < len(parts):
                                    rank = int(parts[i + 1])
                                elif p == "score" and i + 1 < len(parts):
                                    if parts[i + 1] == "cp" and i + 2 < len(parts):
                                        score_cp = int(parts[i + 2])
                                    elif parts[i + 1] == "mate" and i + 2 < len(parts):
                                        mate = int(parts[i + 2])
                                        score_cp = 100000 - abs(mate) * 1000 if mate > 0 else -100000 + abs(mate) * 1000
                                elif p == "pv" and i + 1 < len(parts):
                                    pv_move = parts[i + 1]
                            if pv_move:
                                if depth not in info_by_depth:
                                    info_by_depth[depth] = {}
                                info_by_depth[depth][rank] = {
                                    "uci": pv_move,
                                    "score_cp": score_cp,
                                    "rank": rank
                                }
                    
                    # Reset MultiPV to 1
                    self._send("setoption name MultiPV value 1")
                    
                    # Find deepest depth with all ranks
                    if info_by_depth:
                        max_depth = max(info_by_depth.keys())
                        # Find deepest depth that has all ranks 1..multipv
                        for d in sorted(info_by_depth.keys(), reverse=True):
                            if len(info_by_depth[d]) >= multipv:
                                results = [info_by_depth[d][r] for r in sorted(info_by_depth[d].keys())]
                                break
                        else:
                            # Fallback: use whatever we have
                            max_d = max(info_by_depth.keys())
                            results = [info_by_depth[max_d][r] for r in sorted(info_by_depth[max_d].keys())]
                    
                    if not results and bestmove:
                        results = [{"uci": bestmove, "score_cp": 0, "rank": 1}]
                    
                    return results
            except (RuntimeError, EOFError, OSError, TimeoutError) as e:
                self._last_error = str(e)
                if attempt == 0:
                    self.respawn()
                    continue
                raise
        return results

    def tb_loaded(self) -> bool:
        return self._tb_loaded

    def health(self) -> Dict[str, Any]:
        alive = self.proc is not None and self.proc.poll() is None
        return {
            "status": "ok" if alive else "dead",
            "engine": "duckish",
            "pid": self.proc.pid if self.proc else None,
            "alive": alive,
            "book_loaded": self._book_loaded,
            "tb_loaded": self._tb_loaded,
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