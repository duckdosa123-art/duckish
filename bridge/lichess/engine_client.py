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
)

SEARCH_DEADLINE_S = 30  # hard cap on a single search


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
        self._send("setoption name MultiPV value 1")  # Lazy SMP strength
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

    def best_move(
        self,
        moves_uci: List[str],
        wtime: Optional[int] = None,
        btime: Optional[int] = None,
        winc: Optional[int] = None,
        binc: Optional[int] = None,
    ) -> Optional[str]:
        """Run search and return bestmove UCI.

        Uses wtime/btime/winc/binc with an overhead buffer so we never flag on
        network latency. One respawn retry on transport failure.
        """
        for attempt in range(2):
            try:
                with self.lock:
                    self._ensure_alive()

                    pos_cmd = "position startpos"
                    if moves_uci:
                        pos_cmd += " moves " + " ".join(moves_uci)
                    self._send(pos_cmd)

                    # Build go command with time management
                    parts = ["go"]
                    if wtime is not None:
                        parts.append(f"wtime {max(0, wtime - MOVE_OVERHEAD_MS)}")
                    if btime is not None:
                        parts.append(f"btime {max(0, btime - MOVE_OVERHEAD_MS)}")
                    if winc is not None:
                        parts.append(f"winc {winc}")
                    if binc is not None:
                        parts.append(f"binc {binc}")
                    self._send(" ".join(parts))

                    deadline = time.monotonic() + SEARCH_DEADLINE_S
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