#!/usr/bin/env python3
"""Unit tests for the opening counterplay tiebreak in lichess/runner.py `_make_move`.

Proves:
1. Black counterplay preferences ACTUALLY fire (Sicilian / Grunfeld moves chosen
   when they are within 25cp of the engine's top candidate).
2. The safety valve works - a preferred move that is >25cp worse than the
   engine's top is NOT forced; the engine's bestmove is kept.
3. Counterplay does NOT fire when it shouldn't (ply >= 8, or an unusual White
   first move not in the preference table).
4. White's Reti preferences still fire (unchanged).

Runs fully offline with a MockEngine / StubClient - no network, no engine,
no DB. Run from anywhere:

    python bridge/test_opening_counterplay.py

Exit code 0 = all tests passed, 1 = at least one failed.
"""

import os
import sys
from typing import Any, Dict, List, Optional

# Make the bridge package importable regardless of the caller's cwd.
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

from lichess.runner import GameHandler


# ---- Offline harness -------------------------------------------------------

class MockEngine:
    """Returns a fixed bestmove and a fixed MultiPV candidate list.

    `candidates` is a list of dicts like {"uci", "score_cp", "rank"} - the
    shape produced by Engine.best_moves_multipv(). Order matters: the runner
    treats cands[0] as the engine's top line (top_score).
    """

    def __init__(self, bestmove: str, multipv_candidates: List[Dict[str, Any]]):
        self.bestmove_uci = bestmove
        self.candidates = multipv_candidates

    def best_move(self, moves_uci, **kwargs):
        return self.bestmove_uci

    def best_moves_multipv(self, moves_uci, movetime=1500, multipv=3):
        return self.candidates


class StubClient:
    """Records the uci passed to make_move and returns {'ok': True}."""

    def __init__(self):
        self.last_uci: Optional[str] = None
        self.calls: List[Dict[str, str]] = []

    def make_move(self, game_id: str, uci: str) -> Dict[str, Any]:
        self.last_uci = uci
        self.calls.append({"game_id": game_id, "uci": uci})
        return {"ok": True}


def make_handler(
    color: str,
    moves_uci: List[str],
    engine: MockEngine,
    client: StubClient,
) -> GameHandler:
    """Build a GameHandler without __init__ (which would start a game thread).

    Mirrors the attribute set that _make_move and the repetition guard read.
    """

    handler = object.__new__(GameHandler)
    handler.client = client
    handler.engine = engine
    handler.game_id = "TEST"
    handler.color = color
    handler.initial_fen = "startpos"
    handler.moves_uci = list(moves_uci)
    handler.wtime = 180000
    handler.btime = 180000
    handler.winc = 2000
    handler.binc = 2000
    handler.variant = "standard"
    handler.rated = True
    handler.state = None
    handler._running = True
    handler._finished = False
    handler._position_counts = {}
    # _log prints to stdout when state is None (default behaviour).
    handler._log = lambda msg: print(msg, flush=True)
    # Repetition guard reads _position_counts; rebuild it from moves_uci.
    if hasattr(handler, "_rebuild_position_counts"):
        handler._rebuild_position_counts()
    return handler


def play(
    color: str,
    moves_uci: List[str],
    engine: MockEngine,
    client: StubClient,
) -> Optional[str]:
    """Run one _make_move() and return the move the bot submitted."""
    handler = make_handler(color, moves_uci, engine, client)
    handler._make_move()
    return client.last_uci


# ---- Tests -----------------------------------------------------------------

def test_sicilian_fires_vs_e4():
    """Black, 1.e4 - engine's top is e7e5, but c7c5 (Sicilian) is only 5cp
    worse, so the counterplay preference should fire and pick the Sicilian.

    Only ONE preferred candidate qualifies (c7c5); e7e5 is also in the
    preference set but it is the engine's own bestmove, so even if it were
    chosen the result is still acceptable. We assert the result is in the
    acceptable set {c7c5, e7e5} and that it is NOT some unrelated move.
    """
    engine = MockEngine(
        bestmove="e7e5",
        multipv_candidates=[
            {"uci": "e7e5", "score_cp": 15, "rank": 1},
            {"uci": "c7c5", "score_cp": 10, "rank": 2},
            {"uci": "g8f6", "score_cp": 0, "rank": 3},
        ],
    )
    client = StubClient()
    move = play("black", ["e2e4"], engine, client)
    # c7c5 qualifies (10 >= 15-25). e7e5 is engine's best and also acceptable.
    if move not in ("c7c5", "e7e5"):
        print(f"FAIL test_sicilian_fires_vs_e4: expected c7c5 (or e7e5), got {move}")
        return False
    # The Sicilian must be among the candidates and should win when the engine
    # itself does not prefer e7e5 outright. Here c7c5 qualifies and e7e5 is
    # also eligible; with two eligible moves random.choice picks either, so we
    # only assert the result is one of the two acceptable moves (done above).
    # To prove the preference path is exercised at all, rerun with ONLY c7c5
    # preferred-qualifying by dropping e7e5 from the eligible window is not
    # possible here (e7e5 is engine best), so we rely on the deterministic
    # variant below.
    print(f"PASS test_sicilian_fires_vs_e4: bot played {move}")
    return True


def test_sicilian_deterministic_when_only_c5_qualifies():
    """Same position, but the engine's bestmove is e7e5 (score 15) and the
    only OTHER preferred candidate c7c5 is 5cp worse. Both e7e5 and c7c5 are
    in opening_pref {c7c5, e7e5}; e7e5 qualifies trivially (it is rank 1).

    To make the test deterministic AND prove the preference path fires, we
    make the engine's bestmove NOT a preferred move at all. Use bestmove g8f6
    (not in pref) with c7c5 within 25cp: then the only eligible candidate is
    c7c5 and random.choice must pick it.
    """
    engine = MockEngine(
        bestmove="g8f6",  # engine likes Nf6, not in the {c7c5, e7e5} pref set
        multipv_candidates=[
            {"uci": "g8f6", "score_cp": 15, "rank": 1},  # top_score = 15
            {"uci": "c7c5", "score_cp": 10, "rank": 2},  # 10 >= 15-25 -> eligible
        ],
    )
    client = StubClient()
    move = play("black", ["e2e4"], engine, client)
    if move != "c7c5":
        print(f"FAIL test_sicilian_deterministic_when_only_c5_qualifies: expected c7c5, got {move}")
        return False
    print(f"PASS test_sicilian_deterministic_when_only_c5_qualifies: bot played {move}")
    return True


def test_grunfeld_d5_fires_vs_d4():
    """Black, ply 5: 1.d4 Nf6 2.c4 g6 3.Nc3 - the engine's top is f8g7 (10cp)
    but d7d5 (Grunfeld central strike, 5cp) is within the 25cp window.

    f8g7 is also in the ply-5 pref set {d7d5, c7c5, f8g7}, and d7d5 qualifies
    (5 >= 10-25). So both d7d5 and f8g7 are eligible -> random.choice picks
    one of them. We assert the result is in {d7d5, f8g7} and that the
    preference path was actually exercised (the log line fires).
    """
    engine = MockEngine(
        bestmove="f8g7",
        multipv_candidates=[
            {"uci": "f8g7", "score_cp": 10, "rank": 1},
            {"uci": "d7d5", "score_cp": 5, "rank": 2},
            {"uci": "c7c5", "score_cp": -20, "rank": 3},  # outside window
        ],
    )
    client = StubClient()
    # capture log lines to confirm the preference branch fired
    import io
    import contextlib
    buf = io.StringIO()
    handler = make_handler(
        "black",
        ["d2d4", "g8f6", "c2c4", "g7g6", "b1c3"],
        engine,
        client,
    )
    with contextlib.redirect_stdout(buf):
        handler._make_move()
    move = client.last_uci
    logs = buf.getvalue()
    if move not in ("d7d5", "f8g7"):
        print(f"FAIL test_grunfeld_d5_fires_vs_d4: expected d7d5 or f8g7, got {move}")
        return False
    if "Opening preference:" not in logs:
        print(f"FAIL test_grunfeld_d5_fires_vs_d4: preference branch did not fire. logs={logs!r}")
        return False
    print(f"PASS test_grunfeld_d5_fires_vs_d4: bot played {move} (preference fired)")
    return True


def test_grunfeld_deterministic_when_only_d5_qualifies():
    """Deterministic proof the Grunfeld ...d5 fires: engine bestmove is some
    non-preferred move (g8f6, not in the ply-5 pref set) at 10cp, and d7d5 is
    5cp worse -> only d7d5 is eligible -> must play d7d5.
    """
    engine = MockEngine(
        bestmove="g8f6",  # not in {d7d5, c7c5, f8g7}
        multipv_candidates=[
            {"uci": "g8f6", "score_cp": 10, "rank": 1},
            {"uci": "d7d5", "score_cp": 5, "rank": 2},  # eligible
        ],
    )
    client = StubClient()
    move = play("black", ["d2d4", "g8f6", "c2c4", "g7g6", "b1c3"], engine, client)
    if move != "d7d5":
        print(f"FAIL test_grunfeld_deterministic_when_only_d5_qualifies: expected d7d5, got {move}")
        return False
    print(f"PASS test_grunfeld_deterministic_when_only_d5_qualifies: bot played {move}")
    return True


def test_safety_bad_counterplay_not_forced():
    """Safety valve: 1.e4, engine bestmove e7e5 (score 30). The Sicilian c7c5
    is in the preference set but 40cp worse (-10) -> outside the 25cp window.
    The bot must keep the engine's move e7e5 and NOT force c7c5.
    """
    engine = MockEngine(
        bestmove="e7e5",
        multipv_candidates=[
            {"uci": "e7e5", "score_cp": 30, "rank": 1},
            {"uci": "c7c5", "score_cp": -10, "rank": 3},  # 40cp worse -> NOT eligible
        ],
    )
    client = StubClient()
    move = play("black", ["e2e4"], engine, client)
    if move != "e7e5":
        print(f"FAIL test_safety_bad_counterplay_not_forced: expected e7e5 (engine kept), got {move}")
        return False
    print(f"PASS test_safety_bad_counterplay_not_forced: bot kept engine move {move}")
    return True


def test_no_counterplay_after_ply8():
    """At ply >= 8 the tiebreak is skipped entirely. Black to move on ply 10
    (moves_uci length 10). Even though c7c5 is a candidate within 25cp, the
    bot must keep the engine's bestmove.
    """
    # 9 half-moves (ply 9, odd -> Black to move) of a Ruy Lopez:
    # 1.e4 e5 2.Nf3 Nc6 3.Bb5 a6 4.Ba4 Nf6 5.O-O  -- Black to move, ply 9 >= 8.
    moves = ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a4", "g8f6", "e1g1"]
    assert len(moves) == 9
    # ...d6 is a legal black pawn push here. c7c5 is in the pref table but the
    # tiebreak is skipped at ply >= 8, so it must never be chosen.
    engine = MockEngine(
        bestmove="d7d6",  # legal black pawn push at this Ruy Lopez position
        multipv_candidates=[
            {"uci": "d7d6", "score_cp": 15, "rank": 1},
            {"uci": "c7c5", "score_cp": 10, "rank": 2},  # in pref but ply >= 8
        ],
    )
    client = StubClient()
    move = play("black", moves, engine, client)
    if move != "d7d6":
        print(f"FAIL test_no_counterplay_after_ply8: expected d7d6 (tiebreak skipped), got {move}")
        return False
    print(f"PASS test_no_counterplay_after_ply8: bot played {move} (no tiebreak past ply 8)")
    return True


def test_unusual_white_opening_falls_back():
    """1.Nc3 (Nimzo-Larsen-ish / Dunst) is NOT in the ply-1 table. Black's
    ply-1 branch has no 'b1c3' case, so it falls into the else branch
    opening_pref = {e7e5, d7d5}. The engine bestmove is d7d5 (score 10) with
    other candidates around it. d7d5 IS in the else pref set and is the engine
    best, so the result is d7d5 - no weird forced preference, engine's move.

    This also confirms the fallback else-branch does not force an illegal or
    bizarre move: it only picks from {e7e5, d7d5} within 25cp.
    """
    engine = MockEngine(
        bestmove="d7d5",
        multipv_candidates=[
            {"uci": "d7d5", "score_cp": 10, "rank": 1},
            {"uci": "e7e5", "score_cp": 5, "rank": 2},  # also in else pref, eligible
            {"uci": "g8f6", "score_cp": 0, "rank": 3},
        ],
    )
    client = StubClient()
    move = play("black", ["b1c3"], engine, client)
    # d7d5 and e7e5 are both in the fallback pref {e7e5, d7d5} and within 25cp.
    if move not in ("d7d5", "e7e5"):
        print(f"FAIL test_unusual_white_opening_falls_back: expected d7d5 or e7e5, got {move}")
        return False
    print(f"PASS test_unusual_white_opening_falls_back: bot played {move} (no weird forced move)")
    return True


def test_unusual_white_opening_deterministic():
    """Deterministic version: 1.Nc3 is not in the table, engine bestmove is
    g8f6 (not in fallback pref {e7e5, d7d5}) at 10cp, and d7d5 is 5cp worse.
    Only d7d5 qualifies -> bot must play d7d5.
    """
    engine = MockEngine(
        bestmove="g8f6",  # not in {e7e5, d7d5}
        multipv_candidates=[
            {"uci": "g8f6", "score_cp": 10, "rank": 1},
            {"uci": "d7d5", "score_cp": 5, "rank": 2},  # eligible
        ],
    )
    client = StubClient()
    move = play("black", ["b1c3"], engine, client)
    if move != "d7d5":
        print(f"FAIL test_unusual_white_opening_deterministic: expected d7d5, got {move}")
        return False
    print(f"PASS test_unusual_white_opening_deterministic: bot played {move}")
    return True


def test_white_retic_still_fires():
    """White, ply 0: Reti preference is g1f3. Engine bestmove e2e4 (score 10)
    is not in the pref set {g1f3}, and g1f3 is 5cp worse -> only g1f3 eligible
    -> bot must play g1f3 (Reti).
    """
    engine = MockEngine(
        bestmove="e2e4",  # not in {g1f3}
        multipv_candidates=[
            {"uci": "e2e4", "score_cp": 10, "rank": 1},
            {"uci": "g1f3", "score_cp": 5, "rank": 2},  # eligible
        ],
    )
    client = StubClient()
    move = play("white", [], engine, client)
    if move != "g1f3":
        print(f"FAIL test_white_retic_still_fires: expected g1f3, got {move}")
        return False
    print(f"PASS test_white_retic_still_fires: bot played {move} (Reti preference fired)")
    return True


def test_counterplay_vs_c4():
    """Black vs 1.c4: opening_pref = {e7e5, g8f6}. Engine bestmove g8f6
    (score 5) is in the pref set; e7e5 (score 0) is also in the set and within
    25cp. Both eligible -> random.choice picks one of {e7e5, g8f6}.

    Assert result is in the acceptable set and that the preference branch fired.
    """
    engine = MockEngine(
        bestmove="g8f6",
        multipv_candidates=[
            {"uci": "g8f6", "score_cp": 5, "rank": 1},
            {"uci": "e7e5", "score_cp": 0, "rank": 2},  # eligible
        ],
    )
    client = StubClient()
    import io
    import contextlib
    buf = io.StringIO()
    handler = make_handler("black", ["c2c4"], engine, client)
    with contextlib.redirect_stdout(buf):
        handler._make_move()
    move = client.last_uci
    logs = buf.getvalue()
    if move not in ("e7e5", "g8f6"):
        print(f"FAIL test_counterplay_vs_c4: expected e7e5 or g8f6, got {move}")
        return False
    if "Opening preference:" not in logs:
        print(f"FAIL test_counterplay_vs_c4: preference branch did not fire. logs={logs!r}")
        return False
    print(f"PASS test_counterplay_vs_c4: bot played {move} (preference fired)")
    return True


def test_counterplay_vs_c4_deterministic():
    """Deterministic: 1.c4, engine bestmove g8f6 is in pref {e7e5, g8f6} and
    is rank 1, so it is eligible and will be chosen (or e7e5). To force a
    unique pick, make engine bestmove something NOT in the pref set so only
    one preferred candidate qualifies. Use bestmove b8c6 (not in pref) at 5cp
    and e7e5 at 0cp -> e7e5 eligible and unique.
    """
    engine = MockEngine(
        bestmove="b8c6",  # not in {e7e5, g8f6}
        multipv_candidates=[
            {"uci": "b8c6", "score_cp": 5, "rank": 1},
            {"uci": "e7e5", "score_cp": 0, "rank": 2},  # eligible & unique
        ],
    )
    client = StubClient()
    move = play("black", ["c2c4"], engine, client)
    if move != "e7e5":
        print(f"FAIL test_counterplay_vs_c4_deterministic: expected e7e5, got {move}")
        return False
    print(f"PASS test_counterplay_vs_c4_deterministic: bot played {move}")
    return True


# ---- Runner ----------------------------------------------------------------

def main() -> int:
    tests = [
        test_sicilian_fires_vs_e4,
        test_sicilian_deterministic_when_only_c5_qualifies,
        test_grunfeld_d5_fires_vs_d4,
        test_grunfeld_deterministic_when_only_d5_qualifies,
        test_safety_bad_counterplay_not_forced,
        test_no_counterplay_after_ply8,
        test_unusual_white_opening_falls_back,
        test_unusual_white_opening_deterministic,
        test_white_retic_still_fires,
        test_counterplay_vs_c4,
        test_counterplay_vs_c4_deterministic,
    ]
    failures = 0
    for test in tests:
        try:
            ok = test()
            if ok is False:
                failures += 1
        except Exception as e:
            import traceback
            print(f"FAIL {test.__name__}: raised {e}")
            traceback.print_exc()
            failures += 1
    if failures:
        print(f"\n{failures} test(s) FAILED")
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())