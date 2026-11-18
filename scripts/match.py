#!/usr/bin/env python3
"""
Head-to-head match harness using python-chess.
Plays 40 games (20 openings x 2 colors) between two UCI engines.
"""

import argparse
import json
import math
import sys
import chess
import chess.engine

# 20 opening sequences in UCI notation
OPENINGS = [
    "e2e4 e7e5 g1f3 b8c6 f1b5 a7a6",
    "e2e4 c7c5 g1f3 d7d6 d2d4 c5d4",
    "e2e4 e7e6 d2d4 d7d5 b1c3 g8f6",
    "e2e4 c7c6 d2d4 d7d5 b1c3 d5e4",
    "e2e4 d7d5 e4d5 d8d5 b1c3 d5a5",
    "e2e4 g8f6 e4e5 f6d5 d2d4 d7d6",
    "d2d4 d7d5 c2c4 e7e6 b1c3 g8f6",
    "d2d4 g8f6 c2c4 g7g6 b1c3 f8g7",
    "d2d4 g8f6 c2c4 e7e6 g1f3 b7b6",
    "d2d4 d7d5 c2c4 c7c6 g1f3 g8f6",
    "d2d4 g8f6 c2c4 e7e6 b1c3 f8b4",
    "d2d4 d7d5 c2c4 d5c4 g1f3 g8f6",
    "c2c4 e7e5 b1c3 g8f6 g2g3 d7d5",
    "c2c4 g8f6 b1c3 e7e6 d2d4 d7d5",
    "g1f3 d7d5 g2g3 g8f6 f1g2 g7g6",
    "g1f3 g8f6 c2c4 e7e6 b1c3 d7d5",
    "e2e4 e7e5 g1f3 g8f6 f3e5 d7d6",
    "e2e4 e7e5 f1c4 g8f6 d2d3 f8c5",
    "d2d4 e7e6 e2e4 d7d5 b1c3 g8f6",
    "e2e4 c7c5 b1c3 b8c6 g2g3 g7g6",
]


def parse_args():
    parser = argparse.ArgumentParser(description="Head-to-head engine match")
    parser.add_argument("--engine-a", default=r"E:\dev\duckish\target\release\duckish.exe")
    parser.add_argument("--engine-b", default=r"E:\dev\duckish\target\release\viridithas_base.exe")
    parser.add_argument("--movetime", type=float, default=0.1, help="Seconds per move")
    parser.add_argument("--max-plies", type=int, default=240, help="Adjudicate as draw beyond this")
    parser.add_argument("--hash-a", type=int, default=64, help="Hash in MB for engine A")
    parser.add_argument("--hash-b", type=int, default=64, help="Hash in MB for engine B")
    parser.add_argument("--threads-a", type=int, default=1, help="Threads for engine A")
    parser.add_argument("--threads-b", type=int, default=1, help="Threads for engine B")
    parser.add_argument("--json-out", default="", help="Optional JSON output path")
    parser.add_argument("--games", type=int, default=40, help="Total games (rounded down to even)")
    return parser.parse_args()


def configure_engine(engine, hash_mb, threads):
    """Configure engine with only options it supports."""
    opts = {}
    if "Hash" in engine.options:
        opts["Hash"] = hash_mb
    if "Threads" in engine.options:
        opts["Threads"] = threads
    # MultiPV is a python-chess MANAGED_OPTION: configure() raises "cannot set MultiPV
    # which is automatically managed", so it must be left at the engine default.
    # Duckish ships with MultiPV default 1, so both engines search a single PV here.
    mpv = engine.options.get("MultiPV")
    if mpv is not None and getattr(mpv, "default", 1) != 1:
        print(f"WARNING: engine default MultiPV is {mpv.default}, not 1 - match is not single-PV")
    if opts:
        engine.configure(opts)


def score_from_result(result_str, duckish_is_white):
    """Convert game result to Duckish score: 1.0 win, 0.5 draw, 0.0 loss."""
    if result_str == "1-0":
        return 1.0 if duckish_is_white else 0.0
    elif result_str == "0-1":
        return 0.0 if duckish_is_white else 1.0
    else:  # "1/2-1/2"
        return 0.5


def elo_from_score(score, n_games):
    """Calculate Elo difference from score with 1/(2N) correction for extremes."""
    if score <= 0:
        score = 1.0 / (2 * n_games)
    elif score >= 1:
        score = 1.0 - 1.0 / (2 * n_games)
    return -400 * math.log10(1.0 / score - 1.0)


def main():
    args = parse_args()

    # Round games down to nearest even number (2 per opening)
    total_games = (args.games // 2) * 2
    if total_games == 0:
        print("No games to play")
        return

    engine_a = None
    engine_b = None
    scores = []
    wins = losses = draws = 0
    adjudicated_draws = 0
    game_num = 0

    try:
        engine_a = chess.engine.SimpleEngine.popen_uci(args.engine_a)
        engine_b = chess.engine.SimpleEngine.popen_uci(args.engine_b)

        configure_engine(engine_a, args.hash_a, args.threads_a)
        configure_engine(engine_b, args.hash_b, args.threads_b)

        print(f"A={args.engine_a}  threads={args.threads_a}  hash={args.hash_a}MB")
        print(f"B={args.engine_b}  threads={args.threads_b}  hash={args.hash_b}MB")

        limit = chess.engine.Limit(time=args.movetime)
        openings_to_use = OPENINGS[:total_games // 2]

        for opening_idx, opening_uci in enumerate(openings_to_use):
            # Two games per opening: A=White, then B=White
            for duckish_white in (True, False):
                game_num += 1
                board = chess.Board()

                # Apply opening moves
                bad_opening = False
                for move_uci in opening_uci.split():
                    try:
                        board.push_uci(move_uci)
                    except (ValueError, chess.IllegalMoveError):
                        print(f"BAD_OPENING {opening_idx} {move_uci}")
                        bad_opening = True
                        break

                if bad_opening:
                    game_num -= 1  # Don't count this game
                    continue

                white_engine = engine_a if duckish_white else engine_b
                black_engine = engine_b if duckish_white else engine_a

                # Play the game
                while not board.is_game_over(claim_draw=True) and len(board.move_stack) < args.max_plies:
                    result = white_engine.play(board, limit) if board.turn == chess.WHITE else black_engine.play(board, limit)
                    if result.move is None:
                        break
                    board.push(result.move)

                # Determine result
                adjudicated = False
                if board.is_game_over(claim_draw=True):
                    result_str = board.result(claim_draw=True)
                else:
                    result_str = "1/2-1/2"
                    adjudicated = True
                    adjudicated_draws += 1

                score = score_from_result(result_str, duckish_white)
                scores.append(score)

                if score == 1.0:
                    wins += 1
                elif score == 0.0:
                    losses += 1
                else:
                    draws += 1

                plies = len(board.move_stack)
                color_str = "A=white" if duckish_white else "B=white"
                print(f"game {game_num:02d}/{total_games}  {color_str}  opening={opening_idx}  result={result_str}  plies={plies}")
                print(f"  Running: W={wins} L={losses} D={draws}  Score={sum(scores)/len(scores):.4f}")

        # Final statistics
        n = len(scores)
        mean_score = sum(scores) / n if n > 0 else 0.0

        # Sample standard deviation
        if n > 1:
            variance = sum((x - mean_score) ** 2 for x in scores) / (n - 1)
            sd = math.sqrt(variance)
            se = sd / math.sqrt(n)
        else:
            se = 0.0

        # 95% CI on score, clamped to (0, 1)
        ci_low_score = max(0.0, min(1.0, mean_score - 1.96 * se))
        ci_high_score = max(0.0, min(1.0, mean_score + 1.96 * se))

        elo = elo_from_score(mean_score, n)
        elo_low = elo_from_score(ci_low_score, n)
        elo_high = elo_from_score(ci_high_score, n)

        print(f"\nWINS {wins}  LOSSES {losses}  DRAWS {draws}  GAMES {n}")
        print(f"SCORE {mean_score:.4f}")
        print(f"ELO {elo:+.0f}  CI95 [{elo_low:+.0f}, {elo_high:+.0f}]")
        print(f"MOVETIME {args.movetime}  ADJUDICATED_DRAWS {adjudicated_draws}")

        # JSON output
        if args.json_out:
            data = {
                "wins": wins,
                "losses": losses,
                "draws": draws,
                "games": n,
                "score": round(mean_score, 4),
                "elo": round(elo),
                "elo_ci_low": round(elo_low),
                "elo_ci_high": round(elo_high),
                "adjudicated_draws": adjudicated_draws,
                "movetime": args.movetime,
                "threads_a": args.threads_a,
                "threads_b": args.threads_b,
                "hash_a": args.hash_a,
                "hash_b": args.hash_b,
            }
            with open(args.json_out, "w") as f:
                json.dump(data, f, indent=2)

    finally:
        if engine_a:
            engine_a.quit()
        if engine_b:
            engine_b.quit()


if __name__ == "__main__":
    main()