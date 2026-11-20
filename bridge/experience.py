#!/usr/bin/env python3
"""
Experience Store for Duckish Bridge.
Local SQLite database for position/move learning and mistake tracking.
Pure stdlib (sqlite3, threading).
"""

import sqlite3
import threading
from typing import Optional, List, Dict, Any, Tuple
import os

# Constants
BLUNDER_THRESHOLD_CP = 80.0
LEARNING_BONUS_CP = 15.0
MATE_MAX_CP = 100000.0
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "experience.db")


def mate_score_to_cp(mate: Optional[int]) -> float:
    """Convert mate-in-N to a centipawn score for ranking.
    Positive mate = winning, negative mate = losing.
    Uses MATE_MAX_CP - abs(mate)*1000 to prefer faster mates.
    """
    if mate is None:
        return 0.0
    if mate > 0:
        return MATE_MAX_CP - abs(mate) * 1000.0
    if mate < 0:
        return -MATE_MAX_CP + abs(mate) * 1000.0
    return 0.0


class ExperienceStore:
    """Thread-safe SQLite store for position evaluations and move experience."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_connection()
        self.ensure_schema()

    def _init_connection(self) -> None:
        """Initialize the SQLite connection with thread safety."""
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None  # autocommit mode
        )
        # Enable WAL mode for better concurrency
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def _get_cursor(self) -> sqlite3.Cursor:
        """Get a cursor, ensuring connection is alive."""
        if self._conn is None:
            self._init_connection()
        return self._conn.cursor()

    def ensure_schema(self) -> None:
        """Create tables if they don't exist."""
        with self._lock:
            cur = self._get_cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    fen TEXT PRIMARY KEY,
                    visits INTEGER NOT NULL DEFAULT 0,
                    deep_score_cp REAL,
                    win_count INTEGER NOT NULL DEFAULT 0,
                    loss_count INTEGER NOT NULL DEFAULT 0,
                    draw_count INTEGER NOT NULL DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS move_experience (
                    fen TEXT NOT NULL,
                    move_uci TEXT NOT NULL,
                    penalty_cp REAL NOT NULL DEFAULT 0.0,
                    bonus_cp REAL NOT NULL DEFAULT 0.0,
                    visits INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (fen, move_uci)
                )
            """)
            self._conn.commit()

    def record_position(self, fen: str, deep_score_cp: Optional[float], result: Optional[str]) -> None:
        """Record or update a position with visit count and game result."""
        with self._lock:
            cur = self._get_cursor()
            cur.execute("SELECT visits, win_count, loss_count, draw_count FROM positions WHERE fen = ?", (fen,))
            row = cur.fetchone()
            if row:
                visits, wins, losses, draws = row
                visits += 1
                if result == "win":
                    wins += 1
                elif result == "loss":
                    losses += 1
                elif result == "draw":
                    draws += 1
                cur.execute(
                    "UPDATE positions SET visits = ?, win_count = ?, loss_count = ?, draw_count = ?, deep_score_cp = ? WHERE fen = ?",
                    (visits, wins, losses, draws, deep_score_cp, fen)
                )
            else:
                win = 1 if result == "win" else 0
                loss = 1 if result == "loss" else 0
                draw = 1 if result == "draw" else 0
                cur.execute(
                    "INSERT INTO positions (fen, visits, deep_score_cp, win_count, loss_count, draw_count) VALUES (?, 1, ?, ?, ?, ?)",
                    (fen, deep_score_cp, win, loss, draw)
                )
            self._conn.commit()

    def record_move(self, fen: str, move_uci: str, penalty_cp: float, bonus_cp: float, visits: int = 1) -> None:
        """Record or update move experience with penalty/bonus."""
        with self._lock:
            cur = self._get_cursor()
            cur.execute("SELECT penalty_cp, bonus_cp, visits FROM move_experience WHERE fen = ? AND move_uci = ?", (fen, move_uci))
            row = cur.fetchone()
            if row:
                old_penalty, old_bonus, old_visits = row
                new_visits = old_visits + visits
                # Average the penalties and bonuses
                new_penalty = (old_penalty * old_visits + penalty_cp * visits) / new_visits
                new_bonus = (old_bonus * old_visits + bonus_cp * visits) / new_visits
                cur.execute(
                    "UPDATE move_experience SET penalty_cp = ?, bonus_cp = ?, visits = ? WHERE fen = ? AND move_uci = ?",
                    (new_penalty, new_bonus, new_visits, fen, move_uci)
                )
            else:
                cur.execute(
                    "INSERT INTO move_experience (fen, move_uci, penalty_cp, bonus_cp, visits) VALUES (?, ?, ?, ?, ?)",
                    (fen, move_uci, penalty_cp, bonus_cp, visits)
                )
            self._conn.commit()

    def get_position(self, fen: str) -> Optional[Dict[str, Any]]:
        """Get position record by FEN."""
        with self._lock:
            cur = self._get_cursor()
            cur.execute("SELECT fen, visits, deep_score_cp, win_count, loss_count, draw_count FROM positions WHERE fen = ?", (fen,))
            row = cur.fetchone()
            if row:
                return {
                    "fen": row[0],
                    "visits": row[1],
                    "deep_score_cp": row[2],
                    "win_count": row[3],
                    "loss_count": row[4],
                    "draw_count": row[5]
                }
            return None

    def get_move(self, fen: str, move_uci: str) -> Optional[Dict[str, Any]]:
        """Get move experience record."""
        with self._lock:
            cur = self._get_cursor()
            cur.execute("SELECT fen, move_uci, penalty_cp, bonus_cp, visits FROM move_experience WHERE fen = ? AND move_uci = ?", (fen, move_uci))
            row = cur.fetchone()
            if row:
                return {
                    "fen": row[0],
                    "move_uci": row[1],
                    "penalty_cp": row[2],
                    "bonus_cp": row[3],
                    "visits": row[4]
                }
            return None

    def apply_adjustments(self, fen: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Apply learned adjustments to candidate moves.
        Returns re-ranked candidates with 'learned' flag and adjusted scores.
        Handles both score_cp and score_mate (converted via mate_score_to_cp).
        """
        with self._lock:
            cur = self._get_cursor()
            # Fetch all move experience for this position
            cur.execute("SELECT move_uci, penalty_cp, bonus_cp FROM move_experience WHERE fen = ?", (fen,))
            exp_map = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

            adjusted = []
            for c in candidates:
                uci = c.get("uci", "")
                penalty, bonus = exp_map.get(uci, (0.0, 0.0))
                
                # Resolve effective score: prefer score_cp, else convert score_mate, else 0.0
                score_cp = c.get("score_cp")
                if score_cp is not None:
                    effective_score = float(score_cp)
                else:
                    score_mate = c.get("score_mate")
                    effective_score = mate_score_to_cp(score_mate) if score_mate is not None else 0.0
                
                adjusted_score = effective_score + bonus - penalty
                learned = penalty != 0.0 or bonus != 0.0
                
                new_c = dict(c)
                new_c["learned"] = learned
                new_c["penalty_cp"] = penalty
                new_c["bonus_cp"] = bonus
                new_c["adjusted_score_cp"] = adjusted_score
                adjusted.append(new_c)

            # Re-rank by adjusted_score_cp desc
            adjusted.sort(key=lambda x: x.get("adjusted_score_cp", -999999), reverse=True)
            # Update rank
            for i, c in enumerate(adjusted):
                c["rank"] = i + 1
            return adjusted

    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        with self._lock:
            cur = self._get_cursor()
            cur.execute("SELECT COUNT(*), SUM(visits) FROM positions")
            pos_count, pos_visits = cur.fetchone()
            cur.execute("SELECT COUNT(*), SUM(visits) FROM move_experience")
            move_count, move_visits = cur.fetchone()
            cur.execute("SELECT SUM(win_count), SUM(loss_count), SUM(draw_count) FROM positions")
            wins, losses, draws = cur.fetchone()
            # Mistakes corrected: moves with penalty_cp > 0
            cur.execute("SELECT COUNT(*) FROM move_experience WHERE penalty_cp > 0")
            mistakes_row = cur.fetchone()
            mistakes_corrected = mistakes_row[0] if mistakes_row else 0
            # Recent lessons: last 5 move_experience rows
            cur.execute(
                "SELECT fen, move_uci, penalty_cp, bonus_cp, visits FROM move_experience ORDER BY rowid DESC LIMIT 5"
            )
            recent_rows = cur.fetchall()
            recent_lessons = [
                {
                    "fen": row[0],
                    "move_uci": row[1],
                    "penalty_cp": row[2],
                    "bonus_cp": row[3],
                    "visits": row[4],
                }
                for row in recent_rows
            ]
            return {
                "positions_count": pos_count or 0,
                "positions_total_visits": pos_visits or 0,
                "moves_count": move_count or 0,
                "moves_total_visits": move_visits or 0,
                "games_won": wins or 0,
                "games_lost": losses or 0,
                "games_drawn": draws or 0,
                "mistakes_corrected": mistakes_corrected,
                "recent_lessons": recent_lessons,
            }

    def record_game(
        self,
        game_id: str,
        moves_uci: List[str],
        result: str,  # "win" | "loss" | "draw"
        engine,  # Engine instance with best_moves_multipv()
        duckish_color: str = "white",
        verbose_log=None,  # Optional callable(msg: str) for logging
    ) -> Dict[str, Any]:
        """Post-mortem turning-point analysis and experience recording.
        
        Analyzes the game trajectory, finds turning points, and records
        penalties/bonuses. Returns summary dict with turning points found.
        
        Args:
            game_id: Lichess game ID for logging
            moves_uci: List of UCI moves in order
            result: "win", "loss", or "draw"
            engine: Engine instance with best_moves_multipv() method
            verbose_log: Optional callable for logging (e.g., state.log)
        
        Returns:
            Dict with: turning_points, penalized_moves, boosted_moves, total_lessons
        """
        import chess
        
        log = verbose_log or (lambda msg: None)
        
        if len(moves_uci) < 8:
            return {"turning_points": 0, "penalized": 0, "boosted": 0, "skipped": True}
        
        # Only analyze losses and draws (skip wins)
        if result == "win":
            return {"turning_points": 0, "penalized": 0, "boosted": 0, "skipped": True}
        
        # Replay the game and evaluate each position
        board = chess.Board()
        evaluations: List[Dict[str, Any]] = []  # {ply, fen, move_uci, score_cp, best_uci}
        
        for i, uci in enumerate(moves_uci):
            fen_before = board.fen()
            
            # Skip opening book positions (first 8 plies)
            if i >= 8:
                try:
                    multipv = engine.best_moves_multipv([m for m in moves_uci[:i]], movetime=1200, multipv=1)
                    score_cp = multipv[0]["score_cp"] if multipv else 0
                    # Normalize score to Duckish's perspective
                    # UCI scores are from side-to-move's perspective
                    # If it's NOT Duckish's turn, negate
                    is_duckish_turn = (i % 2 == 0) if duckish_color == "white" else (i % 2 == 1)
                    if not is_duckish_turn:
                        score_cp = -score_cp
                    best_uci = multipv[0]["uci"] if multipv else uci
                except Exception:
                    score_cp = 0
                    best_uci = uci
            else:
                score_cp = 0
                best_uci = uci
            
            try:
                move = chess.Move.from_uci(uci)
                board.push(move)
            except ValueError:
                break
            
            evaluations.append({
                "ply": i,
                "fen": fen_before,
                "move_uci": uci,
                "score_cp": score_cp,
                "best_uci": best_uci,
            })
        
        if not evaluations:
            return {"turning_points": 0, "penalized": 0, "boosted": 0, "skipped": True}
        
        # Detect turning points
        turning_points = []
        
        if result == "loss":
            # Find first clearly losing position (score <= -150)
            losing_idx = None
            for i, ev in enumerate(evaluations):
                if ev["score_cp"] <= -150:
                    losing_idx = i
                    break
            
            if losing_idx is not None and losing_idx > 0:
                # Walk backward to find last move where score was >= -25
                for i in range(losing_idx - 1, -1, -1):
                    if evaluations[i]["score_cp"] >= -25:
                        # This is the root-cause turning point
                        turning_points.append({
                            "ply": evaluations[i]["ply"],
                            "fen": evaluations[i]["fen"],
                            "move_uci": evaluations[i]["move_uci"],
                            "score_before": evaluations[i]["score_cp"],
                            "score_after": evaluations[i + 1]["score_cp"] if i + 1 < len(evaluations) else evaluations[i]["score_cp"],
                            "type": "root_cause_loss",
                            "severity": abs(evaluations[i]["score_cp"]) + abs(evaluations[i + 1]["score_cp"] if i + 1 < len(evaluations) else 0),
                        })
                        break
            
            # Also detect any sharp drops (< -40 cp)
            for i in range(len(evaluations) - 1):
                drop = evaluations[i + 1]["score_cp"] - evaluations[i]["score_cp"]
                if drop <= -40:
                    turning_points.append({
                        "ply": evaluations[i]["ply"],
                        "fen": evaluations[i]["fen"],
                        "move_uci": evaluations[i]["move_uci"],
                        "score_before": evaluations[i]["score_cp"],
                        "score_after": evaluations[i + 1]["score_cp"],
                        "type": "sharp_drop",
                        "severity": abs(drop),
                    })
        
        elif result == "draw":
            # Find if Duckish held >= +80 cp and slipped to <= +20 cp
            had_advantage = False
            slipped_idx = None
            for i, ev in enumerate(evaluations):
                if ev["score_cp"] >= 80:
                    had_advantage = True
                if had_advantage and ev["score_cp"] <= 20:
                    slipped_idx = i
                    break
            
            if slipped_idx is not None and slipped_idx > 0:
                # Find where advantage slipped below +50
                for i in range(slipped_idx - 1, -1, -1):
                    if evaluations[i]["score_cp"] >= 50:
                        turning_points.append({
                            "ply": evaluations[i]["ply"],
                            "fen": evaluations[i]["fen"],
                            "move_uci": evaluations[i]["move_uci"],
                            "score_before": evaluations[i]["score_cp"],
                            "score_after": evaluations[i + 1]["score_cp"] if i + 1 < len(evaluations) else evaluations[i]["score_cp"],
                            "type": "missed_win",
                            "severity": abs(evaluations[i]["score_cp"]) + 50,
                        })
                        break
        
        # Deduplicate turning points (keep highest severity per ply)
        seen_plies = set()
        unique_turning_points = []
        for tp in sorted(turning_points, key=lambda x: x["severity"], reverse=True):
            if tp["ply"] not in seen_plies:
                seen_plies.add(tp["ply"])
                unique_turning_points.append(tp)
        
        # Apply penalties and bonuses
        penalized = 0
        boosted = 0
        
        for tp in unique_turning_points[:5]:  # Max 5 turning points per game
            # Re-search with MultiPV 3 at 1500ms to find true best move
            try:
                multipv = engine.best_moves_multipv([m for m in moves_uci[:tp["ply"]]], movetime=1500, multipv=3)
                if multipv and len(multipv) >= 2:
                    best_move = multipv[0]["uci"]
                    best_score = multipv[0]["score_cp"]
                    
                    # Calculate graduated penalty (35-100 cp based on severity and result)
                    if result == "loss":
                        base_penalty = min(100, 35 + tp["severity"] // 2)
                    else:  # draw
                        base_penalty = min(60, 25 + tp["severity"] // 3)
                    
                    # Record penalty on the move Duckish played
                    self.record_move(tp["fen"], tp["move_uci"], penalty_cp=float(base_penalty), bonus_cp=0.0)
                    penalized += 1
                    
                    # Record bonus on the best move found
                    bonus = 15.0 if result == "loss" else 25.0
                    if tp["move_uci"] != best_move:
                        self.record_move(tp["fen"], best_move, penalty_cp=0.0, bonus_cp=bonus)
                        boosted += 1
                    
                    # Also record the position with the game result
                    self.record_position(tp["fen"], float(best_score), result)
                    
                    log(f"[Experience] Turning point at ply {tp['ply']}: played {tp['move_uci']} (score {tp['score_before']}), best was {best_move} (score {best_score}) | Penalty -{base_penalty}cp, Bonus +{bonus}cp")
            except Exception as e:
                log(f"[Experience] Failed to analyze turning point at ply {tp['ply']}: {e}")
        
        # Record final position - use the actual final FEN after all moves
        if evaluations:
            final_board = chess.Board()
            for uci in moves_uci:
                try:
                    final_board.push_uci(uci)
                except ValueError:
                    break
            final_fen = final_board.fen()
            self.record_position(final_fen, None, result)
        
        # Get total lessons count
        total_lessons = 0
        try:
            with self._lock:
                cur = self._get_cursor()
                cur.execute("SELECT COUNT(*) FROM move_experience WHERE penalty_cp > 0 OR bonus_cp > 0")
                total_lessons = cur.fetchone()[0] or 0
        except Exception:
            pass
        
        return {
            "turning_points": len(unique_turning_points),
            "penalized": penalized,
            "boosted": boosted,
            "total_lessons": total_lessons,
            "skipped": False,
        }

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None


# Module-level singleton for convenience
_default_store: Optional[ExperienceStore] = None


def get_store(db_path: str = DEFAULT_DB_PATH) -> ExperienceStore:
    """Get or create the default ExperienceStore singleton."""
    global _default_store
    if _default_store is None:
        _default_store = ExperienceStore(db_path)
    return _default_store


if __name__ == "__main__":
    # Quick smoke test
    store = ExperienceStore(":memory:")
    store.record_position("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 15.0, None)
    store.record_move("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "e2e4", 0.0, 15.0)
    store.record_move("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "d2d4", 5.0, 0.0)
    pos = store.get_position("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    print("Position:", pos)
    move = store.get_move("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", "e2e4")
    print("Move:", move)
    cands = [
        {"uci": "e2e4", "score_cp": 20, "rank": 1},
        {"uci": "d2d4", "score_cp": 15, "rank": 2},
    ]
    adjusted = store.apply_adjustments("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", cands)
    print("Adjusted:", adjusted)
    print("Stats:", store.get_stats())
    print("PASS: ExperienceStore basic operations work")