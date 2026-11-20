#!/usr/bin/env python3
"""
Lichess bot orchestrator: event stream, challenge handling, game spawning,
auto-match, and per-game play. python-chess is used here only to convert the
stream's SAN move list into UCI coordinates for the engine.
"""
import os
import json
import time
import random
import threading
import urllib.error
from typing import Any, Dict, List, Optional

import chess

from .api import LichessClient
from .engine_client import Engine
from .dashboard import DashboardServer
from .config import (
    BotState,
    ENGINE_PATH,
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    BOOK_PATH,
    SUPPORTED_VARIANTS,
    SUPPORTED_SPEEDS,
)
from experience import (
    ExperienceStore,
    DEFAULT_DB_PATH,
    BLUNDER_THRESHOLD_CP,
)


# ---- Game Handler -----------------------------------------------------------

class GameHandler:
    """Handles a single bot game: streams moves, drives engine, records experience."""

    def __init__(
        self,
        client: LichessClient,
        engine: Engine,
        experience: ExperienceStore,
        game_id: str,
        color: str,  # "white" or "black"
        initial_fen: str = "startpos",
        moves: str = "",
        wtime: Optional[int] = None,
        btime: Optional[int] = None,
        winc: Optional[int] = None,
        binc: Optional[int] = None,
        variant: str = "standard",
        rated: bool = False,
        state: Optional[Any] = None,
    ):
        self.client = client
        self.engine = engine
        self.experience = experience
        self.game_id = game_id
        self.color = color
        self.initial_fen = initial_fen if initial_fen else "startpos"
        self.moves_uci: List[str] = []
        self.wtime = wtime
        self.btime = btime
        self.winc = winc
        self.binc = binc
        self.variant = variant
        self.rated = rated
        self.state = state
        self._running = True
        self._finished = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # -- logging --------------------------------------------------------------

    def _log(self, msg: str) -> None:
        if self.state:
            self.state.log(msg)
        else:
            print(msg, flush=True)

    # -- board helpers --------------------------------------------------------

    def _new_board(self) -> chess.Board:
        return chess.Board(self.initial_fen) if self.initial_fen != "startpos" else chess.Board()

    def _board(self) -> chess.Board:
        """Replay current moves_uci from the initial position."""
        board = self._new_board()
        for uci in self.moves_uci:
            try:
                board.push_uci(uci)
            except ValueError:
                break
        return board

    def _parse_initial_state(self, initial_fen: str, moves: str) -> None:
        self.initial_fen = initial_fen if initial_fen else "startpos"
        self.moves_uci = []
        board = self._new_board()
        if moves:
            for san in moves.split():
                try:
                    move = board.parse_san(san)
                    self.moves_uci.append(move.uci())
                    board.push(move)
                except ValueError:
                    break

    # -- stream loop ----------------------------------------------------------

    def _run(self) -> None:
        self._log(f"[Game {self.game_id}] Opening stream...")
        try:
            while self._running:
                try:
                    resp = self.client.stream_game(self.game_id)
                    for raw_line in resp.iter_lines(chunk_size=1, decode_unicode=True):
                        if not self._running:
                            break
                        if not raw_line:
                            continue
                        line = raw_line.strip()
                        if not line:
                            continue
                        event = json.loads(line)
                        typ = event.get("type", "?")
                        moves_str = event.get("moves", "") or event.get("state", {}).get("moves", "")
                        self._log(f"[Game {self.game_id}] Event: {typ} moves='{moves_str}'")
                        self._handle_event(event)
                    # Stream ended - if still running AND not finished, reconnect
                    if self._running and not self._finished:
                        self._log(f"[Game {self.game_id}] Stream ended, reconnecting...")
                        time.sleep(2)
                        continue
                    else:
                        break
                except Exception as e:
                    import traceback
                    self._log(f"[Game {self.game_id}] ERROR: {e}")
                    print(traceback.format_exc(), flush=True)
                    if self._running and not self._finished:
                        self._log(f"[Game {self.game_id}] Reconnecting in 3s...")
                        time.sleep(3)
                        continue
                    break
        finally:
            self._running = False
            self._finished = True

    def _handle_event(self, event: Dict[str, Any]) -> None:
        typ = event.get("type")
        if typ == "gameFull":
            state = event.get("state", {})
            self._parse_initial_state(event.get("initialFen", "startpos"), state.get("moves", ""))
            self.wtime = state.get("wtime")
            self.btime = state.get("btime")
            self.winc = state.get("winc")
            self.binc = state.get("binc")
            if self._is_our_turn():
                self._make_move()
        elif typ == "gameState":
            status = event.get("status")
            if isinstance(status, dict):
                status = status.get("name", "unknown")
            GAME_END_STATUSES = {"mate", "resign", "draw", "stalemate", "insufficient", "repetition", "fiftymoves", "timeout", "outoftime", "aborted", "cheat", "noStart", "unknownFinish"}
            if status and status in GAME_END_STATUSES:
                # Game is over - parse moves, then finish (don't make a move)
                moves = event.get("moves", "")
                if moves:
                    self._parse_incremental_moves(moves)
                self._handle_game_finish(event)
                return
            moves = event.get("moves", "")
            if moves:
                self._parse_incremental_moves(moves)
            self.wtime = event.get("wtime", self.wtime)
            self.btime = event.get("btime", self.btime)
            if self._is_our_turn():
                self._make_move()
        elif typ == "chatLine":
            pass  # ignore chat
        elif typ == "gameFinish":
            self._handle_game_finish(event)

    def _parse_incremental_moves(self, moves_str: str) -> None:
        """Append only the SAN moves we have not yet recorded.

        The event stream is cumulative, so we skip the first ``len(moves_uci)``
        tokens (already known) and convert the remainder to UCI.
        """
        if not moves_str:
            return
        board = self._board()
        for san in moves_str.split()[len(self.moves_uci):]:
            try:
                move = board.parse_san(san)
            except ValueError:
                self._log(f"[Game {self.game_id}] Failed to parse move: {san}")
                break
            self.moves_uci.append(move.uci())
            board.push(move)

    def _is_our_turn(self) -> bool:
        moves_count = len(self.moves_uci)
        if self.color == "white":
            return moves_count % 2 == 0
        else:
            return moves_count % 2 == 1

    def _make_move(self) -> None:
        if not self._running:
            return

        # Calculate dynamic movetime based on our remaining time
        our_time = self.wtime if self.color == "white" else self.btime
        our_inc = self.winc if self.color == "white" else self.binc
        if our_time and our_time > 0:
            inc = our_inc if our_inc and our_inc > 0 else 0
            movetime = max(150, min(int(our_time // 22 + inc * 0.8), int(our_time * 0.15)))
        else:
            movetime = 1000

        # Opening repertoire: soft bonus for preferred openings (first 8 plies only)
        opening_bonus = {}
        if len(self.moves_uci) < 8:
            if self.color == "white":
                # As White: prefer 1. Nf3, 2. c4/d4/g3
                if len(self.moves_uci) == 0:
                    opening_bonus = {"g1f3": 25}  # 1. Nf3
                elif len(self.moves_uci) == 2:
                    opening_bonus = {"c2c4": 20, "d2d4": 15, "g2g3": 15}  # 2. c4/d4/g3
                elif len(self.moves_uci) == 4:
                    opening_bonus = {"f1g2": 15, "d2d4": 10}  # 3. Bg2/d4
            else:
                # As Black: Sicilian against e4, Nf6 against d4/c4
                if len(self.moves_uci) == 1:
                    first_move = self.moves_uci[0] if self.moves_uci else ""
                    if first_move == "e2e4":
                        opening_bonus = {"c7c5": 25, "e7e5": 15}  # 1... c5/e5
                    elif first_move in ("d2d4", "c2c4"):
                        opening_bonus = {"g8f6": 25}  # 1... Nf6

        # Get MultiPV candidates, apply experience adjustments and opening bonus
        try:
            current_fen = self._board().fen()
            multipv = self.engine.best_moves_multipv(self.moves_uci, movetime=movetime, multipv=3)
            if multipv:
                adjusted = self.experience.apply_adjustments(current_fen, multipv)
                if adjusted:
                    # Apply opening bonus (only if within 30cp of top move)
                    top_score = adjusted[0].get("adjusted_score_cp", 0)
                    for move in adjusted:
                        uci = move.get("uci", "")
                        if uci in opening_bonus and move.get("adjusted_score_cp", 0) >= top_score - 30:
                            move["adjusted_score_cp"] = move.get("adjusted_score_cp", 0) + opening_bonus[uci]
                            move["learned"] = True
                    # Re-sort by adjusted score
                    adjusted.sort(key=lambda x: x.get("adjusted_score_cp", -999999), reverse=True)
                    for i, m in enumerate(adjusted):
                        m["rank"] = i + 1
                    bestmove = adjusted[0].get("uci", "")
                    if adjusted[0].get("learned"):
                        print(f"[Game {self.game_id}] Opening preference: {bestmove}", flush=True)
                else:
                    bestmove = multipv[0]["uci"]
            else:
                bestmove = self.engine.best_move(self.moves_uci, movetime=movetime)
        except Exception as e:
            self._log(f"[Game {self.game_id}] Engine error: {e}")
            try:
                bestmove = self.engine.best_move(self.moves_uci, movetime=movetime)
            except Exception as e2:
                self._log(f"[Game {self.game_id}] Engine failed completely: {e2}")
                return  # Don't kill the game, wait for next event

        if bestmove:
            self._log(f"[Game {self.game_id}] Engine chose {bestmove}")
            try:
                result = self.client.make_move(self.game_id, bestmove)
                if isinstance(result, dict) and result.get("error"):
                    self._log(f"[Game {self.game_id}] Move failed: {result['error']}")
                    return  # Don't kill, wait for next gameState
                board = self._board()
                try:
                    move = chess.Move.from_uci(bestmove)
                    if not board.is_legal(move):
                        self._log(f"[Game {self.game_id}] Illegal move: {bestmove}")
                        return
                    san = board.san(move)
                    board.push(move)
                except ValueError:
                    self._log(f"[Game {self.game_id}] Invalid move: {bestmove}")
                    return
                self.moves_uci.append(bestmove)
                self._log(f"[Game {self.game_id}] Move {bestmove} submitted")
            except Exception as e:
                import traceback
                self._log(f"[Game {self.game_id}] Move failed: {e}")
                print(traceback.format_exc(), flush=True)

    def _handle_game_finish(self, event: Dict[str, Any]) -> None:
        if self._finished:
            return
        self._finished = True
        self._running = False  # STOP THE GAME LOOP IMMEDIATELY

        winner = event.get("winner")
        status = event.get("status")
        # Lichess may send status as a dict {"id": 34, "name": "draw"} or a string
        if isinstance(status, dict):
            status = status.get("name", "unknown")

        # Skip non-result statuses
        NON_RESULT_STATUSES = {"aborted", "noStart", "cheat"}
        if status in NON_RESULT_STATUSES:
            print(f"[Game {self.game_id}] Skipping non-result status: {status}", flush=True)
            return

        # If no winner field (gameState event), infer from status
        if not winner and status:
            if status in ("draw", "stalemate", "insufficient", "repetition", "fiftymoves"):
                winner = None
            elif status == "mate":
                # The side NOT to move delivered mate
                board = self._board()
                winner = "white" if board.turn == "black" else "black"
            elif status == "resign":
                # The side to move resigned
                board = self._board()
                winner = "black" if board.turn == "white" else "white"
            elif status in ("timeout", "outoftime"):
                # The side that ran out of time lost
                board = self._board()
                winner = "black" if board.turn == "white" else "white"

        game_result = "draw"
        game_winner = "draw"
        if winner == "white":
            game_result = "win" if self.color == "white" else "loss"
            game_winner = "white"
        elif winner == "black":
            game_result = "win" if self.color == "black" else "loss"
            game_winner = "black"

        print(f"[Game {self.game_id}] Finished: {game_result} ({status})", flush=True)
        self._record_experience(game_result)

        # Auto-train on loss/draw games with >= 8 moves
        if game_result in ("loss", "draw") and len(self.moves_uci) >= 8:
            def _train():
                # Wait until no active games before training
                import time as _time
                for _ in range(60):  # wait up to 60s
                    if not self.engine.lock.locked():
                        break
                    _time.sleep(1)
                try:
                    summary = self.experience.record_game(
                        game_id=self.game_id,
                        moves_uci=self.moves_uci,
                        result=game_result,
                        engine=self.engine,
                        duckish_color=self.color,
                        verbose_log=lambda msg: print(msg, flush=True),
                    )
                    if not summary.get("skipped"):
                        print(f"[Experience] Audited Lichess game {self.game_id} ({game_result}): found {summary['turning_points']} turning points | Penalized {summary['penalized']} moves | Total DB lessons: {summary['total_lessons']}", flush=True)
                except Exception as e:
                    print(f"[Experience] Training failed for game {self.game_id}: {e}", flush=True)
            threading.Thread(target=_train, daemon=True).start()

    def _record_experience(self, result: str) -> None:
        """Post-mortem learning: record the final position and penalize a losing blunder."""
        if len(self.moves_uci) < 2:
            return

        final_fen = self._board().fen()
        self.experience.record_position(final_fen, None, result)

        # For losses, penalize the last move WE played.
        if result == "loss" and self.moves_uci:
            # Find the last move that was ours (odd index for black, even for white)
            our_color_idx = 0 if self.color == "white" else 1
            last_our_idx = None
            for i in range(len(self.moves_uci) - 1, -1, -1):
                if i % 2 == our_color_idx:
                    last_our_idx = i
                    break
            if last_our_idx is not None and last_our_idx > 0:
                board = self._new_board()
                for uci in self.moves_uci[:last_our_idx]:
                    try:
                        board.push_uci(uci)
                    except ValueError:
                        break
                fen_before = board.fen()
                self.experience.record_move(fen_before, self.moves_uci[last_our_idx], penalty_cp=BLUNDER_THRESHOLD_CP, bonus_cp=0.0)

    def stop(self) -> None:
        self._running = False


# ---- Bot Main Loop ----------------------------------------------------------

class LichessBot:
    """Main bot orchestrator: event stream, challenge handling, game spawning, auto-match, dashboard."""

    def __init__(self, token: str):
        # Shared dashboard state (data only; needed so boot logs flow through it).
        self.state = BotState()

        # Boot order: dashboard server FIRST - before engine spawn or Lichess connection.
        self.dashboard = DashboardServer(self)
        self.dashboard.start()
        self._log_event(f"Dashboard started at http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")

        self.client = LichessClient(token)
        self.engine = Engine(ENGINE_PATH)
        self.experience = ExperienceStore(DEFAULT_DB_PATH)
        self.games: Dict[str, GameHandler] = {}
        self._running = True
        self._username: Optional[str] = None
        self._blitz_rating: Optional[int] = None
        self._rapid_rating: Optional[int] = None
        self._session_wins = 0
        self._session_losses = 0
        self._session_draws = 0
        self._auto_match_active = False
        self._auto_match_rated = True
        self._accept_challenges = True
        self._auto_match_thread: Optional[threading.Thread] = None
        self._current_game_id: Optional[str] = None
        self._current_game_info: Optional[Dict[str, Any]] = None
        self._challenge_log: List[Dict[str, Any]] = []
        self._cooldown_until: Dict[str, float] = {}
        self._timers: List[threading.Timer] = []
        self._perf_book_loaded = os.path.isfile(BOOK_PATH)
        self._exp_book_loaded = os.path.isfile(DEFAULT_DB_PATH)

    # -- state helpers --------------------------------------------------------

    _STATE_KEYS = {"phase", "detail", "target_bot", "challenge_id", "active_game_url"}

    def _set_phase(self, phase: str, detail: str, **extra) -> None:
        with self.state.lock:
            self.state.phase = phase
            self.state.detail = detail
            for k, v in extra.items():
                if k in self._STATE_KEYS:
                    setattr(self.state, k, v)
        self._log_event(detail)

    def _log_event(self, msg: str) -> None:
        self.state.log(msg)

    def _log_challenge(self, msg: str, success: Optional[bool] = None) -> None:
        entry = {"time": time.time(), "msg": msg}
        if success is not None:
            entry["success"] = success
        self._challenge_log.append(entry)
        if len(self._challenge_log) > 100:
            self._challenge_log = self._challenge_log[-100:]

    # -- account / challenge --------------------------------------------------

    def upgrade_account(self) -> None:
        print("Upgrading account to BOT...", flush=True)
        print("WARNING: This permanently converts a 0-game account into a BOT account.", flush=True)
        try:
            result = self.client.upgrade_account()
            print(f"Upgrade result: {json.dumps(result, indent=2)}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"Upgrade failed: {e.code} - {e.read().decode('utf-8')}", flush=True)
        except Exception as e:
            print(f"Upgrade error: {e}", flush=True)

    def challenge_user(self, username: str, rated: bool = True) -> None:
        self._set_phase("CHALLENGING", f"Challenging {username} (3+2 {'Rated' if rated else 'Casual'} Standard)...", target_bot=username)
        try:
            result = self.client.challenge_user(username, clock_limit=180, clock_increment=2, rated=rated)
            challenge_id = result.get("challenge", {}).get("id") if isinstance(result.get("challenge"), dict) else None
            self._set_phase("WAITING_REPLY", f"Waiting for {username} to accept (challenge {challenge_id}, 10s timeout)...", challenge_id=challenge_id, target_bot=username)
            self._log_event(f"Challenge sent to {username}")
            self._log_challenge(f"Challenge sent to {username} ({'rated' if rated else 'casual'})", success=True)
        except urllib.error.HTTPError as e:
            err = e.read().decode('utf-8')
            self._set_phase("DECLINED", f"{username} declined ({err})", target_bot=username)
            self._log_event(f"Challenge to {username} failed: {err}")
            self._log_challenge(f"Failed: {err}", success=False)
        except Exception as e:
            self._set_phase("DECLINED", f"{username} error ({e})", target_bot=username)
            self._log_event(f"Challenge to {username} error: {e}")
            self._log_challenge(f"Error: {e}", success=False)

    # -- run loop -------------------------------------------------------------

    def run(self) -> None:
        self._log_event("Connecting to Lichess API...")
        self._fetch_account_info()

        if self._username:
            self._log_event(f"Connected as {self._username}!")

        self._set_phase("IDLE", "Idle \u2014 click Start Auto-Match or challenge a bot")
        self._log_event("Starting Lichess Bot event stream...")
        self._log_event(f"Engine: {self.engine.health()}")
        self._log_event(f"Dashboard: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")

        while self._running:
            try:
                resp = self.client.stream_events()
                for raw_line in resp.iter_lines(chunk_size=1, decode_unicode=True):
                    if not self._running:
                        break
                    if not raw_line:
                        continue
                    line = raw_line.strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    self._handle_event(event)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    self._log_event("Authentication failed: invalid token")
                    break
                self._log_event(f"HTTP error: {e.code}, reconnecting in 5s...")
                time.sleep(5)
            except Exception as e:
                if "401" in str(e):
                    self._log_event("Authentication failed: invalid token")
                    break
                self._log_event(f"Stream error: {e}, reconnecting in 5s...")
                time.sleep(5)

    def _fetch_account_info(self) -> None:
        try:
            info = self.client.get_account_info()
            self._username = info.get("username") or info.get("id")
            perfs = info.get("perfs", {})
            self._blitz_rating = perfs.get("blitz", {}).get("rating")
            self._rapid_rating = perfs.get("rapid", {}).get("rating")
        except Exception as e:
            self._log_event(f"Failed to fetch account info: {e}")

    def _handle_event(self, event: Dict[str, Any]) -> None:
        typ = event.get("type")
        if typ == "challenge":
            self._handle_challenge(event.get("challenge", {}))
        elif typ == "gameStart":
            self._handle_game_start(event.get("game", {}))
        elif typ == "gameFinish":
            self._handle_game_finish(event.get("game", {}))
        # Ignore other events

    def _handle_challenge(self, challenge: Dict[str, Any]) -> None:
        # Ignore our own outgoing challenges
        challenger_id = challenge.get("challenger", {}).get("id", "").lower()
        our_username = self._username.lower() if self._username else ""
        if our_username and challenger_id == our_username:
            return

        if not self._accept_challenges:
            self._log_event(f"  -> Declining: challenges disabled")
            try:
                self.client.decline_challenge(challenge.get("id"), "generic")
            except Exception:
                pass
            return

        challenge_id = challenge.get("id")

        # Decline if already in a game
        if self._current_game_id:
            self._log_event(f"  -> Declining: already in game {self._current_game_id}")
            try:
                self.client.decline_challenge(challenge_id, "later")
            except Exception:
                pass
            return

        variant = challenge.get("variant", {}).get("key", "standard")
        speed = challenge.get("speed", "unknown")
        time_control = challenge.get("timeControl", {})
        rated = challenge.get("rated", False)
        challenger = challenge.get("challenger", {}).get("name", "unknown")

        self._log_event(f"Challenge from {challenger}: {variant} {speed} rated={rated} tc={time_control}")

        # Only accept standard variant real-time games
        if variant not in SUPPORTED_VARIANTS:
            self._log_event(f"  -> Declining: unsupported variant {variant}")
            self.client.decline_challenge(challenge_id, "variant")
            return
        if speed not in SUPPORTED_SPEEDS:
            self._log_event(f"  -> Declining: unsupported speed {speed} (correspondence not supported)")
            self.client.decline_challenge(challenge_id, "timecontrol")
            return

        # Accept!
        self._log_event(f"  -> Accepting challenge from {challenger}")
        try:
            self.client.accept_challenge(challenge_id)
        except Exception as e:
            self._log_event(f"  -> Accept failed: {e}")

    def _handle_game_start(self, game: Dict[str, Any]) -> None:
        if self._current_game_id or self.games:
            return  # Ignore second gameStart

        game_id = game.get("id")
        color = game.get("color")  # "white" or "black"
        initial_fen = game.get("initialFen", "startpos")
        state = game.get("state", {})
        moves = state.get("moves", "")
        wtime = state.get("wtime")
        btime = state.get("btime")
        winc = state.get("winc")
        binc = state.get("binc")
        variant = game.get("variant", "standard")
        rated = game.get("rated", False)
        opponent = game.get("opponent", {}).get("username") or game.get("opponent", {}).get("id") or "unknown"
        opponent_rating = game.get("opponent", {}).get("rating")

        self._log_event(f"Game started: {game_id} as {color} vs {opponent} ({variant} rated={rated})")

        handler = GameHandler(
            self.client,
            self.engine,
            self.experience,
            game_id,
            color,
            initial_fen,
            moves,
            wtime,
            btime,
            winc,
            binc,
            variant,
            rated,
            self.state,
        )
        self.games[game_id] = handler
        self._current_game_id = game_id
        self._current_game_info = {
            "game_id": game_id,
            "opponent_name": opponent,
            "opponent_rating": opponent_rating,
            "our_color": color,
            "variant": variant,
            "rated": rated,
            "moves": [],
            "move_count": 0,
            "last_eval": None,
            "game_url": f"/{game_id}",
        }
        self._set_phase("IN_GAME", f"Playing vs {opponent} ({opponent_rating or '?'}) \u2014 Move 0 | Eval ?", active_game_url=f"https://lichess.org/{game_id}")
        # Patch handler to update our game info
        original_handle_event = handler._handle_event

        def wrapped_handle_event(event, _orig=original_handle_event, _h=handler):
            _orig(event)
            self._update_game_info(_h, event)

        handler._handle_event = wrapped_handle_event

    def _update_game_info(self, handler: GameHandler, event: Dict[str, Any]) -> None:
        if not self._current_game_info or handler.game_id != self._current_game_id:
            return
        typ = event.get("type")
        if typ == "gameState":
            moves_str = event.get("moves", "")
            if moves_str:
                move_list = moves_str.split()
                self._current_game_info["move_count"] = len(move_list)
                self._current_game_info["moves"] = [
                    {"san": san, "eval": "?"} for san in move_list
                ]
                opp_name = self._current_game_info["opponent_name"]
                opp_rating = self._current_game_info["opponent_rating"]
                self._set_phase("IN_GAME", f"Playing vs {opp_name} ({opp_rating or '?'}) \u2014 Move {len(move_list)} | Eval ?", active_game_url=f"https://lichess.org/{handler.game_id}")
        elif typ == "gameFull":
            moves_str = event.get("state", {}).get("moves", "")
            if moves_str:
                move_list = moves_str.split()
                self._current_game_info["move_count"] = len(move_list)
                self._current_game_info["moves"] = [
                    {"san": san, "eval": "?"} for san in move_list
                ]
                opp_name = self._current_game_info["opponent_name"]
                opp_rating = self._current_game_info["opponent_rating"]
                self._set_phase("IN_GAME", f"Playing vs {opp_name} ({opp_rating or '?'}) \u2014 Move {len(move_list)} | Eval ?", active_game_url=f"https://lichess.org/{handler.game_id}")

    def _handle_game_finish(self, game: Dict[str, Any]) -> None:
        game_id = game.get("id")
        winner = game.get("winner")  # "white", "black", or None for draw
        status = game.get("status")
        # Lichess may send status as a dict {"id": 34, "name": "draw"} or a string
        if isinstance(status, dict):
            status = status.get("name", "unknown")

        if game_id in self.games:
            handler = self.games[game_id]
            our_color = handler.color
            if winner == "white":
                result = "win" if our_color == "white" else "loss"
            elif winner == "black":
                result = "win" if our_color == "black" else "loss"
            else:
                result = "draw"

            if result == "win":
                self._session_wins += 1
            elif result == "loss":
                self._session_losses += 1
            else:
                self._session_draws += 1

            opponent_name = self._current_game_info['opponent_name'] if self._current_game_info else 'unknown'
            self._log_event(f"Game vs {opponent_name} finished: {result} ({status})")
            self._log_challenge(f"Game vs {opponent_name}: {result} ({status})", success=(result == "win"))

            # Call handler's game finish (records experience + auto-training)
            try:
                handler._handle_game_finish({"type": "gameFinish", "winner": winner, "status": status})
            except Exception as e:
                print(f"[Game {game_id}] Handler finish error: {e}", flush=True)

            del self.games[game_id]
            if self._current_game_id == game_id:
                self._current_game_id = None
                self._current_game_info = None

            if self._auto_match_active:
                self._set_phase("IDLE", "Game finished \u2014 searching next opponent...")
                t = threading.Timer(5.0, self._auto_match_next)
                self._timers.append(t)
                t.start()
            else:
                self._set_phase("IDLE", "Idle \u2014 click Start Auto-Match or challenge a bot")

    # -- dashboard status -----------------------------------------------------

    def get_dashboard_status(self) -> Dict[str, Any]:
        active_game = dict(self._current_game_info) if self._current_game_info else None
        state_copy = self.state.snapshot()
        return {
            "ok": True,
            "online": True,
            "phase": state_copy["phase"],
            "detail": state_copy["detail"],
            "auto_match": self._auto_match_active,
            "accept_challenges": self._accept_challenges,
            "logs": state_copy["logs"],
            "username": self._username,
            "blitz_rating": self._blitz_rating,
            "rapid_rating": self._rapid_rating,
            "session_wins": self._session_wins,
            "session_losses": self._session_losses,
            "session_draws": self._session_draws,
            "perf_book_loaded": self._perf_book_loaded,
            "exp_book_loaded": self._exp_book_loaded,
            "active_game": active_game,
            "challenge_log": self._challenge_log[-20:],  # Last 20 entries
            "state": state_copy,
        }

    # -- auto match -----------------------------------------------------------

    def start_auto_match(self) -> None:
        if self._auto_match_active:
            return
        self._auto_match_active = True
        self._auto_match_thread = threading.Thread(target=self._auto_match_loop, daemon=True)
        self._auto_match_thread.start()
        self._set_phase("FETCHING", "Auto-match started \u2014 fetching online bots...")
        self._log_challenge("Auto-match started", success=True)

    def stop_auto_match(self) -> None:
        self._auto_match_active = False
        self._set_phase("IDLE", "Auto-match stopped \u2014 click Start Auto-Match or challenge a bot")
        self._log_challenge("Auto-match stopped", success=False)

    def _auto_match_loop(self) -> None:
        self._auto_match_next()

    def _auto_match_next(self) -> None:
        if not self._auto_match_active:
            return
        # Clean up dead game handlers
        for gid in list(self.games.keys()):
            h = self.games.get(gid)
            if h and not h._thread.is_alive():
                del self.games[gid]
                if self._current_game_id == gid:
                    self._current_game_id = None
                    self._current_game_info = None
        # Don't start a new game if one is already in progress
        if self._current_game_id:
            return  # Wait for current game to finish
        # Also check if any game handlers are still running
        if self.games:
            return  # Games still in progress

        try:
            self._set_phase("FETCHING", "Fetching online bots from Lichess...")
            bots = self.client.get_online_bots(own_username=self._username)
            self._log_event(f"Fetched {len(bots)} candidate bots")
            candidates = []
            for bot in bots:
                name = bot.get("username") or bot.get("id")
                if name == self._username:
                    continue
                perfs = bot.get("perfs", {})
                blitz_games = perfs.get("blitz", {}).get("games", 0)
                if blitz_games == 0:
                    continue
                if bot.get("tosViolation") or bot.get("disabled"):
                    continue
                if name in self._cooldown_until and time.time() < self._cooldown_until[name]:
                    continue
                candidates.append(name)

            if not candidates:
                self._set_phase("IDLE", "No suitable bots found, retrying in 30s")
                t = threading.Timer(30.0, self._auto_match_next)
                self._timers.append(t)
                t.start()
                return

            random.shuffle(candidates)
            for opponent in candidates:
                if not self._auto_match_active:
                    return
                if self._current_game_id:
                    info = self._current_game_info or {}
                    self._set_phase("IN_GAME", f"Playing vs {info.get('opponent_name', opponent)} ({info.get('opponent_rating') or '?'}) \u2014 Move 0 | Eval ?", active_game_url=f"https://lichess.org/{self._current_game_id}")
                    return  # Game started
                self._set_phase("CHALLENGING", f"Challenging {opponent} (3+2 Rated Standard)...", target_bot=opponent)
                try:
                    result = self.client.challenge_user(opponent, clock_limit=180, clock_increment=2, rated=self._auto_match_rated)
                    challenge_id = result.get("challenge", {}).get("id") if isinstance(result.get("challenge"), dict) else None
                    self._set_phase("WAITING_REPLY", f"Waiting for {opponent} to accept (challenge {challenge_id}, 10s timeout)...", challenge_id=challenge_id, target_bot=opponent)
                    self._log_event(f"Challenge sent to {opponent}")
                except urllib.error.HTTPError as e:
                    err = e.read().decode('utf-8')
                    self._set_phase("DECLINED", f"{opponent} declined ({err}) \u2014 trying next bot...", target_bot=opponent)
                    self._log_event(f"Challenge to {opponent} failed: {err}")
                    self._cooldown_until[opponent] = time.time() + (3600 if "bot.vsBot.day" in err else 600)
                    time.sleep(2)
                    continue
                except Exception as e:
                    self._set_phase("DECLINED", f"{opponent} error ({e}) \u2014 trying next bot...", target_bot=opponent)
                    self._log_event(f"Challenge to {opponent} error: {e}")
                    self._cooldown_until[opponent] = time.time() + 600
                    time.sleep(2)
                    continue
                # Wait ~10s for acceptance
                time.sleep(10)
                if self._current_game_id:
                    return  # Game started
                self._set_phase("DECLINED", f"{opponent} declined (timeout) \u2014 trying next bot...", target_bot=opponent)
                self._log_event(f"{opponent} did not accept in time")
                self._cooldown_until[opponent] = time.time() + 120  # 2min cooldown on timeout
                if challenge_id:
                    try:
                        self.client.cancel_challenge(challenge_id)
                    except Exception as e:
                        self._log_event(f"Failed to cancel challenge {challenge_id}: {e}")

            if self._auto_match_active:
                self._set_phase("IDLE", "Exhausted candidates, retrying in 30s")
                t = threading.Timer(30.0, self._auto_match_next)
                self._timers.append(t)
                t.start()
        except Exception as e:
            self._set_phase("IDLE", f"Auto-match error: {e}")
            self._log_event(f"Auto-match error: {e}")
            if self._auto_match_active:
                t = threading.Timer(30.0, self._auto_match_next)
                self._timers.append(t)
                t.start()

    # -- shutdown -------------------------------------------------------------

    def shutdown(self) -> None:
        self._running = False
        self._auto_match_active = False
        for t in self._timers:
            t.cancel()
        self._timers.clear()
        for handler in self.games.values():
            handler.stop()
        self.engine.shutdown()
        self.experience.close()
        self.dashboard.stop()