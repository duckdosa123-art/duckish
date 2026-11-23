#!/usr/bin/env python3
"""Tests for the repetition guard and the end-of-game move gate in runner.py.

Covers two live-game bugs (sg6UKyZX):

1. Repetition guard "avoids repetition" by playing the SAME move. With a
   repeat threshold of 1 (fast mode / >20s ahead), a position seen once made
   the guard fire, but the candidate filter used a hardcoded `>= 2`, so the
   engine's own move was accepted as the "replacement" -> the bot logged
   "avoiding repetition" and replayed the identical move into a threefold.

2. A move was submitted AFTER the game finished (HTTP 400). The event-stream
   thread can finish the game while the game-stream thread is mid-search; the
   submit had no state re-check.

Style mirrors bridge/test_draw_logic.py: plain functions returning bool,
printed PASS/FAIL, main() runner, sys.path insertion for the `bridge` package,
exit code 0 = all passed / 1 = at least one failed. No pytest. Offline stubs
only. Run from anywhere:

    python bridge/test_repetition_guard.py

TDD RED phase: r01, r02, r05 (helper missing), r06 fail before the fix.
"""

import os
import sys
from typing import Any, Dict, List, Optional

# Make the bridge package importable regardless of the caller's cwd.
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

import chess
from lichess.runner import GameHandler


# ---- Fixture keys (derived by hand with python-chess; expectations are
# ---- literals, not computed from production helpers) ----------------------

def _key_after(fen: str, uci: str) -> str:
    b = chess.Board() if fen == "startpos" else chess.Board(fen)
    b.push_uci(uci)
    return " ".join(b.fen().split()[:4])


KEY_E2E4 = _key_after("startpos", "e2e4")
KEY_D2D4 = _key_after("startpos", "d2d4")


# ---- Offline harness -------------------------------------------------------

class DrawStubClient:
    def __init__(self):
        self.last_uci: Optional[str] = None
        self.make_move_calls: List[Dict[str, str]] = []

    def make_move(self, game_id: str, uci: str, offering_draw: bool = False) -> Dict[str, Any]:
        self.last_uci = uci
        self.make_move_calls.append({"game_id": game_id, "uci": uci, "offering_draw": offering_draw})
        return {"ok": True}

    def accept_draw(self, game_id: str) -> Dict[str, Any]:
        return {"ok": True}

    def decline_draw(self, game_id: str) -> Dict[str, Any]:
        return {"ok": True}


class MockEngine:
    """Returns a fixed bestmove and a fixed MultiPV candidate list."""

    def __init__(self, bestmove: str = "e2e4", candidates=None):
        self.bestmove_uci = bestmove
        self.candidates = candidates if candidates is not None else [
            {"uci": "e2e4", "score_cp": 10, "rank": 1},
            {"uci": "d2d4", "score_cp": 5, "rank": 2},
        ]

    def best_move(self, moves_uci, **kwargs):
        return self.bestmove_uci

    def best_moves_multipv(self, moves_uci, movetime=1500, multipv=3):
        return self.candidates


def make_handler(engine=None, client=None, wtime=300000, btime=100000,
                 color="white") -> GameHandler:
    """Build a GameHandler via object.__new__ (never start the game thread)."""
    h = object.__new__(GameHandler)
    h.client = client if client is not None else DrawStubClient()
    h.engine = engine if engine is not None else MockEngine()
    h.experience = None
    h.game_id = "TEST"
    h.color = color
    h.initial_fen = "startpos"
    h.moves_uci = []
    h.wtime = wtime
    h.btime = btime
    h.winc = 2000
    h.binc = 2000
    h.variant = "standard"
    h.rated = True
    h.state = None
    h.our_rating = None
    h.opponent_rating = None
    h._draw_offer_seen = False
    h._fast_mode = False
    h._fast_mode_source = None
    h._running = True
    h._finished = False
    h._position_counts = {}
    h.logs: List[str] = []
    h._log = lambda msg, _logs=h.logs: _logs.append(msg)
    if hasattr(h, "_rebuild_position_counts"):
        h._rebuild_position_counts()
    return h


def assert_eq(label: str, got: Any, want: Any) -> bool:
    if got != want:
        print(f"FAIL {label}: expected {want!r}, got {got!r}")
        return False
    return True


def assert_no_log(logs: List[str], needle: str, label: str) -> bool:
    bad = [l for l in logs if needle in l]
    if bad:
        print(f"FAIL {label}: unexpected log containing {needle!r}: {bad}")
        return False
    return True


# ===========================================================================
# BUG 1: repetition guard must not replay the same move
# ===========================================================================

def r01_same_move_not_selected():
    """Threshold 1, position after e2e4 seen once. The guard fires, but e2e4
    itself repeats (count 1 >= 1), so it must be rejected and d2d4 (count 0)
    chosen instead."""
    client = DrawStubClient()
    h = make_handler(engine=MockEngine(), client=client)
    h._position_counts = {KEY_E2E4: 1}
    h._make_move()
    return assert_eq("r01 last_uci", client.last_uci, "d2d4")


def r02_no_false_avoid_log():
    """The guard must not log that it is avoiding repetition by playing e2e4."""
    client = DrawStubClient()
    h = make_handler(engine=MockEngine(), client=client)
    h._position_counts = {KEY_E2E4: 1}
    h._make_move()
    return assert_no_log(h.logs, "playing e2e4 instead", "r02")


def r03_no_avoid_log_when_no_alternative():
    """Both e2e4 and d2d4 lead to positions already seen once (threshold 1).
    No fresh alternative exists, so the guard must NOT log 'Avoiding
    repetition' and must keep the engine's move e2e4."""
    client = DrawStubClient()
    h = make_handler(engine=MockEngine(), client=client)
    h._position_counts = {KEY_E2E4: 1, KEY_D2D4: 1}
    h._make_move()
    ok = assert_no_log(h.logs, "Avoiding repetition", "r03")
    ok &= assert_eq("r03 last_uci", client.last_uci, "e2e4")
    return ok


def r04_threshold_two_still_switches():
    """Time ahead only 20s -> repeat_threshold 2. Position after e2e4 seen
    twice (2 >= 2) triggers the guard; d2d4 (count 0) must be chosen."""
    client = DrawStubClient()
    h = make_handler(engine=MockEngine(), client=client, wtime=300000, btime=280000)
    h._position_counts = {KEY_E2E4: 2}
    h._make_move()
    return assert_eq("r04 last_uci", client.last_uci, "d2d4")


def r05_helper_count_semantics():
    """_repeat_count_after returns the stored count of the position after the
    move (1 for e2e4, 0 for d2d4), 0 for an illegal uci, and must not mutate
    the board."""
    client = DrawStubClient()
    h = make_handler(engine=MockEngine(), client=client)
    h._position_counts = {KEY_E2E4: 1}
    board = chess.Board()
    before = board.fen()
    ok = assert_eq("r05 e2e4", h._repeat_count_after(board, "e2e4"), 1)
    ok &= assert_eq("r05 d2d4", h._repeat_count_after(board, "d2d4"), 0)
    ok &= assert_eq("r05 illegal", h._repeat_count_after(board, "e9e9"), 0)
    ok &= assert_eq("r05 board unmutated", board.fen(), before)
    return ok


# ===========================================================================
# BUG 2: never submit a move after the game has finished
# ===========================================================================

def r06_finished_flag_blocks_move():
    """If _finished is set (event thread ended the game mid-search), the move
    must not be submitted and a 'Game ended during search' line logged."""
    client = DrawStubClient()
    h = make_handler(engine=MockEngine(), client=client)
    h._position_counts = {}
    h._finished = True
    h._make_move()
    ok = assert_eq("r06 make_move_calls", len(client.make_move_calls), 0)
    ok &= assert_eq("r06 log", any("Game ended during search" in l for l in h.logs), True)
    return ok


def r07_running_flag_blocks_move():
    """If _running is False the move must not be submitted."""
    client = DrawStubClient()
    h = make_handler(engine=MockEngine(), client=client)
    h._position_counts = {}
    h._running = False
    h._make_move()
    return assert_eq("r07 make_move_calls", len(client.make_move_calls), 0)


def r08_normal_move_still_submitted():
    """No flags, no repeats -> the engine's move e2e4 is submitted normally."""
    client = DrawStubClient()
    h = make_handler(engine=MockEngine(), client=client)
    h._position_counts = {}
    h._make_move()
    return assert_eq("r08 last_uci", client.last_uci, "e2e4")


# ---- Runner ----------------------------------------------------------------

def main() -> int:
    tests = [
        r01_same_move_not_selected,
        r02_no_false_avoid_log,
        r03_no_avoid_log_when_no_alternative,
        r04_threshold_two_still_switches,
        r05_helper_count_semantics,
        r06_finished_flag_blocks_move,
        r07_running_flag_blocks_move,
        r08_normal_move_still_submitted,
    ]

    failures = 0
    passed = 0
    for test in tests:
        try:
            ok = test()
            if ok is False:
                print(f"FAIL {test.__name__}")
                failures += 1
            else:
                print(f"PASS {test.__name__}")
                passed += 1
        except Exception as e:
            import traceback
            print(f"FAIL {test.__name__}: raised {e}")
            traceback.print_exc()
            failures += 1
    print(f"\n{passed} passed, {failures} failed, {len(tests)} total")
    if failures:
        print(f"{failures} test(s) FAILED")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())