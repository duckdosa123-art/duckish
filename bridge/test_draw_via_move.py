#!/usr/bin/env python3
"""Tests for answering draw offers via the MOVE endpoint (offeringDraw=true)
instead of the separate /draw/{yes,no} POST (which 404s).

Lichess's official bot answers draw offers on the move POST:
POST /api/bot/game/{id}/move/{uci}?offeringDraw=true  (accept)
A plain move (no param) auto-declines a pending offer.

Covers:
  b01 api.make_move builds the offeringDraw query param correctly.
  b02 accepting sets _pending_draw_accept and never calls accept_draw.
  b03 declining clears _pending_draw_accept.
  b04 _make_move passes offering_draw=True when pending and resets the flag.
  b05 the flag never leaks into a later move.
  b06 a withdrawn offer clears a pending accept.
  b07 full accept flow submits a move with offering_draw=True.
  b08 the old accept_draw/decline_draw endpoints are never touched.

Style mirrors bridge/test_draw_logic.py. Offline stubs only.

    python bridge/test_draw_via_move.py

TDD RED phase: b01, b02, b04, b05, b06, b07 fail before the fix.
"""

import os
import sys
from typing import Any, Dict, List, Optional

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

import chess
from lichess.runner import GameHandler
from lichess.api import LichessClient


# ---- Offline harness -------------------------------------------------------

class DrawStubClient:
    """Records moves (with offering_draw) and any accept/decline calls."""

    def __init__(self):
        self.accept_calls: List[str] = []
        self.decline_calls: List[str] = []
        self.make_move_calls: List[Dict[str, Any]] = []
        self.last_uci: Optional[str] = None

    def accept_draw(self, game_id: str) -> Dict[str, Any]:
        self.accept_calls.append(game_id)
        return {"ok": True}

    def decline_draw(self, game_id: str) -> Dict[str, Any]:
        self.decline_calls.append(game_id)
        return {"ok": True}

    def make_move(self, game_id: str, uci: str, offering_draw: bool = False) -> Dict[str, Any]:
        self.last_uci = uci
        self.make_move_calls.append(
            {"game_id": game_id, "uci": uci, "offering_draw": offering_draw}
        )
        return {"ok": True}


class NoDrawClient(DrawStubClient):
    """Raises if the legacy draw endpoints are touched (they must not be)."""

    def accept_draw(self, game_id: str) -> Dict[str, Any]:
        raise AssertionError("accept_draw() must not be called - use offeringDraw on the move")

    def decline_draw(self, game_id: str) -> Dict[str, Any]:
        raise AssertionError("decline_draw() must not be called - a plain move auto-declines")


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
    h._pending_draw_accept = False
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


# ---- b01: api.make_move query param ---------------------------------------

class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class CaptureClient(LichessClient):
    """LichessClient with _request stubbed to capture the endpoint."""

    def __init__(self):
        self.endpoints: List[Any] = []

    def _request(self, method, endpoint, data=None, stream=False, timeout=None):
        self.endpoints.append((method, endpoint))
        return _FakeResp(b"{}")


def b01_api_offering_draw_param():
    c = CaptureClient()
    c.make_move("g1", "e2e4", offering_draw=True)
    c.make_move("g1", "e2e4", offering_draw=False)
    ok = assert_eq("b01 accept endpoint", c.endpoints[0],
                   ("POST", "/bot/game/g1/move/e2e4?offeringDraw=true"))
    ok &= assert_eq("b01 plain endpoint", c.endpoints[1],
                    ("POST", "/bot/game/g1/move/e2e4"))
    return ok


# ---- b02/b03: decision sets/clears the pending flag -----------------------

def b02_accept_sets_pending():
    """Lower-rated drawn position -> accept -> _pending_draw_accept True, and
    accept_draw() must NOT be called."""
    client = DrawStubClient()
    h = make_handler("white", "startpos", engine=MockEngine(score_cp=0), client=client,
                     our_rating=2500, opponent_rating=2700)
    h._handle_draw_offer()
    ok = assert_eq("b02 pending", h._pending_draw_accept, True)
    ok &= assert_eq("b02 accept_calls", len(client.accept_calls), 0)
    return ok


def b03_decline_clears_pending():
    """Higher-rated drawn position -> decline -> _pending_draw_accept False."""
    client = DrawStubClient()
    h = make_handler("white", "startpos", engine=MockEngine(score_cp=0), client=client,
                     our_rating=2700, opponent_rating=2500)
    h._pending_draw_accept = True  # pretend a prior accept was pending
    h._handle_draw_offer()
    return assert_eq("b03 pending", h._pending_draw_accept, False)


# ---- b04/b05: _make_move embeds offering_draw and resets ------------------

def b04_make_move_passes_offering_draw():
    """Pending accept -> _make_move submits offering_draw=True and resets the
    flag to False."""
    client = DrawStubClient()
    h = make_handler("white", "startpos", engine=MockEngine(bestmove="g1f3"), client=client)
    h._pending_draw_accept = True
    h._make_move()
    ok = assert_eq("b04 moves", len(client.make_move_calls), 1)
    ok &= assert_eq("b04 offering_draw", client.make_move_calls[-1]["offering_draw"], True)
    ok &= assert_eq("b04 pending reset", h._pending_draw_accept, False)
    return ok


def b05_no_leak():
    """After a move that offered a draw, the flag is reset so the NEXT move does
    not offer again."""
    client = DrawStubClient()
    h = make_handler("white", "startpos", engine=MockEngine(bestmove="g1f3"), client=client)
    h._pending_draw_accept = True
    h._make_move()  # move 1: offering_draw True, flag reset
    first = client.make_move_calls[-1]["offering_draw"]
    # Fresh board so a second legal move is possible; do NOT touch the flag.
    h.moves_uci = []
    h._position_counts = {}
    h._make_move()  # move 2: must be offering_draw False
    second = client.make_move_calls[-1]["offering_draw"]
    ok = assert_eq("b05 first offering_draw", first, True)
    ok &= assert_eq("b05 second offering_draw", second, False)
    return ok


# ---- b06: withdrawn offer clears pending ----------------------------------

def b06_withdrawn_offer_clears_pending():
    """A gameState with NO offer flag clears a pending accept (so a later move
    cannot wrongly offer a draw)."""
    client = DrawStubClient()
    h = make_handler("white", "startpos", engine=MockEngine(bestmove="g1f3"), client=client)
    h._pending_draw_accept = True
    h._handle_event({"type": "gameState", "status": "started"})
    return assert_eq("b06 pending", h._pending_draw_accept, False)


# ---- b07: full accept flow ------------------------------------------------

def b07_full_accept_flow():
    """White, lower-rated, gameState with bdraw=True -> we accept and the
    submitted move carries offering_draw=True."""
    client = DrawStubClient()
    h = make_handler("white", "startpos", engine=MockEngine(bestmove="g1f3"), client=client,
                     our_rating=2500, opponent_rating=2700)
    h._handle_event({"type": "gameState", "status": "started", "bdraw": True})
    ok = assert_eq("b07 moves", len(client.make_move_calls), 1)
    ok &= assert_eq("b07 offering_draw", client.make_move_calls[-1]["offering_draw"], True)
    return ok


# ---- b08: legacy endpoints never touched ----------------------------------

def b08_no_404_path():
    """With a client whose accept_draw/decline_draw raise, both an accept and a
    decline decision must complete without touching those endpoints."""
    client = NoDrawClient()
    h = make_handler("white", "startpos", engine=MockEngine(score_cp=0), client=client,
                     our_rating=2500, opponent_rating=2700)
    h._handle_draw_offer()  # accept -> pending True
    ok = assert_eq("b08 accept pending", h._pending_draw_accept, True)
    h2 = make_handler("white", "startpos", engine=MockEngine(score_cp=0), client=client,
                      our_rating=2700, opponent_rating=2500)
    h2._handle_draw_offer()  # decline -> pending False
    ok &= assert_eq("b08 decline pending", h2._pending_draw_accept, False)
    return ok


# ---- Runner ----------------------------------------------------------------

def main() -> int:
    tests = [
        b01_api_offering_draw_param,
        b02_accept_sets_pending,
        b03_decline_clears_pending,
        b04_make_move_passes_offering_draw,
        b05_no_leak,
        b06_withdrawn_offer_clears_pending,
        b07_full_accept_flow,
        b08_no_404_path,
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