#!/usr/bin/env python3
"""Unit tests for the incoming draw-offer decision system in lichess/runner.py.

TDD RED phase: these tests reference NEW GameHandler members that do NOT exist
yet:

    _is_proven_draw(board, tablebase=None) -> Optional[str]
    draw_decision(score, our_ms, their_ms, board, our_rating, opp_rating)
    _effective_movetime(base_ms, our_ms, fast_mode) -> int
    _repeat_threshold(time_ahead_ms, fast_mode) -> int
    _detect_and_apply_draw_state() -> Optional[str]

They are expected to FAIL right now (AttributeError) and to pass once the
production methods are added. That is the point of the RED phase.

Style mirrors bridge/test_opening_counterplay.py: plain functions returning
bool, printed PASS/FAIL, a main() runner, sys.path insertion for the ``bridge``
package, exit code 0 = all passed, 1 = at least one failed. No pytest.

Runs fully offline with a MockEngine / DrawStubClient / FakeTB - no network,
no real engine, no DB. Run from anywhere:

    python bridge/test_draw_logic.py

All expected values below are hand-derived literals (never computed from the
production helpers under test), so a wrong implementation cannot make a test
pass by tautology.
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


# ---- Hand-picked FENs (expected behaviour derived by hand) -----------------

FEN_KV_K = "8/8/8/8/8/8/4k3/4K3 w - - 0 1"          # 2 pieces, insufficient
FEN_KB_V_K = "8/8/8/8/8/8/4k3/4KB2 w - - 0 1"       # 3 pieces, insufficient
FEN_KN_V_KN = "8/8/8/8/8/8/4kn2/4KN2 w - - 0 1"     # 4 pieces, NOT insufficient
FEN_KR_V_KR = "8/8/8/8/8/8/4k2r/4K2R w - - 0 1"     # 4 pieces, no queen, no castling
FEN_6P = "r2qk3/8/8/8/8/8/8/R2QK3 w - - 0 1"        # 6 pieces (2 queens)
FEN_CASTLE = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"  # 6 pieces, castling rights
FEN_STARTPOS = "startpos"


# ---- Offline harness -------------------------------------------------------

class DrawStubClient:
    """Records every accept/decline/make_move call for assertions."""

    def __init__(self):
        self.accept_calls: List[str] = []
        self.decline_calls: List[str] = []
        self.make_move_calls: List[Dict[str, str]] = []

    def accept_draw(self, game_id: str) -> Dict[str, Any]:
        self.accept_calls.append(game_id)
        return {"ok": True}

    def decline_draw(self, game_id: str) -> Dict[str, Any]:
        self.decline_calls.append(game_id)
        return {"ok": True}

    def make_move(self, game_id: str, uci: str, offering_draw: bool = False) -> Dict[str, Any]:
        self.make_move_calls.append({"game_id": game_id, "uci": uci, "offering_draw": offering_draw})
        return {"ok": True}


class MockEngine:
    """Configurable engine stub for the draw-offer matrix.

    score_cp is the side-to-move-relative centipawn score returned by
    best_moves_multipv. bestmove_uci is returned by best_move (used by
    _make_move when a gameState event triggers a move). raise_on_multipv
    simulates an engine failure so the caller must degrade gracefully.
    """

    def __init__(self, score_cp: int = 0, bestmove: str = "g1f3",
                 raise_on_multipv: bool = False):
        self.score_cp = score_cp
        self.bestmove_uci = bestmove
        self.raise_on_multipv = raise_on_multipv

    def best_moves_multipv(self, moves_uci, movetime=1500, multipv=3):
        if self.raise_on_multipv:
            raise RuntimeError("engine multipv failure (simulated)")
        return [{"uci": self.bestmove_uci, "score_cp": self.score_cp, "rank": 1}]

    def best_move(self, moves_uci, **kwargs):
        return self.bestmove_uci


class FakeTB:
    """Syzygy-like tablebase stub. Records whether it was probed."""

    def __init__(self, wdl: Optional[int] = None, exc: Optional[Exception] = None):
        self.wdl = wdl
        self.exc = exc
        self.called = False

    def get_wdl(self, board) -> int:
        self.called = True
        if self.exc is not None:
            raise self.exc
        return self.wdl


class ExpStub:
    """Experience store stub so _handle_game_finish never hits the real DB."""

    def record_game(self, *args, **kwargs):
        return {"skipped": True}

    def record_position(self, *args, **kwargs):
        return None

    def record_move(self, *args, **kwargs):
        return None


def make_handler(
    color: str,
    *,
    initial_fen: str = FEN_STARTPOS,
    moves_uci: Optional[List[str]] = None,
    wtime: Optional[int] = 180000,
    btime: Optional[int] = 180000,
    winc: Optional[int] = 2000,
    binc: Optional[int] = 2000,
    our_rating: Optional[int] = None,
    opponent_rating: Optional[int] = None,
    engine: Optional[MockEngine] = None,
    client: Optional[DrawStubClient] = None,
    capture_logs: bool = True,
) -> GameHandler:
    """Build a GameHandler via object.__new__ (never start the game thread).

    Mirrors the attribute set the draw-offer path reads, per the RED spec.
    When capture_logs is True, _log appends to handler.logs instead of
    printing, so tests can inspect what the decision logged.
    """

    handler = object.__new__(GameHandler)
    handler.client = client if client is not None else DrawStubClient()
    handler.engine = engine if engine is not None else MockEngine()
    handler.experience = None
    handler.game_id = "TEST"
    handler.color = color
    handler.initial_fen = initial_fen if initial_fen else "startpos"
    handler.moves_uci = list(moves_uci) if moves_uci is not None else []
    handler.wtime = wtime
    handler.btime = btime
    handler.winc = winc
    handler.binc = binc
    handler.variant = "standard"
    handler.rated = True
    handler.state = None
    handler.our_rating = our_rating
    handler.opponent_rating = opponent_rating
    handler._draw_offer_seen = False
    handler._fast_mode = False
    handler._fast_mode_source = None
    handler._running = True
    handler._finished = False
    handler._position_counts = {}

    handler.logs: List[str] = []
    if capture_logs:
        handler._log = lambda msg, _logs=handler.logs: _logs.append(msg)
    else:
        handler._log = lambda msg: print(msg, flush=True)

    # Rebuild repetition counts when the helper exists (harmless pre-change).
    if hasattr(handler, "_rebuild_position_counts"):
        try:
            handler._rebuild_position_counts()
        except Exception:
            pass
    return handler


def our_ms_of(handler: GameHandler) -> int:
    return handler.wtime if handler.color == "white" else handler.btime


def their_ms_of(handler: GameHandler) -> int:
    return handler.btime if handler.color == "white" else handler.wtime


def offer_once(handler: GameHandler) -> None:
    """Invoke the incoming draw-offer handler exactly once."""
    handler._handle_draw_offer()


def assert_eq(label: str, got: Any, want: Any) -> bool:
    if got != want:
        print(f"FAIL {label}: expected {want!r}, got {got!r}")
        return False
    return True


# ===========================================================================
# INCOMING OFFER MATRIX (via handler._handle_draw_offer)
# ===========================================================================

def t01_low_clock_winning_accepts():
    """Our clock 15s (<20s) -> never flag -> accept even though winning."""
    engine = MockEngine(score_cp=200)
    client = DrawStubClient()
    h = make_handler("white", wtime=15000, btime=180000, engine=engine, client=client)
    offer_once(h)
    ok = True
    ok &= assert_eq("t01 pending_accept", h._pending_draw_accept, True)
    ok &= assert_eq("t01 fast_mode", h._fast_mode, False)
    return ok


def t02_low_clock_losing_accepts():
    """Low clock overrides losing score -> accept 'low clock'."""
    engine = MockEngine(score_cp=-200)
    client = DrawStubClient()
    h = make_handler("white", wtime=10000, btime=180000, engine=engine, client=client)
    offer_once(h)
    ok = True
    ok &= assert_eq("t02 pending_accept", h._pending_draw_accept, True)
    return ok


def t03_losing_accepts():
    """Comfortable clock, score -200 -> accept (a draw is a great result)."""
    engine = MockEngine(score_cp=-200)
    client = DrawStubClient()
    h = make_handler("white", wtime=180000, btime=180000, engine=engine, client=client)
    offer_once(h)
    ok = True
    ok &= assert_eq("t03 pending_accept", h._pending_draw_accept, True)
    return ok


def t04_winning_declines():
    """Comfortable clock, score +200 -> decline, fast mode stays off."""
    engine = MockEngine(score_cp=200)
    client = DrawStubClient()
    h = make_handler("white", wtime=180000, btime=180000, engine=engine, client=client)
    offer_once(h)
    ok = True
    ok &= assert_eq("t04 pending_accept", h._pending_draw_accept, False)
    ok &= assert_eq("t04 fast_mode", h._fast_mode, False)
    return ok


def t05_drawn_proven_can_flag():
    """Drawn + proven K vs K + their clock 10s -> decline, fast mode ON (we
    have >=60s so we can flag them)."""
    engine = MockEngine(score_cp=0)
    client = DrawStubClient()
    h = make_handler("white", initial_fen=FEN_KV_K, wtime=180000, btime=10000,
                     our_rating=2500, opponent_rating=2500, engine=engine, client=client)
    offer_once(h)
    ok = True
    ok &= assert_eq("t05 pending_accept", h._pending_draw_accept, False)
    ok &= assert_eq("t05 fast_mode", h._fast_mode, True)
    return ok


def t06_drawn_higher_rated_effective():
    """Drawn + we are higher-rated + effective endgame (K+R vs K+R, no queen,
    <=10 pieces) + healthy clock -> decline and enable fast mode."""
    engine = MockEngine(score_cp=0)
    client = DrawStubClient()
    h = make_handler("white", initial_fen=FEN_KR_V_KR, wtime=180000, btime=180000,
                     our_rating=2700, opponent_rating=2500, engine=engine, client=client)
    offer_once(h)
    ok = True
    ok &= assert_eq("t06 pending_accept", h._pending_draw_accept, False)
    ok &= assert_eq("t06 fast_mode", h._fast_mode, True)
    return ok


def t07_drawn_lower_rated_accepts():
    """Drawn + we are lower-rated + effective endgame -> accept, fast off."""
    engine = MockEngine(score_cp=0)
    client = DrawStubClient()
    h = make_handler("white", initial_fen=FEN_KR_V_KR, wtime=180000, btime=180000,
                     our_rating=2500, opponent_rating=2700, engine=engine, client=client)
    offer_once(h)
    ok = True
    ok &= assert_eq("t07 pending_accept", h._pending_draw_accept, True)
    ok &= assert_eq("t07 fast_mode", h._fast_mode, False)
    return ok


def t08_drawn_equal_rating_accepts():
    """Drawn + equal rating -> accept, fast off."""
    engine = MockEngine(score_cp=0)
    client = DrawStubClient()
    h = make_handler("white", initial_fen=FEN_KR_V_KR, wtime=180000, btime=180000,
                     our_rating=2600, opponent_rating=2600, engine=engine, client=client)
    offer_once(h)
    ok = True
    ok &= assert_eq("t08 pending_accept", h._pending_draw_accept, True)
    ok &= assert_eq("t08 fast_mode", h._fast_mode, False)
    return ok


def t09_drawn_unknown_rating_accepts():
    """Drawn + unknown rating (None) -> accept, fast off."""
    engine = MockEngine(score_cp=0)
    client = DrawStubClient()
    h = make_handler("white", initial_fen=FEN_KR_V_KR, wtime=180000, btime=180000,
                     our_rating=None, opponent_rating=None, engine=engine, client=client)
    offer_once(h)
    ok = True
    ok &= assert_eq("t09 pending_accept", h._pending_draw_accept, True)
    ok &= assert_eq("t09 fast_mode", h._fast_mode, False)
    return ok


def t10_sign_flip_black_offer_on_opp_turn():
    """Black bot. The offer arrives on the OPPONENT's turn (even ply count ->
    it is White to move). The engine score is side-to-move relative, so +80
    means the opponent is +80 -> after negation WE are -80 -> accept 'losing'.

    NOTE: the spec's parenthetical said 'odd plies with moves_uci=[e2e4]',
    but for a Black bot an ODD ply count means it is OUR turn (no negation
    would fire). To actually exercise the negation branch we use an EVEN ply
    count (two plies: 1.e4 e5) so it is the opponent's turn. This is the only
    internally-consistent way to get the stated result (-80 -> accept).
    """
    engine = MockEngine(score_cp=80)
    client = DrawStubClient()
    h = make_handler("black", initial_fen=FEN_KV_K,
                     moves_uci=["e2e4", "e7e5"],  # 2 plies -> White (opponent) to move
                     wtime=180000, btime=180000, engine=engine, client=client)
    offer_once(h)
    ok = True
    ok &= assert_eq("t10 pending_accept", h._pending_draw_accept, True)
    return ok


def t11_engine_failure_still_decides():
    """Engine multipv raises -> treat score as 0, still make a decision
    (drawn + unknown rating -> accept)."""
    engine = MockEngine(score_cp=999, raise_on_multipv=True)
    client = DrawStubClient()
    h = make_handler("white", initial_fen=FEN_KR_V_KR, wtime=180000, btime=180000,
                     our_rating=None, opponent_rating=None, engine=engine, client=client)
    offer_once(h)
    ok = True
    ok &= assert_eq("t11 pending_accept", h._pending_draw_accept, True)
    return ok


def t12_missing_rating_attrs_no_crash():
    """Delete the rating attributes entirely; the handler must not raise
    AttributeError and must still make a decision (drawn -> accept)."""
    engine = MockEngine(score_cp=0)
    client = DrawStubClient()
    h = make_handler("white", initial_fen=FEN_KR_V_KR, wtime=180000, btime=180000,
                     our_rating=2700, opponent_rating=2500, engine=engine, client=client)
    del h.our_rating
    del h.opponent_rating
    offer_once(h)
    ok = True
    # Unknown rating -> not higher-rated -> accept.
    ok &= assert_eq("t12 pending_accept", h._pending_draw_accept, True)
    return ok


def t13_dedupe_via_handle_event():
    """Two consecutive gameState events with bdraw=true (we are White) must
    only trigger ONE accept (the _draw_offer_seen dedupe). _make_move is
    stubbed through the client, so this is safe."""
    engine = MockEngine(score_cp=0, bestmove="g1f3")
    client = DrawStubClient()
    h = make_handler("white", initial_fen=FEN_STARTPOS, wtime=180000, btime=180000,
                     our_rating=2600, opponent_rating=2600, engine=engine, client=client)
    event = {"type": "gameState", "status": "started", "bdraw": True, "wdraw": False}
    h._handle_event(dict(event))
    h._handle_event(dict(event))
    # The dedupe means only ONE answer is delivered: exactly one submitted move
    # carries offering_draw=True.
    offering = [m for m in client.make_move_calls if m.get("offering_draw")]
    return assert_eq("t13 offering_draw moves (dedupe)", len(offering), 1)


def t14_withdraw_then_reoffer():
    """bdraw true -> false (withdrawn) -> true (re-offered) must produce two
    decisions (two accepts)."""
    engine = MockEngine(score_cp=0, bestmove="g1f3")
    client = DrawStubClient()
    h = make_handler("white", initial_fen=FEN_STARTPOS, wtime=180000, btime=180000,
                     our_rating=2600, opponent_rating=2600, engine=engine, client=client)
    h._handle_event({"type": "gameState", "status": "started", "bdraw": True, "wdraw": False})
    h._handle_event({"type": "gameState", "status": "started", "bdraw": False, "wdraw": False})
    h._handle_event({"type": "gameState", "status": "started", "bdraw": True, "wdraw": False})
    # Two accept decisions -> two "Accepted draw offer" log lines.
    accepts = [l for l in h.logs if "Accepted draw offer" in l]
    return assert_eq("t14 accept decisions", len(accepts), 2)


# ===========================================================================
# PURE HELPERS
# ===========================================================================

def t15_effective_movetime_fast():
    """fast_mode -> 100ms regardless of base."""
    h = make_handler("white")
    return assert_eq("t15", h._effective_movetime(1500, 180000, True), 100)


def t16_effective_movetime_low_clock():
    """our_ms < 15000 -> 100ms."""
    h = make_handler("white")
    return assert_eq("t16", h._effective_movetime(1500, 10000, False), 100)


def t17_effective_movetime_normal():
    """Healthy clock, fast off -> base."""
    h = make_handler("white")
    return assert_eq("t17", h._effective_movetime(5000, 180000, False), 5000)


def t18_repeat_threshold_fast():
    """fast_mode -> threshold 1."""
    h = make_handler("white")
    return assert_eq("t18", h._repeat_threshold(0, True), 1)


def t19_repeat_threshold_time_ahead():
    """time_ahead > 20000 -> threshold 1."""
    h = make_handler("white")
    return assert_eq("t19", h._repeat_threshold(25000, False), 1)


def t20_repeat_threshold_normal():
    """Not fast, not far ahead -> threshold 2."""
    h = make_handler("white")
    return assert_eq("t20", h._repeat_threshold(10000, False), 2)


# ===========================================================================
# PROVEN DRAW DETECTION
# ===========================================================================

def t21_kv_k_insufficient():
    """K vs K -> 'insufficient material'."""
    h = make_handler("white")
    r = h._is_proven_draw(chess.Board(FEN_KV_K))
    return assert_eq("t21", r, "insufficient material")


def t22_kb_v_k_insufficient():
    """K+B vs K -> 'insufficient material'."""
    h = make_handler("white")
    r = h._is_proven_draw(chess.Board(FEN_KB_V_K))
    return assert_eq("t22", r, "insufficient material")


def t23_kn_v_kn_not_insufficient():
    """K+N vs K+N is NOT insufficient material in python-chess. With a
    tablebase that returns None (unknown), the result must be None."""
    h = make_handler("white")
    tb = FakeTB(wdl=None)
    r = h._is_proven_draw(chess.Board(FEN_KN_V_KN), tablebase=tb)
    return assert_eq("t23", r, None)


def t24_kr_v_kr_tablebase_zero():
    """K+R vs K+R + tablebase WDL 0 -> 'tablebase WDL=0'."""
    h = make_handler("white")
    tb = FakeTB(wdl=0)
    r = h._is_proven_draw(chess.Board(FEN_KR_V_KR), tablebase=tb)
    ok = assert_eq("t24", r, "tablebase WDL=0")
    ok &= assert_eq("t24 tb.called", tb.called, True)
    return ok


def t25_tablebase_nonzero_none():
    """Tablebase WDL 2 -> not proven -> None."""
    h = make_handler("white")
    tb = FakeTB(wdl=2)
    r = h._is_proven_draw(chess.Board(FEN_KR_V_KR), tablebase=tb)
    return assert_eq("t25", r, None)


def t26_tablebase_keyerror_graceful():
    """Tablebase raises KeyError -> treated as None, no crash."""
    h = make_handler("white")
    tb = FakeTB(exc=KeyError("no such position"))
    r = h._is_proven_draw(chess.Board(FEN_KR_V_KR), tablebase=tb)
    return assert_eq("t26", r, None)


def t27_startpos_not_proven():
    """startpos (>5 pieces) -> no tablebase probe, None."""
    h = make_handler("white")
    tb = FakeTB(wdl=0)
    r = h._is_proven_draw(chess.Board(), tablebase=tb)
    ok = assert_eq("t27 result", r, None)
    ok &= assert_eq("t27 tb.called", tb.called, False)
    return ok


def t28_six_piece_no_probe():
    """6 pieces -> tablebase must NOT be probed (even though it would say 0)."""
    h = make_handler("white")
    tb = FakeTB(wdl=0)
    r = h._is_proven_draw(chess.Board(FEN_6P), tablebase=tb)
    ok = assert_eq("t28 result", r, None)
    ok &= assert_eq("t28 tb.called", tb.called, False)
    return ok


def t29_castling_no_probe():
    """Castling rights present -> tablebase must NOT be probed."""
    h = make_handler("white")
    tb = FakeTB(wdl=0)
    r = h._is_proven_draw(chess.Board(FEN_CASTLE), tablebase=tb)
    ok = assert_eq("t29 result", r, None)
    ok &= assert_eq("t29 tb.called", tb.called, False)
    return ok


def t30_no_tablebase_no_crash():
    """tablebase=None with a 4-piece non-insufficient board -> None."""
    h = make_handler("white")
    r = h._is_proven_draw(chess.Board(FEN_KR_V_KR))
    return assert_eq("t30", r, None)


# ===========================================================================
# AUTONOMOUS STATE
# ===========================================================================

def t31_detect_proven_sets_fast_mode():
    """K vs K board -> proven -> enable fast mode with source 'proven'."""
    h = make_handler("white", initial_fen=FEN_KV_K)
    r = h._detect_and_apply_draw_state()
    ok = assert_eq("t31 return", r, "insufficient material")
    ok &= assert_eq("t31 fast_mode", h._fast_mode, True)
    ok &= assert_eq("t31 source", h._fast_mode_source, "proven")
    return ok


def t32_detect_not_proven_clears_fast_mode():
    """Startpos after a prior 'proven' fast-mode -> fast mode cleared."""
    h = make_handler("white", initial_fen=FEN_STARTPOS)
    h._fast_mode = True
    h._fast_mode_source = "proven"
    r = h._detect_and_apply_draw_state()
    ok = assert_eq("t32 return", r, None)
    ok &= assert_eq("t32 fast_mode", h._fast_mode, False)
    return ok


# ===========================================================================
# GAME FINISH
# ===========================================================================

def t33_game_finish_clears_fast_mode():
    """Finishing a game must clear fast mode. Uses a minimal draw event with no
    moves so the experience/training paths are skipped; asserts only the
    fast-mode reset (as the spec allows for a tightly-coupled method)."""
    h = make_handler("white", initial_fen=FEN_STARTPOS, moves_uci=[])
    h.experience = ExpStub()  # spec allows a stub for this tightly-coupled method
    h._fast_mode = True
    h._fast_mode_source = "proven"
    h._finished = False
    h._handle_game_finish({"type": "gameFinish", "status": "draw", "winner": None})
    return assert_eq("t33 fast_mode", h._fast_mode, False)


# ===========================================================================
# SAFETY GAPS (fifty-move + decisive material)
# ===========================================================================

def t34_fifty_moves_does_not_activate_fast():
    """A fifty-move position is a proven draw by the 50-move rule, but must
    NOT trigger autonomous fast mode (blitzing could throw a win, and flagging
    is pointless once a 50-move draw is available)."""
    h = make_handler("white", initial_fen="r6k/8/8/8/8/8/8/R6K w - - 100 1")
    h._detect_and_apply_draw_state()
    return assert_eq("t34 fast_mode", h._fast_mode, False)


def t35_decisive_material_clears_offer_fast():
    """Offer-driven fast mode in a position with an extra queen (decisive
    material) must be cleared so the bot does not blitz a won game."""
    h = make_handler("white", initial_fen="8/8/8/8/8/8/4k3/4KQ2 w - - 0 1")
    h._fast_mode = True
    h._fast_mode_source = "offer"
    h._detect_and_apply_draw_state()
    return assert_eq("t35 fast_mode", h._fast_mode, False)


def t36_equal_material_keeps_offer_fast():
    """Equal material (K+R vs K+R, 5v5, no queen) must NOT be cleared -
    guards against over-clearing the offer fast mode."""
    h = make_handler("white", initial_fen=FEN_KR_V_KR)
    h._fast_mode = True
    h._fast_mode_source = "offer"
    h._detect_and_apply_draw_state()
    return assert_eq("t36 fast_mode", h._fast_mode, True)


# ---- Runner ----------------------------------------------------------------

def main() -> int:
    tests = [
        # Incoming offer matrix
        t01_low_clock_winning_accepts,
        t02_low_clock_losing_accepts,
        t03_losing_accepts,
        t04_winning_declines,
        t05_drawn_proven_can_flag,
        t06_drawn_higher_rated_effective,
        t07_drawn_lower_rated_accepts,
        t08_drawn_equal_rating_accepts,
        t09_drawn_unknown_rating_accepts,
        t10_sign_flip_black_offer_on_opp_turn,
        t11_engine_failure_still_decides,
        t12_missing_rating_attrs_no_crash,
        t13_dedupe_via_handle_event,
        t14_withdraw_then_reoffer,
        # Pure helpers
        t15_effective_movetime_fast,
        t16_effective_movetime_low_clock,
        t17_effective_movetime_normal,
        t18_repeat_threshold_fast,
        t19_repeat_threshold_time_ahead,
        t20_repeat_threshold_normal,
        # Proven draw
        t21_kv_k_insufficient,
        t22_kb_v_k_insufficient,
        t23_kn_v_kn_not_insufficient,
        t24_kr_v_kr_tablebase_zero,
        t25_tablebase_nonzero_none,
        t26_tablebase_keyerror_graceful,
        t27_startpos_not_proven,
        t28_six_piece_no_probe,
        t29_castling_no_probe,
        t30_no_tablebase_no_crash,
        # Autonomous state
        t31_detect_proven_sets_fast_mode,
        t32_detect_not_proven_clears_fast_mode,
        # Game finish
        t33_game_finish_clears_fast_mode,
        # Safety gaps
        t34_fifty_moves_does_not_activate_fast,
        t35_decisive_material_clears_offer_fast,
        t36_equal_material_keeps_offer_fast,
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