#!/usr/bin/env python3
"""
Unit tests for ExperienceStore.
Plain functions, print PASS/FAIL, like test_promotion.py.
"""

import os
import tempfile
from experience import ExperienceStore, BLUNDER_THRESHOLD_CP, LEARNING_BONUS_CP, mate_score_to_cp, MATE_MAX_CP


def test_store_roundtrip_position():
    """Test record_position and get_position round-trip."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = ExperienceStore(db_path)
        
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        store.record_position(fen, 15.0, "win")
        store.record_position(fen, 20.0, "loss")  # second visit, updates
        
        pos = store.get_position(fen)
        if pos is None:
            print("FAIL: get_position returned None")
            return
        if pos["fen"] != fen:
            print(f"FAIL: fen mismatch: {pos['fen']} != {fen}")
            return
        if pos["visits"] != 2:
            print(f"FAIL: visits should be 2, got {pos['visits']}")
            return
        if pos["win_count"] != 1:
            print(f"FAIL: win_count should be 1, got {pos['win_count']}")
            return
        if pos["loss_count"] != 1:
            print(f"FAIL: loss_count should be 1, got {pos['loss_count']}")
            return
        if pos["draw_count"] != 0:
            print(f"FAIL: draw_count should be 0, got {pos['draw_count']}")
            return
        if pos["deep_score_cp"] != 20.0:
            print(f"FAIL: deep_score_cp should be 20.0 (last), got {pos['deep_score_cp']}")
            return
        
        print("PASS: test_store_roundtrip_position")
    except Exception as e:
        print(f"FAIL: test_store_roundtrip_position -> {type(e).__name__}: {e}")
    finally:
        try:
            os.unlink(db_path)
        except Exception:
            pass


def test_store_roundtrip_move():
    """Test record_move and get_move round-trip."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = ExperienceStore(db_path)
        
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        move_uci = "e2e4"
        store.record_move(fen, move_uci, penalty_cp=10.0, bonus_cp=5.0, visits=1)
        store.record_move(fen, move_uci, penalty_cp=20.0, bonus_cp=0.0, visits=1)  # average
        
        move = store.get_move(fen, move_uci)
        if move is None:
            print("FAIL: get_move returned None")
            return
        if move["fen"] != fen:
            print(f"FAIL: fen mismatch: {move['fen']} != {fen}")
            return
        if move["move_uci"] != move_uci:
            print(f"FAIL: move_uci mismatch: {move['move_uci']} != {move_uci}")
            return
        # Average penalty: (10*1 + 20*1) / 2 = 15.0
        if abs(move["penalty_cp"] - 15.0) > 0.001:
            print(f"FAIL: penalty_cp should be 15.0, got {move['penalty_cp']}")
            return
        # Average bonus: (5*1 + 0*1) / 2 = 2.5
        if abs(move["bonus_cp"] - 2.5) > 0.001:
            print(f"FAIL: bonus_cp should be 2.5, got {move['bonus_cp']}")
            return
        if move["visits"] != 2:
            print(f"FAIL: visits should be 2, got {move['visits']}")
            return
        
        print("PASS: test_store_roundtrip_move")
    except Exception as e:
        print(f"FAIL: test_store_roundtrip_move -> {type(e).__name__}: {e}")
    finally:
        try:
            os.unlink(db_path)
        except Exception:
            pass


def test_apply_adjustments_re_ranks():
    """Test apply_adjustments re-ranks candidates after penalty and sets learned flag."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = ExperienceStore(db_path)
        
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        # Record penalty for e2e4, bonus for d2d4
        store.record_move(fen, "e2e4", penalty_cp=50.0, bonus_cp=0.0)
        store.record_move(fen, "d2d4", penalty_cp=0.0, bonus_cp=20.0)
        
        candidates = [
            {"uci": "e2e4", "score_cp": 30, "rank": 1},
            {"uci": "d2d4", "score_cp": 20, "rank": 2},
            {"uci": "g1f3", "score_cp": 10, "rank": 3},  # no experience
        ]
        
        adjusted = store.apply_adjustments(fen, candidates)
        
        # Check learned flags
        e2e4_adj = next(c for c in adjusted if c["uci"] == "e2e4")
        d2d4_adj = next(c for c in adjusted if c["uci"] == "d2d4")
        g1f3_adj = next(c for c in adjusted if c["uci"] == "g1f3")
        
        if not e2e4_adj.get("learned", False):
            print("FAIL: e2e4 should have learned=true (has penalty)")
            return
        if not d2d4_adj.get("learned", False):
            print("FAIL: d2d4 should have learned=true (has bonus)")
            return
        if g1f3_adj.get("learned", False):
            print("FAIL: g1f3 should have learned=false (no experience)")
            return
        
        # Check adjusted scores
        # e2e4: 30 - 50 = -20
        # d2d4: 20 + 20 = 40
        # g1f3: 10 (no change)
        if abs(e2e4_adj.get("adjusted_score_cp", 0) - (-20.0)) > 0.001:
            print(f"FAIL: e2e4 adjusted_score_cp should be -20, got {e2e4_adj.get('adjusted_score_cp')}")
            return
        if abs(d2d4_adj.get("adjusted_score_cp", 0) - 40.0) > 0.001:
            print(f"FAIL: d2d4 adjusted_score_cp should be 40, got {d2d4_adj.get('adjusted_score_cp')}")
            return
        if abs(g1f3_adj.get("adjusted_score_cp", 0) - 10.0) > 0.001:
            print(f"FAIL: g1f3 adjusted_score_cp should be 10, got {g1f3_adj.get('adjusted_score_cp')}")
            return
        
        # Check re-ranking: d2d4 (40) > g1f3 (10) > e2e4 (-20)
        if adjusted[0]["uci"] != "d2d4":
            print(f"FAIL: rank 1 should be d2d4, got {adjusted[0]['uci']}")
            return
        if adjusted[1]["uci"] != "g1f3":
            print(f"FAIL: rank 2 should be g1f3, got {adjusted[1]['uci']}")
            return
        if adjusted[2]["uci"] != "e2e4":
            print(f"FAIL: rank 3 should be e2e4, got {adjusted[2]['uci']}")
            return
        
        # Check ranks updated
        if adjusted[0]["rank"] != 1 or adjusted[1]["rank"] != 2 or adjusted[2]["rank"] != 3:
            print("FAIL: ranks not updated correctly")
            return
        
        print("PASS: test_apply_adjustments_re_ranks")
    except Exception as e:
        print(f"FAIL: test_apply_adjustments_re_ranks -> {type(e).__name__}: {e}")
    finally:
        try:
            os.unlink(db_path)
        except Exception:
            pass


def test_get_stats():
    """Test get_stats returns correct counts."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = ExperienceStore(db_path)
        
        store.record_position("fen1", 10.0, "win")
        store.record_position("fen2", 20.0, "loss")
        store.record_position("fen1", 15.0, "draw")  # second visit to fen1
        store.record_move("fen1", "e2e4", 5.0, 0.0)
        store.record_move("fen1", "d2d4", 0.0, 10.0)
        store.record_move("fen2", "e7e5", 3.0, 0.0)
        
        stats = store.get_stats()
        
        if stats["positions_count"] != 2:
            print(f"FAIL: positions_count should be 2, got {stats['positions_count']}")
            return
        if stats["positions_total_visits"] != 3:
            print(f"FAIL: positions_total_visits should be 3, got {stats['positions_total_visits']}")
            return
        if stats["moves_count"] != 3:
            print(f"FAIL: moves_count should be 3, got {stats['moves_count']}")
            return
        if stats["moves_total_visits"] != 3:
            print(f"FAIL: moves_total_visits should be 3, got {stats['moves_total_visits']}")
            return
        if stats["games_won"] != 1:
            print(f"FAIL: games_won should be 1, got {stats['games_won']}")
            return
        if stats["games_lost"] != 1:
            print(f"FAIL: games_lost should be 1, got {stats['games_lost']}")
            return
        if stats["games_drawn"] != 1:
            print(f"FAIL: games_drawn should be 1, got {stats['games_drawn']}")
            return
        
        print("PASS: test_get_stats")
    except Exception as e:
        print(f"FAIL: test_get_stats -> {type(e).__name__}: {e}")
    finally:
        try:
            os.unlink(db_path)
        except Exception:
            pass


def test_constants():
    """Test that constants are defined correctly."""
    if BLUNDER_THRESHOLD_CP != 80.0:
        print(f"FAIL: BLUNDER_THRESHOLD_CP should be 80.0, got {BLUNDER_THRESHOLD_CP}")
        return
    if LEARNING_BONUS_CP != 15.0:
        print(f"FAIL: LEARNING_BONUS_CP should be 15.0, got {LEARNING_BONUS_CP}")
        return
    print("PASS: test_constants")


def test_mate_score_to_cp():
    """Test mate_score_to_cp conversion function."""
    # mate in 3 -> 100000 - 3*1000 = 97000
    if mate_score_to_cp(3) != 97000.0:
        print(f"FAIL: mate_score_to_cp(3) should be 97000.0, got {mate_score_to_cp(3)}")
        return
    # mate in -3 -> -100000 + 3*1000 = -97000
    if mate_score_to_cp(-3) != -97000.0:
        print(f"FAIL: mate_score_to_cp(-3) should be -97000.0, got {mate_score_to_cp(-3)}")
        return
    # None -> 0.0
    if mate_score_to_cp(None) != 0.0:
        print(f"FAIL: mate_score_to_cp(None) should be 0.0, got {mate_score_to_cp(None)}")
        return
    print("PASS: test_mate_score_to_cp")


def test_apply_adjustments_mate_candidate():
    """Test apply_adjustments handles mate scores correctly."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = ExperienceStore(db_path)
        store.ensure_schema()
        
        # Candidates: one with mate score, one with cp score
        cands = [
            {"uci": "a2a4", "score_cp": None, "score_mate": 3, "rank": 1},
            {"uci": "h7h6", "score_cp": 25, "score_mate": None, "rank": 2},
        ]
        
        adjusted = store.apply_adjustments("8/8/8/8/8/8/8/K6k w - - 0 1", cands)
        
        # a2a4 (mate in 3) should be rank 1 with adjusted_score_cp = 97000.0
        if adjusted[0]["uci"] != "a2a4":
            print(f"FAIL: rank 1 should be a2a4, got {adjusted[0]['uci']}")
            return
        if abs(adjusted[0]["adjusted_score_cp"] - 97000.0) > 0.001:
            print(f"FAIL: a2a4 adjusted_score_cp should be 97000.0, got {adjusted[0]['adjusted_score_cp']}")
            return
        if adjusted[0]["rank"] != 1:
            print(f"FAIL: a2a4 rank should be 1, got {adjusted[0]['rank']}")
            return
        
        # h7h6 (cp 25) should be rank 2 with adjusted_score_cp = 25.0
        if adjusted[1]["adjusted_score_cp"] != 25.0:
            print(f"FAIL: h7h6 adjusted_score_cp should be 25.0, got {adjusted[1]['adjusted_score_cp']}")
            return
        
        print("PASS: test_apply_adjustments_mate_candidate")
    except Exception as e:
        print(f"FAIL: test_apply_adjustments_mate_candidate -> {type(e).__name__}: {e}")
    finally:
        try:
            os.unlink(db_path)
        except Exception:
            pass


def test_san_whitespace_normalization():
    """Test that SAN moves with whitespace parse correctly after normalization."""
    import re
    import chess
    
    # Test basic whitespace stripping - moves legal from startpos
    tests = [
        (["N f3", "N f6"], "Knight moves with space"),
        (["e 4", "e 5"], "Pawn moves with space"),
    ]
    
    for moves, description in tests:
        normalized = [re.sub(r'\s+', '', m) for m in moves]
        board = chess.Board()
        try:
            for san in normalized:
                move = board.parse_san(san)
                board.push(move)
            print(f"PASS: {description} -> {normalized}")
        except Exception as e:
            print(f"FAIL: {description} -> {type(e).__name__}: {e}")
            return

    # Test castling kingside with spaces (in a position where it's legal)
    normalized = [re.sub(r'\s+', '', "O - O")]
    board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    try:
        move = board.parse_san(normalized[0])
        board.push(move)
        print(f"PASS: Castling kingside with spaces -> {normalized}")
    except Exception as e:
        print(f"FAIL: Castling kingside with spaces -> {type(e).__name__}: {e}")
        return

    # Test castling queenside with spaces
    normalized = [re.sub(r'\s+', '', "O - O - O")]
    board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    try:
        move = board.parse_san(normalized[0])
        board.push(move)
        print(f"PASS: Castling queenside with spaces -> {normalized}")
    except Exception as e:
        print(f"FAIL: Castling queenside with spaces -> {type(e).__name__}: {e}")
        return

    # Test promotion with check - set up a legal position
    # White pawn on e7, black king on e1 -> e7e8=Q gives check
    normalized = [re.sub(r'\s+', '', "e 8 = Q")]
    board = chess.Board("8/4P3/8/8/8/8/8/4k3 w - - 0 1")
    try:
        move = board.parse_san(normalized[0])
        board.push(move)
        print(f"PASS: Promotion with spaces -> {normalized}")
    except Exception as e:
        print(f"FAIL: Promotion with spaces -> {type(e).__name__}: {e}")
        return

    # Test "axb1= Q+" -> "axb1=Q+" normalization (just verify string normalization)
    promo_tests = [
        ("axb1= Q+", "axb1=Q+"),
        ("e8 = N #", "e8=N#"),
        ("h1 = R +", "h1=R+"),
    ]
    
    for raw, expected in promo_tests:
        normalized = re.sub(r'\s+', '', raw)
        if normalized != expected:
            print(f"FAIL: normalization mismatch: {raw} -> {normalized} != {expected}")
            return
        print(f"PASS: normalization {raw} -> {normalized}")

if __name__ == "__main__":
    test_constants()
    test_mate_score_to_cp()
    test_store_roundtrip_position()
    test_store_roundtrip_move()
    test_apply_adjustments_re_ranks()
    test_apply_adjustments_mate_candidate()
    test_get_stats()
    test_san_whitespace_normalization()