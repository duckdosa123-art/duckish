#!/usr/bin/env python3
"""Load / branch-coverage benchmark for the draw-offer decision system.

Standalone, runnable:

    python bridge/bench_draw.py

It proves the draw decision system stays fast and shows what it actually
decides. TDD RED phase: the methods it calls (_is_proven_draw, draw_decision,
_effective_movetime, _repeat_threshold) do NOT exist yet, so this script
raises AttributeError until the implementation lands. That is expected.

What it does:
  * Runs a set of hand-picked FEN scenarios through every decision helper and
    prints what the bot would do in each.
  * Runs 5000+ randomized trials, measuring wall time, decisions/sec and
    min/median/p95 latency, and prints a sorted branch-distribution table.
  * Verifies determinism (same seeded input twice -> identical output).
  * Writes the full report to bridge/bench_report.txt.

Exit 0 only if every assertion holds and every expected decision branch was
actually exercised; otherwise exit 1.
"""

import os
import sys
import time
import random
import statistics
from typing import Any, Dict, List, Tuple

# Make the bridge package importable regardless of the caller's cwd.
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

import chess
from lichess.runner import GameHandler

REPORT_PATH = os.path.join(BRIDGE_DIR, "bench_report.txt")

# Hand-picked FENs spanning the whole decision space.
FENS: Dict[str, str] = {
    "KvK (proven)": "8/8/8/8/8/8/4k3/4K3 w - - 0 1",
    "KBvK (proven)": "8/8/8/8/8/8/4k3/4KB2 w - - 0 1",
    "KNvKN (not proven)": "8/8/8/8/8/8/4kn2/4KN2 w - - 0 1",
    "KRvKR (effective)": "8/8/8/8/8/8/4k2r/4K2R w - - 0 1",
    "6pc (queens)": "r2qk3/8/8/8/8/8/8/R2QK3 w - - 0 1",
    "castling 6pc": "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
    "startpos": "startpos",
}

# 12+ hand-picked scenarios: (label, fen, score, our_ms, their_ms, our_rating, opp_rating)
SCENARIOS: List[Tuple[str, str, int, int, int, int, int]] = [
    ("proven + can flag them", FENS["KvK (proven)"], 0, 180000, 10000, 2500, 2500),
    ("proven draw hunt", FENS["KvK (proven)"], 0, 180000, 180000, 2500, 2500),
    ("higher-rated effective", FENS["KRvKR (effective)"], 0, 180000, 180000, 2700, 2500),
    ("lower-rated accept", FENS["KRvKR (effective)"], 0, 180000, 180000, 2500, 2700),
    ("equal-rated accept", FENS["KRvKR (effective)"], 0, 180000, 180000, 2600, 2600),
    ("unknown rating accept", FENS["KRvKR (effective)"], 0, 180000, 180000, 0, 0),
    ("winning decline", FENS["startpos"], 200, 180000, 180000, 2600, 2600),
    ("losing accept", FENS["startpos"], -200, 180000, 180000, 2600, 2600),
    ("low clock accept", FENS["startpos"], 0, 10000, 180000, 2600, 2600),
    ("proven + low our clock", FENS["KBvK (proven)"], 0, 12000, 20000, 2500, 2500),
    ("six-piece queens drawn", FENS["6pc (queens)"], 20, 180000, 180000, 2700, 2600),
    ("castling drawn", FENS["castling 6pc"], -10, 180000, 180000, 2600, 2700),
    ("boundary +50 drawn", FENS["startpos"], 50, 180000, 180000, 2600, 2600),
    ("KRvKR winning", FENS["KRvKR (effective)"], 60, 180000, 180000, 2600, 2600),
]

# Branch labels we expect the randomized sweep to reach.
REQUIRED_BRANCHES = [
    "accept:low clock",
    "accept:losing",
    "decline:winning",
    "decline:can flag",
    "decline:higher-rated",
    "accept:drawn",
]

CLOCK_CHOICES = [5000, 12000, 18000, 25000, 40000, 65000, 90000, 120000, 180000, 300000]


# ---- Offline harness -------------------------------------------------------

class DrawStubClient:
    def accept_draw(self, game_id):
        return {"ok": True}

    def decline_draw(self, game_id):
        return {"ok": True}

    def make_move(self, game_id, uci):
        return {"ok": True}


class MockEngine:
    def __init__(self, score_cp=0, bestmove="g1f3"):
        self.score_cp = score_cp
        self.bestmove_uci = bestmove

    def best_moves_multipv(self, moves_uci, movetime=1500, multipv=3):
        return [{"uci": self.bestmove_uci, "score_cp": self.score_cp, "rank": 1}]

    def best_move(self, moves_uci, **kwargs):
        return self.bestmove_uci


class ExpStub:
    def record_game(self, *a, **k):
        return {"skipped": True}


def make_handler() -> GameHandler:
    """Build a GameHandler without starting the game thread."""
    h = object.__new__(GameHandler)
    h.client = DrawStubClient()
    h.engine = MockEngine()
    h.experience = ExpStub()
    h.game_id = "BENCH"
    h.color = "white"
    h.initial_fen = "startpos"
    h.moves_uci = []
    h.wtime = 180000
    h.btime = 180000
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
    h.logs = []
    h._log = lambda msg, _logs=h.logs: _logs.append(msg)
    return h


# ---- Decision helpers ------------------------------------------------------

def board_for(fen: str) -> chess.Board:
    return chess.Board() if fen == "startpos" else chess.Board(fen)


def classify(decision: Dict[str, Any]) -> str:
    """Map a draw_decision() result to a stable branch label."""
    action = decision.get("action")
    reason = (decision.get("reason") or "").lower()
    if action == "accept":
        if "low clock" in reason:
            return "accept:low clock"
        if "losing" in reason:
            return "accept:losing"
        if "drawn" in reason:
            return "accept:drawn"
        return "accept:other"
    # decline
    if "winning" in reason:
        return "decline:winning"
    if "can flag" in reason:
        return "decline:can flag"
    if "proven draw" in reason:
        return "decline:proven draw"
    if "higher-rated" in reason:
        return "decline:higher-rated"
    return "decline:other"


def run_trial(h: GameHandler, trial: Dict[str, Any]) -> Tuple[str, str, bool]:
    """One draw_decision() call plus the helper probes, returning (action,
    reason, fast_mode). Timed by the caller."""
    fen = trial["fen"]
    board = board_for(fen)
    decision = h.draw_decision(
        trial["score"],
        trial["our_ms"],
        trial["their_ms"],
        board,
        trial["our_rating"],
        trial["opp_rating"],
    )
    # Exercise the other helpers so their behaviour is covered under load too.
    h._is_proven_draw(board)
    h._effective_movetime(1500, trial["our_ms"], decision.get("fast_mode", False))
    h._repeat_threshold(trial["our_ms"] - trial["their_ms"], decision.get("fast_mode", False))
    return (
        decision.get("action"),
        decision.get("reason"),
        bool(decision.get("fast_mode", False)),
    )


def gen_trials(n: int, seed: int) -> List[Dict[str, Any]]:
    rng = random.Random(seed)
    fens = list(FENS.values())
    trials: List[Dict[str, Any]] = []
    for _ in range(n):
        trials.append({
            "fen": rng.choice(fens),
            "score": rng.randint(-400, 400),
            "our_ms": rng.choice(CLOCK_CHOICES),
            "their_ms": rng.choice(CLOCK_CHOICES),
            "our_rating": rng.randint(2600, 3000),
            "opp_rating": rng.randint(2600, 3000),
        })
    return trials


# ---- Report ----------------------------------------------------------------

def main() -> int:
    lines: List[str] = []

    def emit(msg: str = "") -> None:
        print(msg, flush=True)
        lines.append(msg)

    h = make_handler()

    emit("=" * 72)
    emit("DRAW DECISION BENCHMARK")
    emit("=" * 72)

    # -- Scenario walkthrough ------------------------------------------------
    emit("")
    emit("-- Hand-picked scenarios --")
    emit(f"{'scenario':<28} {'score':>6} {'our':>7} {'their':>7} {'rating':>11} "
         f"{'proven':<22} {'action':<8} {'fast':<5} reason")
    for (label, fen, score, our_ms, their_ms, our_r, opp_r) in SCENARIOS:
        board = board_for(fen)
        proven = h._is_proven_draw(board) or "-"
        d = h.draw_decision(score, our_ms, their_ms, board, our_r, opp_r)
        rating = f"{our_r}/{opp_r}"
        emit(f"{label:<28} {score:>6} {our_ms:>7} {their_ms:>7} {rating:>11} "
             f"{str(proven):<22} {d['action']:<8} {str(d['fast_mode']):<5} {d['reason']}")

    # -- Helper spot checks --------------------------------------------------
    emit("")
    emit("-- Helper spot checks --")
    for (label, fen, score, our_ms, their_ms, our_r, opp_r) in SCENARIOS:
        board = board_for(fen)
        d = h.draw_decision(score, our_ms, their_ms, board, our_r, opp_r)
        em = h._effective_movetime(1500, our_ms, d["fast_mode"])
        rt = h._repeat_threshold(our_ms - their_ms, d["fast_mode"])
        emit(f"  {label:<28} movetime={em:>5}ms repeat_threshold={rt}")

    # -- Randomized sweep ----------------------------------------------------
    N = 6000
    SEED = 20261123
    trials = gen_trials(N, SEED)

    emit("")
    emit("-- Randomized sweep --")
    emit(f"trials={N} seed={SEED}")

    branch_counts: Dict[str, int] = {}
    fast_on = 0
    fast_off = 0
    accept_count = 0
    decline_count = 0
    latencies_ms: List[float] = []

    t0 = time.perf_counter()
    for trial in trials:
        s = time.perf_counter()
        action, reason, fast = run_trial(h, trial)
        e = time.perf_counter()
        latencies_ms.append((e - s) * 1000.0)

        if action == "accept":
            accept_count += 1
        else:
            decline_count += 1
        if fast:
            fast_on += 1
        else:
            fast_off += 1
        label = classify({"action": action, "reason": reason, "fast_mode": fast})
        branch_counts[label] = branch_counts.get(label, 0) + 1
    total_s = time.perf_counter() - t0

    latencies_sorted = sorted(latencies_ms)
    p95 = latencies_sorted[int(len(latencies_sorted) * 0.95)]
    median = statistics.median(latencies_ms)
    mn = min(latencies_ms)
    mx = max(latencies_ms)
    dps = N / total_s if total_s > 0 else float("inf")

    emit("")
    emit(f"total wall time : {total_s:.4f} s")
    emit(f"decisions/sec   : {dps:,.0f}")
    emit(f"latency min     : {mn:.4f} ms")
    emit(f"latency median  : {median:.4f} ms")
    emit(f"latency p95     : {p95:.4f} ms")
    emit(f"latency max     : {mx:.4f} ms")
    emit(f"accept/decline  : {accept_count} / {decline_count}")
    emit(f"fast_mode on/off: {fast_on} / {fast_off}")

    emit("")
    emit("-- Branch distribution (sorted) --")
    emit(f"{'branch':<26} {'count':>7} {'share':>8}")
    for label, count in sorted(branch_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        share = 100.0 * count / N
        emit(f"{label:<26} {count:>7} {share:>6.2f}%")

    # -- Determinism check ---------------------------------------------------
    emit("")
    emit("-- Determinism check --")
    first = [run_trial(h, t) for t in trials[:500]]
    second = [run_trial(h, t) for t in trials[:500]]
    deterministic = first == second
    emit(f"identical across two seeded runs: {deterministic}")
    if not deterministic:
        for i, (a, b) in enumerate(zip(first, second)):
            if a != b:
                emit(f"  first divergence at trial {i}: {a!r} != {b!r}")
                break

    # -- Assertions ----------------------------------------------------------
    emit("")
    emit("-- Assertions --")
    ok = True
    if not deterministic:
        emit("FAIL determinism: two seeded runs diverged")
        ok = False
    for branch in REQUIRED_BRANCHES:
        count = branch_counts.get(branch, 0)
        status = "ok" if count > 0 else "FAIL"
        emit(f"  [{status}] {branch:<24} count={count}")
        if count == 0:
            ok = False

    # -- Write report --------------------------------------------------------
    try:
        with open(REPORT_PATH, "w", encoding="ascii") as fh:
            fh.write("\n".join(lines) + "\n")
        emit("")
        emit(f"report written to {REPORT_PATH}")
    except OSError as e:
        emit(f"WARNING: could not write report: {e}")

    emit("")
    if ok:
        emit(f"PASS bench_draw: {N} trials in {total_s:.4f}s "
             f"({dps:,.0f} decisions/sec), all {len(REQUIRED_BRANCHES)} required "
             f"branches reached, determinism ok.")
        return 0
    emit("FAIL bench_draw: one or more required branches were never reached, "
         "or determinism failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())