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
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

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
        try:
            resp = self.client.stream_game(self.game_id)
            for line in resp:
                if not self._running:
                    break
                line = line.decode("utf-8").strip()
                if not line:
                    continue
                event = json.loads(line)
                self._handle_event(event)
        except Exception as e:
            print(f"[Game {self.game_id}] Stream error: {e}", flush=True)
        finally:
            self._running = False

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
                break
            self.moves_uci.append(move.uci())
            board.push(move)

    def _is_our_turn(self) -> bool:
        return self._board().turn == (chess.WHITE if self.color == "white" else chess.BLACK)

    def _make_move(self) -> None:
        if not self._running:
            return
        my_wtime = self.wtime if self.color == "white" else None
        my_btime = self.btime if self.color == "black" else None
        my_winc = self.winc if self.color == "white" else None
        my_binc = self.binc if self.color == "black" else None

        bestmove = self.engine.best_move(
            self.moves_uci,
            wtime=my_wtime,
            btime=my_btime,
            winc=my_winc,
            binc=my_binc,
        )
        if bestmove:
            # Convert to SAN for display and validate against the replay board
            # before sending; _board() replays moves_uci from the initial FEN.
            board = self._board()
            san = bestmove
            try:
                move = chess.Move.from_uci(bestmove)
                san = board.san(move)
                board.push(move)
            except ValueError:
                pass
            try:
                self.client.make_move(self.game_id, bestmove)
                self.moves_uci.append(bestmove)
                print(f"[Game {self.game_id}] Played {san} ({bestmove})", flush=True)
            except Exception as e:
                print(f"[Game {self.game_id}] Move failed: {e}", flush=True)

    def _handle_game_finish(self, event: Dict[str, Any]) -> None:
        winner = event.get("winner")  # "white", "black", or None for draw
        status = event.get("status")  # "mate", "resign", "timeout", etc.

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
        self._running = False

    def _record_experience(self, result: str) -> None:
        """Post-mortem learning: record the final position and penalize a losing blunder."""
        if len(self.moves_uci) < 2:
            return

        final_fen = self._board().fen()
        self.experience.record_position(final_fen, None, result)

        # For losses, penalize the last move we played.
        if result == "loss" and self.moves_uci:
            board = self._new_board()
            for uci in self.moves_uci[:-1]:
                try:
                    board.push_uci(uci)
                except ValueError:
                    break
            fen_before = board.fen()
            last_move = self.moves_uci[-1]
            self.experience.record_move(
                fen_before, last_move, penalty_cp=BLUNDER_THRESHOLD_CP, bonus_cp=0.0
            )

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
        self._auto_match_thread: Optional[threading.Thread] = None
        self._current_game_id: Optional[str] = None
        self._current_game_info: Optional[Dict[str, Any]] = None
        self._challenge_log: List[Dict[str, Any]] = []
        self._cooldown_until: Dict[str, float] = {}
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
                for line in resp:
                    if not self._running:
                        break
                    line = line.decode("utf-8").strip()
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

        challenge_id = challenge.get("id")
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
        game_id = game.get("id")
        color = game.get("color")  # "white" or "black"
        initial_fen = game.get("initialFen", "startpos")
        moves = game.get("state", {}).get("moves", "")
        wtime = game.get("state", {}).get("wtime")
        btime = game.get("state", {}).get("btime")
        winc = game.get("winc")
        binc = game.get("binc")
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

            del self.games[game_id]
            if self._current_game_id == game_id:
                self._current_game_id = None
                self._current_game_info = None

            if self._auto_match_active:
                self._set_phase("IDLE", "Game finished \u2014 searching next opponent...")
                threading.Timer(5.0, self._auto_match_next).start()
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
        if self._current_game_id:
            return  # Wait for current game to finish

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
                threading.Timer(30.0, self._auto_match_next).start()
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
                if challenge_id:
                    try:
                        self.client.cancel_challenge(challenge_id)
                    except Exception as e:
                        self._log_event(f"Failed to cancel challenge {challenge_id}: {e}")

            if self._auto_match_active:
                self._set_phase("IDLE", "Exhausted candidates, retrying in 30s")
                threading.Timer(30.0, self._auto_match_next).start()
        except Exception as e:
            self._set_phase("IDLE", f"Auto-match error: {e}")
            self._log_event(f"Auto-match error: {e}")
            if self._auto_match_active:
                threading.Timer(30.0, self._auto_match_next).start()

    # -- shutdown -------------------------------------------------------------

    def shutdown(self) -> None:
        self._running = False
        self._auto_match_active = False
        for handler in self.games.values():
            handler.stop()
        self.engine.shutdown()
        self.experience.close()
        self.dashboard.stop()