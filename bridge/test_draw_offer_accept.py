#!/usr/bin/env python3
"""Tests for draw-offer accept/decline policy (lower-rated rule) and the
"no move after accepting a draw" fix.

Two bugs from live game FPN4DdYY:

1. A LOWER-rated bot in a proven-drawn position declined (flag hunt) instead
   of accepting. As the lower-rated side a draw GAINS ELO, so any draw in a
   drawn position must be accepted regardless of proven / can-flag.

2. After accepting a draw, `_handle_event` still called `_make_move()`, so a
   move was POSTed into a finished game (HTTP 400). `_handle_draw_offer` now
   returns True iff a draw was accepted, and `_handle_event` skips the move.

Style mirrors bridge/test_draw_logic.py: plain functions returning bool,
printed PASS/FAIL, main() runner, sys.path insertion, object.__new__ harness,
exit 0 = all passed / 1 = at least one failed. No pytest. Offline stubs only.

    python bridge/test_draw_offer_accept.py

TDD RED phase: a01, a02, a08 fail before the fix.
"""

import os
import sys
from typing import Any, Dict, List, Optional

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

import chess
from lichess.runner import GameHandler

FEN_KV_K = "8/8/8/8/8/8/4k3/4K3 w - - 0 1"        # insufficient material
FEN_KR_V_KR = "8/8/8/8/8/8/4k2r/4K2R w - - 0 1"   # 4 pieces, no queen


# ---- Offline harness -------------------------------------------------------

class DrawStubClient:
    def __init__(self):
        self.accept_calls: List[str] = []
        self.decline_calls: List[str] = []
        self.make_move_calls: List[Dict[str, str]] = []
        self.last_uci: Optional[str] = None

    def accept_draw(self, game_id: str) -> Dict[str, Any]:
        self.accept_calls.append(game_id)
        return {"ok": True}

    def decline_draw(self, game_id: str) -> Dict[str, Any]:
        self.decline_calls.append(game_id)
        return {"ok": True}

    def make_move(self, game_id: str, uci: str, offering_draw: bool = False) -> Dict[str, Any]:
        self.last_uci = uci
        self.make_move_calls.append({"game_id": game_id, "uci": uci, "offering_draw": offering_draw})
        return {"ok": True}


class MockEngine:
    def __init__(self, bestmove: str = "g1f3", score_cp: int = 0):
        self.bestmove_uci = bestmove
        self.score_cp = score_cp

    def best_move(self, moves_uci, **kwargs):
        return self.bestmove_uci

    def best_moves_multipv(self, moves_uci, movetime=1500, multipv=3):
        return [{"uci": self.bestmove_uci, "score_cp": self.score_cp, "rank": 1}]


def make_handler(color="white", initial_fen="startpos", moves_uci=None,
                 engine=None, client=None, wtime=180000, btime=180000,
                 our_rating=None, opponent_rating=None) -> GameHandler:
    h = object.__new__(GameHandler)
    h.client = client if client is not None else DrawStubClient()
    h.engine = engine if engine is not None else MockEngine()
    h.experience = None
    h.game_id = "TEST"
    h.color = color
    h.initial_fen = initial_fen
    h.moves_uci = list(moves_uci or [])
    h.wtime = wtime
    h.btime = btime
    h.winc = 2000
    h.binc = 2000
    h.variant = "standard"
    h.rated = True
    h.state = None
    h.our_rating = our_rating
    h.opponent_rating = opponent_rating
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


# ===========================================================================
# BUG 1: lower-rated side accepts a draw in a drawn position
# ===========================================================================

def a01_lower_rated_proven_can_flag_accept():
    """Lower-rated, proven K vs K, their clock 10s (<15s). Must ACCEPT (a draw
    gains ELO for the lower-rated side) - never flag-hunt."""
    client = DrawStubClient()
    h = make_handler("white", FEN_KV_K, engine=MockEngine(score_cp=0), client=client,
                     wtime=180000, btime=10000, our_rating=2500, opponent_rating=2700)
    h._handle_draw_offer()
    ok = assert_eq("a01 pending_accept", h._pending_draw_accept, True)
    return ok


def a02_lower_rated_proven_healthy_clock_accept():
    """Lower-rated, proven K vs K, both clocks healthy. Must ACCEPT."""
    client = DrawStubClient()
    h = make_handler("white", FEN_KV_K, engine=MockEngine(score_cp=0), client=client,
                     wtime=180000, btime=180000, our_rating=2500, opponent_rating=2700)
    h._handle_draw_offer()
    ok = assert_eq("a02 pending_accept", h._pending_draw_accept, True)
    return ok


def a03_lower_rated_effective_accept():
    """Lower-rated, drawn KRvKR (not proven), healthy clocks. Must ACCEPT, and
    fast mode must stay off."""
    client = DrawStubClient()
    h = make_handler("white", FEN_KR_V_KR, engine=MockEngine(score_cp=0), client=client,
                     wtime=180000, btime=180000, our_rating=2500, opponent_rating=2700)
    h._handle_draw_offer()
    ok = assert_eq("a03 pending_accept", h._pending_draw_accept, True)
    ok &= assert_eq("a03 fast_mode", h._fast_mode, False)
    return ok


def a04_higher_rated_proven_can_flag_decline():
    """Higher-rated, proven K vs K, their clock 10s. Must DECLINE and enable
    fast mode (flag hunt pays when we are the favourite)."""
    client = DrawStubClient()
    h = make_handler("white", FEN_KV_K, engine=MockEngine(score_cp=0), client=client,
                     wtime=180000, btime=10000, our_rating=2700, opponent_rating=2500)
    h._handle_draw_offer()
    ok = assert_eq("a04 pending_accept", h._pending_draw_accept, False)
    ok &= assert_eq("a04 fast_mode", h._fast_mode, True)
    return ok


def a05_equal_rated_proven_can_flag_decline():
    """Equal-rated, proven K vs K, their clock 10s. Behavior unchanged: DECLINE
    (flag hunt) - the lower-rated rule must not touch equal ratings."""
    client = DrawStubClient()
    h = make_handler("white", FEN_KV_K, engine=MockEngine(score_cp=0), client=client,
                     wtime=180000, btime=10000, our_rating=2500, opponent_rating=2500)
    h._handle_draw_offer()
    return assert_eq("a05 pending_accept", h._pending_draw_accept, False)


def a06_lower_rated_winning_decline():
    """Lower-rated but WINNING (+200). Must DECLINE - never throw away a win."""
    client = DrawStubClient()
    h = make_handler("white", "startpos", engine=MockEngine(score_cp=200), client=client,
                     wtime=180000, btime=180000, our_rating=2500, opponent_rating=2700)
    h._handle_draw_offer()
    return assert_eq("a06 pending_accept", h._pending_draw_accept, False)


def a07_lower_rated_losing_accept():
    """Lower-rated and LOSING (-200). Must ACCEPT - a draw is a great result."""
    client = DrawStubClient()
    h = make_handler("white", "startpos", engine=MockEngine(score_cp=-200), client=client,
                     wtime=180000, btime=180000, our_rating=2500, opponent_rating=2700)
    h._handle_draw_offer()
    return assert_eq("a07 pending_accept", h._pending_draw_accept, True)


# ===========================================================================
# BUG 2: the draw answer is delivered with the next move
# ===========================================================================

def a08_accept_via_move_with_offering_draw():
    """Drive a gameState draw offer (we are White, lower-rated, drawn startpos).
    We accept, and the acceptance is delivered by submitting a move that carries
    offering_draw=True (no separate /draw POST)."""
    client = DrawStubClient()
    h = make_handler("white", "startpos", engine=MockEngine(bestmove="g1f3", score_cp=0),
                     client=client, wtime=180000, btime=180000,
                     our_rating=2500, opponent_rating=2700)
    h._handle_event({"type": "gameState", "status": "started", "bdraw": True})
    ok = assert_eq("a08 make_move_calls", len(client.make_move_calls), 1)
    ok &= assert_eq("a08 offering_draw", client.make_move_calls[-1]["offering_draw"], True)
    return ok


def a09_decline_then_move():
    """Same, but higher-rated so we DECLINE. The game continues, so exactly one
    move must be submitted, and it must NOT offer a draw (offering_draw False)."""
    client = DrawStubClient()
    h = make_handler("white", "startpos", engine=MockEngine(bestmove="g1f3", score_cp=0),
                     client=client, wtime=180000, btime=180000,
                     our_rating=2700, opponent_rating=2500)
    h._handle_event({"type": "gameState", "status": "started", "bdraw": True})
    ok = assert_eq("a09 pending_accept", h._pending_draw_accept, False)
    ok &= assert_eq("a09 make_move_calls", len(client.make_move_calls), 1)
    ok &= assert_eq("a09 offering_draw", client.make_move_calls[-1]["offering_draw"], False)
    return ok


# ---- Runner ----------------------------------------------------------------

def main() -> int:
    tests = [
        a01_lower_rated_proven_can_flag_accept,
        a02_lower_rated_proven_healthy_clock_accept,
        a03_lower_rated_effective_accept,
        a04_higher_rated_proven_can_flag_decline,
        a05_equal_rated_proven_can_flag_decline,
        a06_lower_rated_winning_decline,
        a07_lower_rated_losing_accept,
        a08_accept_via_move_with_offering_draw,
        a09_decline_then_move,
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