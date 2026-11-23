#!/usr/bin/env python3
"""Local engine-vs-engine match: analyzer policy vs bot policy on duckish.exe.

Plays GAMES games between two decision policies that both drive the SAME
duckish.exe binary (two independent engine subprocesses):

  * analyzer  -- bridge/server.py's recipe: MultiPV 3, 400ms ``go movetime``,
                 no opening book, raw bestmove, experience used for display only.
  * bot       -- bridge/lichess/runner.py's ``GameHandler._make_move`` driven
                 through ``object.__new__(GameHandler)`` with a stub client and a
                 real Engine: clock-scaled movetime, opening tiebreak, repetition
                 guard.

A tiny live viewer runs on http://0.0.0.0:5002 and auto-refreshes every second
(board, move list with policy/eval/time, running score, PGN).

Usage:
    python bridge/engine_match.py

Stop with Ctrl+C. The viewer stays up after the match so the final board,
score and PGNs can be inspected; the PGNs are also written to
``bridge/match_pgns/game_N.pgn`` plus ``match_summary.json``.
"""

import json
import os
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import chess

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

from lichess.config import ENGINE_PATH  # noqa: E402
from lichess.engine_client import Engine as BotEngine  # noqa: E402
from lichess import runner as bot_runner  # noqa: E402
from server import Engine as AnalyzerEngine  # noqa: E402

# ---- Match configuration ----------------------------------------------------

CLOCK_MS = 180000   # 3+2 blitz: 180s + 2s increment
INC_MS = 2000
GAMES = 3
VIEWER_PORT = 5002
VIEWER_HOST = "0.0.0.0"

ANALYZER_MOVETIME_MS = 400
ANALYZER_MULTIPV = 3

MAX_PLIES = 400     # safety cap so a pathological shuffle can't run forever

PGN_DIR = os.path.join(BRIDGE_DIR, "match_pgns")


# ---- Stub client for the bot policy ----------------------------------------

class StubClient:
    """Minimal Lichess client stand-in: records the UCI the handler submits."""

    def __init__(self):
        self.last_uci = None

    def make_move(self, game_id, uci, offering_draw=False):
        self.last_uci = uci
        return {"ok": True}


# ---- Shared match state (read by the viewer thread) -------------------------

class MatchState:
    """Holds the live match: finished games, the in-progress game, score."""

    def __init__(self, total_games=GAMES):
        self.lock = threading.RLock()
        self.total_games = total_games
        self.games = []                 # every game, in order (in-progress first-class)
        self.current = None             # the game currently being played / last played
        self.board_fen = chess.Board().fen()
        self.turn = "white"
        self.status = "starting"
        self.scores = {
            "analyzer_wins": 0,
            "bot_wins": 0,
            "draws": 0,
            "played": 0,
        }

    # -- lifecycle ----------------------------------------------------------

    def start_game(self, number, white_policy, black_policy):
        game = {
            "number": number,
            "white_policy": white_policy,
            "black_policy": black_policy,
            "moves": [],
            "clocks": {"white": CLOCK_MS, "black": CLOCK_MS},
            "result": None,
            "status": "in progress",
            "last_move_san": None,
        }
        with self.lock:
            self.games.append(game)
            self.current = game
            self.board_fen = chess.Board().fen()
            self.turn = "white"
            self.status = "game %d/%d in progress" % (number, self.total_games)
        return game

    def record_move(self, game, record, fen):
        with self.lock:
            game["moves"].append(record)
            game["clocks"]["white"] = record["clk_white"]
            game["clocks"]["black"] = record["clk_black"]
            game["last_move_san"] = record["san"]
            self.board_fen = fen
            self.turn = "white" if fen.split()[1] == "w" else "black"

    def finish_game(self, game, result, status):
        with self.lock:
            game["result"] = result
            game["status"] = status
            self.scores["played"] += 1
            if result == "1/2-1/2":
                self.scores["draws"] += 1
            else:
                # Work out which policy won, regardless of colour assignment.
                winner_policy = None
                if result == "1-0":
                    winner_policy = game["white_policy"]
                elif result == "0-1":
                    winner_policy = game["black_policy"]
                if winner_policy == "analyzer":
                    self.scores["analyzer_wins"] += 1
                elif winner_policy == "bot":
                    self.scores["bot_wins"] += 1
            if self.scores["played"] >= self.total_games:
                self.status = "match complete"
            else:
                self.status = "game %d/%d finished (%s)" % (
                    game["number"], self.total_games, result)

    # -- serialization ------------------------------------------------------

    @staticmethod
    def _movetext(moves):
        parts = []
        for i in range(0, len(moves), 2):
            num = i // 2 + 1
            text = "%d. %s" % (num, moves[i]["san"])
            if i + 1 < len(moves):
                text += " " + moves[i + 1]["san"]
            parts.append(text)
        return " ".join(parts)

    @staticmethod
    def _game_json(game, include_moves=True):
        if game is None:
            return None
        out = {
            "number": game["number"],
            "white_policy": game["white_policy"],
            "black_policy": game["black_policy"],
            "status": game["status"],
            "result": game["result"],
            "clocks": dict(game["clocks"]),
            "last_move_san": game["last_move_san"],
            "ply_count": len(game["moves"]),
            "movetext": MatchState._movetext(game["moves"]),
        }
        if include_moves:
            out["moves"] = [dict(m) for m in game["moves"]]
        return out

    def to_json(self):
        with self.lock:
            analyzer_pts = self.scores["analyzer_wins"] + 0.5 * self.scores["draws"]
            bot_pts = self.scores["bot_wins"] + 0.5 * self.scores["draws"]
            return {
                "status": self.status,
                "board_fen": self.board_fen,
                "turn": self.turn,
                "total_games": self.total_games,
                "scores": dict(self.scores),
                "points": {"analyzer": analyzer_pts, "bot": bot_pts},
                "games": [self._game_json(g, include_moves=False) for g in self.games],
                "current": self._game_json(self.current, include_moves=True),
            }


# ---- Policies ---------------------------------------------------------------

def _engine_new_game(engine):
    """Best-effort ucinewgame between games (both wrappers keep an idle process)."""
    send = getattr(engine, "_send", None)
    lock = getattr(engine, "lock", None)
    if send is None:
        return
    try:
        if lock is not None:
            with lock:
                send("ucinewgame")
                send("isready")
        else:
            send("ucinewgame")
    except Exception as exc:  # pragma: no cover - defensive
        print("[match] ucinewgame failed: %s" % (exc,), flush=True)


def make_bot_handler(color, moves_uci, wtime, btime, winc, binc, engine, client,
                     game_id="engine-match"):
    """Build a GameHandler without running __init__ (which starts a stream thread).

    Every attribute ``_make_move`` and the repetition block read is set here.
    """
    handler = object.__new__(bot_runner.GameHandler)
    handler.client = client
    handler.engine = engine
    handler.experience = None          # _make_move never touches it
    handler.game_id = game_id
    handler.color = color              # "white" | "black"
    handler.initial_fen = "startpos"
    handler.moves_uci = list(moves_uci)
    handler._position_counts = {}
    handler.wtime = wtime
    handler.btime = btime
    handler.winc = winc
    handler.binc = binc
    handler.variant = "standard"
    handler.rated = False
    handler.state = None               # _log() falls back to print()
    handler._running = True
    handler._finished = False
    handler._thread = None
    handler._rebuild_position_counts()  # repetition guard reads this
    return handler


def choose_bot(engine, board, color, wtime, btime, winc, binc):
    """Run the Lichess bot's _make_move; return (uci_or_None, handler)."""
    moves_uci = [m.uci() for m in board.move_stack]
    client = StubClient()
    handler = make_bot_handler(color, moves_uci, wtime, btime, winc, binc,
                               engine, client, game_id="em-%s" % (color,))
    handler._make_move()
    return client.last_uci, handler


def choose_analyzer(engine, board):
    """Run server.py's analyzer recipe; return (uci_or_None, result_dict)."""
    san_moves = []
    replay = chess.Board()
    for move in board.move_stack:
        san_moves.append(replay.san(move))
        replay.push(move)
    result = engine.analyze(san_moves, ANALYZER_MOVETIME_MS, ANALYZER_MULTIPV, None)
    best = (result or {}).get("bestmove") or {}
    return best.get("uci") or None, result


# ---- Match loop -------------------------------------------------------------

def _eval_cp(result, policy):
    if policy != "analyzer" or not result:
        return None
    mpv = result.get("multipv") or []
    if mpv:
        return mpv[0].get("score_cp")
    return None


def _policy_label(policy, color):
    return "%s (%s)" % (policy, color)


def play_game(state, number, white_policy, black_policy, bot_engine, analyzer_engine):
    game = state.start_game(number, white_policy, black_policy)
    board = chess.Board()
    clocks = {"white": CLOCK_MS, "black": CLOCK_MS}

    print("\n=== Game %d: analyzer=%s bot=%s ===" % (
        number,
        _policy_label(white_policy, "white"),
        _policy_label(black_policy, "black")), flush=True)
    _engine_new_game(bot_engine)
    _engine_new_game(analyzer_engine)

    result = None
    status = "in progress"
    try:
        while not board.is_game_over(claim_draw=True):
            if len(board.move_stack) >= MAX_PLIES:
                result = "1/2-1/2"
                status = "draw (move cap)"
                break

            side = "white" if board.turn else "black"
            policy = white_policy if side == "white" else black_policy
            wtime = clocks["white"]
            btime = clocks["black"]

            t0 = time.time()
            res = None
            if policy == "bot":
                uci, handler = choose_bot(bot_engine, board, side, wtime, btime,
                                          INC_MS, INC_MS)
            else:
                uci, res = choose_analyzer(analyzer_engine, board)
            think_ms = int((time.time() - t0) * 1000)

            move = None
            if uci:
                try:
                    candidate = chess.Move.from_uci(uci)
                    if candidate in board.legal_moves:
                        move = candidate
                    else:
                        print("[game %d] %s played illegal %s - falling back" % (
                            number, policy, uci), flush=True)
                except ValueError:
                    print("[game %d] %s returned malformed uci %r - falling back" % (
                        number, policy, uci), flush=True)
            if move is None:
                legal = list(board.legal_moves)
                if not legal:
                    break
                move = legal[0]
                print("[game %d] %s emergency fallback %s" % (
                    number, policy, move.uci()), flush=True)

            san = board.san(move)
            board.push(move)

            clocks[side] = clocks[side] - think_ms + INC_MS
            if clocks[side] <= 0:
                result = "0-1" if side == "white" else "1-0"
                status = "timeout (%s)" % side
                state.record_move(game, {
                    "san": san,
                    "uci": move.uci(),
                    "policy": policy,
                    "eval_cp": _eval_cp(res, policy),
                    "think_ms": think_ms,
                    "clk_white": max(0, clocks["white"]),
                    "clk_black": max(0, clocks["black"]),
                }, board.fen())
                break

            state.record_move(game, {
                "san": san,
                "uci": move.uci(),
                "policy": policy,
                "eval_cp": _eval_cp(res, policy),
                "think_ms": think_ms,
                "clk_white": max(0, clocks["white"]),
                "clk_black": max(0, clocks["black"]),
            }, board.fen())

            print("  %3d. %-13s %-6s eval=%-7s %5dms  W %s  B %s" % (
                len(game["moves"]),
                san,
                policy,
                _eval_cp(res, policy),
                think_ms,
                _fmt_clock(clocks["white"]),
                _fmt_clock(clocks["black"])), flush=True)

        if result is None:
            result = board.result(claim_draw=True)
            status = "finished"
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        import traceback
        traceback.print_exc()
        result = result or "1/2-1/2"
        status = "aborted (%s)" % (exc,)

    state.finish_game(game, result, status)
    print("--- Game %d result: %s (%s) | analyzer %s / bot %s" % (
        number, result, status,
        game["white_policy"], game["black_policy"]), flush=True)
    return game, result


def _fmt_clock(ms):
    ms = max(0, int(ms))
    return "%d:%02d" % (ms // 60000, (ms % 60000) // 1000)


# ---- PGN / summary output ---------------------------------------------------

def _pgn_headers(game):
    date = time.strftime("%Y.%m.%d")
    return [
        '[Event "Duckish local engine match"]',
        '[Site "local"]',
        '[Date "%s"]' % date,
        '[Round "%d"]' % game["number"],
        '[White "%s"]' % game["white_policy"].capitalize(),
        '[Black "%s"]' % game["black_policy"].capitalize(),
        '[Result "%s"]' % (game["result"] or "*"),
    ]


def _pgn_text(game):
    lines = list(_pgn_headers(game))
    lines.append("")
    body = MatchState._movetext(game["moves"])
    if body:
        body += " "
    body += game["result"] or "*"
    lines.append(body)
    return "\n".join(lines) + "\n"


def write_outputs(state):
    os.makedirs(PGN_DIR, exist_ok=True)
    for game in state.games:
        path = os.path.join(PGN_DIR, "game_%d.pgn" % game["number"])
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_pgn_text(game))
        print("wrote %s" % path, flush=True)

    summary = {
        "engine": ENGINE_PATH,
        "games": GAMES,
        "clock_ms": CLOCK_MS,
        "inc_ms": INC_MS,
        "analyzer_movetime_ms": ANALYZER_MOVETIME_MS,
        "analyzer_multipv": ANALYZER_MULTIPV,
        "scores": dict(state.scores),
        "results": [
            {
                "number": g["number"],
                "white_policy": g["white_policy"],
                "black_policy": g["black_policy"],
                "result": g["result"],
                "status": g["status"],
                "plies": len(g["moves"]),
            }
            for g in state.games
        ],
    }
    path = os.path.join(PGN_DIR, "match_summary.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(summary, fh, indent=2)
        fh.write("\n")
    print("wrote %s" % path, flush=True)


# ---- Live viewer ------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duckish - Analyzer vs Bot</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #171a21;
    --panel2: #1e222b;
    --line: #2a2f3a;
    --text: #e6e8ec;
    --dim: #8b93a1;
    --accent: #6ea8fe;
    --bot: #ffb86c;
    --win: #50fa7b;
    --loss: #ff6b6b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.45 "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 18px 18px 40px; }
  header { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; margin-bottom: 14px; }
  h1 { font-size: 19px; font-weight: 600; margin: 0; letter-spacing: .2px; }
  .score {
    font-size: 17px; font-weight: 600; padding: 4px 12px; border-radius: 8px;
    background: var(--panel2); border: 1px solid var(--line);
  }
  .score .a { color: var(--accent); }
  .score .b { color: var(--bot); }
  .score .sep { color: var(--dim); margin: 0 6px; }
  .status { color: var(--dim); margin-left: auto; font-size: 13px; }
  .main { display: grid; grid-template-columns: 460px 1fr; gap: 18px; align-items: start; }
  @media (max-width: 900px) { .main { grid-template-columns: 1fr; } }
  .boardwrap { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px; }
  .board {
    display: grid; grid-template-columns: repeat(8, 1fr);
    width: 100%; aspect-ratio: 1 / 1; border-radius: 6px; overflow: hidden;
    box-shadow: 0 6px 20px rgba(0,0,0,.45);
  }
  .sq { display: flex; align-items: center; justify-content: center; font-size: 40px; line-height: 1; user-select: none; }
  .light { background: #e8e2d0; }
  .dark  { background: #7d8a99; }
  .sq.last { box-shadow: inset 0 0 0 999px rgba(110,168,254,.28); }
  .wp { color: #ffffff; text-shadow: 0 1px 2px rgba(0,0,0,.55); }
  .bp { color: #14181f; text-shadow: 0 1px 1px rgba(255,255,255,.25); }
  .meta { display: flex; justify-content: space-between; gap: 12px; margin-top: 10px; color: var(--dim); font-size: 12.5px; }
  .meta b { color: var(--text); }
  .side { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
  .clocks { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .clock { background: var(--panel2); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
  .clock .lbl { color: var(--dim); font-size: 11.5px; text-transform: uppercase; letter-spacing: .5px; }
  .clock .val { font-size: 20px; font-variant-numeric: tabular-nums; font-weight: 600; }
  .clock.white .val { color: #ffffff; }
  .clock.black .val { color: #c3cad6; }
  .tablewrap { max-height: 330px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; }
  table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  th {
    position: sticky; top: 0; background: var(--panel2); color: var(--dim);
    text-align: left; font-weight: 600; font-size: 11.5px; letter-spacing: .4px;
    text-transform: uppercase; padding: 7px 8px; border-bottom: 1px solid var(--line);
  }
  td { padding: 5px 8px; border-bottom: 1px solid #22262f; font-size: 13px; }
  tbody tr:nth-child(even) { background: rgba(255,255,255,.02); }
  tbody tr:hover { background: rgba(110,168,254,.10); }
  .badge { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px; font-weight: 600; }
  .badge.analyzer { background: rgba(110,168,254,.16); color: var(--accent); border: 1px solid rgba(110,168,254,.35); }
  .badge.bot { background: rgba(255,184,108,.14); color: var(--bot); border: 1px solid rgba(255,184,108,.35); }
  .san { font-weight: 600; }
  .mv { color: var(--dim); }
  .pgn pre {
    margin: 6px 0 0; white-space: pre-wrap; word-break: break-word; color: #c9d1de;
    background: var(--panel2); border: 1px solid var(--line); border-radius: 8px;
    padding: 10px; font-size: 12.5px; max-height: 160px; overflow: auto;
  }
  .games { display: flex; gap: 8px; flex-wrap: wrap; }
  .gchip {
    border: 1px solid var(--line); background: var(--panel2); border-radius: 8px;
    padding: 6px 10px; font-size: 12px; color: var(--dim);
  }
  .gchip b { color: var(--text); }
  .gchip.live { border-color: var(--accent); }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--dim); margin-right: 6px; vertical-align: middle; }
  .dot.live { background: var(--win); box-shadow: 0 0 6px var(--win); }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .7px; color: var(--dim); margin: 0 0 8px; font-weight: 600; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Duckish engine match - analyzer vs bot</h1>
    <div class="score" id="score"><span class="a">Analyzer 0</span><span class="sep">-</span><span class="b">0 Bot</span></div>
    <div class="status" id="status">loading...</div>
  </header>
  <div class="main">
    <div>
      <div class="boardwrap">
        <div class="board" id="board"></div>
        <div class="meta">
          <div>Turn: <b id="turn">-</b> &middot; Last: <b id="last">-</b></div>
          <div id="games-chips"></div>
        </div>
      </div>
      <div class="card" style="margin-top:12px">
        <div class="clocks">
          <div class="clock white"><div class="lbl">White</div><div class="val" id="cw">--:--</div></div>
          <div class="clock black"><div class="lbl">Black</div><div class="val" id="cb">--:--</div></div>
        </div>
      </div>
    </div>
    <div class="side">
      <div class="card">
        <h2>Move list</h2>
        <div class="tablewrap" id="moves-wrap">
          <table>
            <thead>
              <tr>
                <th>#</th><th>Side</th><th>Policy</th><th>Move</th>
                <th>Eval</th><th>Think</th><th>White</th><th>Black</th>
              </tr>
            </thead>
            <tbody id="moves"></tbody>
          </table>
        </div>
      </div>
      <div class="card">
        <h2>PGN</h2>
        <pre id="pgn">waiting for the first move...</pre>
      </div>
    </div>
  </div>
</div>
<script>
var GLYPH_W = {k:'\u2654', q:'\u2655', r:'\u2656', b:'\u2657', n:'\u2658', p:'\u2659'};
var GLYPH_B = {k:'\u265A', q:'\u265B', r:'\u265C', b:'\u265D', n:'\u265E', p:'\u265F'};

function pieceHtml(ch, light) {
  var isWhite = ch === ch.toUpperCase();
  var g = isWhite ? GLYPH_W[ch.toLowerCase()] : GLYPH_B[ch.toLowerCase()];
  if (!g) return '';
  return '<span class="' + (isWhite ? 'wp' : 'bp') + '">' + g + '</span>';
}

function renderBoard(fen, lastMove) {
  var el = document.getElementById('board');
  var placement = (fen || '').split(' ')[0];
  var ranks = placement.split('/');
  var html = '';
  var lastFrom = lastMove ? lastMove.slice(0, 2) : null;
  var lastTo = lastMove ? lastMove.slice(2, 4) : null;
  for (var r = 0; r < 8; r++) {
    var rankStr = ranks[r] || '8';
    var fileIdx = 0;
    for (var i = 0; i < rankStr.length; i++) {
      var ch = rankStr.charAt(i);
      if (ch >= '1' && ch <= '8') {
        var n = parseInt(ch, 10);
        for (var k = 0; k < n; k++) {
          var sq = 'abcdefgh'.charAt(fileIdx) + (8 - r);
          html += squareHtml((r + fileIdx) % 2 === 0, sq, lastFrom, lastTo, '');
          fileIdx++;
        }
      } else {
        var sq2 = 'abcdefgh'.charAt(fileIdx) + (8 - r);
        html += squareHtml((r + fileIdx) % 2 === 0, sq2, lastFrom, lastTo, pieceHtml(ch, (r + fileIdx) % 2 === 0));
        fileIdx++;
      }
    }
  }
  el.innerHTML = html;
}

function squareHtml(isLight, sq, lastFrom, lastTo, content) {
  var cls = 'sq ' + (isLight ? 'light' : 'dark');
  if (sq === lastFrom || sq === lastTo) cls += ' last';
  return '<div class="' + cls + '">' + content + '</div>';
}

function fmtClock(ms) {
  if (ms === null || ms === undefined) return '--:--';
  ms = Math.max(0, Math.round(ms));
  var m = Math.floor(ms / 60000);
  var s = Math.floor((ms % 60000) / 1000);
  return m + ':' + (s < 10 ? '0' : '') + s;
}

function fmtEval(cp) {
  if (cp === null || cp === undefined) return '-';
  var v = cp / 100.0;
  return (v >= 0 ? '+' : '') + v.toFixed(2);
}

function renderMoves(moves) {
  var tb = document.getElementById('moves');
  if (!moves || !moves.length) {
    tb.innerHTML = '<tr><td colspan="8" class="mv">no moves yet</td></tr>';
    return;
  }
  var html = '';
  for (var i = 0; i < moves.length; i++) {
    var m = moves[i];
    var side = (i % 2 === 0) ? 'w' : 'b';
    html += '<tr>'
      + '<td class="mv">' + (i + 1) + '</td>'
      + '<td>' + side + '</td>'
      + '<td><span class="badge ' + m.policy + '">' + m.policy + '</span></td>'
      + '<td class="san">' + m.san + '</td>'
      + '<td>' + fmtEval(m.eval_cp) + '</td>'
      + '<td class="mv">' + m.think_ms + 'ms</td>'
      + '<td class="mv">' + fmtClock(m.clk_white) + '</td>'
      + '<td class="mv">' + fmtClock(m.clk_black) + '</td>'
      + '</tr>';
  }
  tb.innerHTML = html;
  var wrap = document.getElementById('moves-wrap');
  if (wrap) { wrap.scrollTop = wrap.scrollHeight; }
}

function renderChips(d) {
  var el = document.getElementById('games-chips');
  var html = '<div class="games">';
  var games = d.games || [];
  for (var i = 0; i < games.length; i++) {
    var g = games[i];
    var live = g.status === 'in progress';
    html += '<span class="gchip' + (live ? ' live' : '') + '">'
      + '<span class="dot' + (live ? ' live' : '') + '"></span>'
      + 'G' + g.number + ' <b>' + (g.result || (live ? '*' : '-')) + '</b></span>';
  }
  html += '</div>';
  el.innerHTML = html;
}

function pgnHeader(c, r) {
  var w = c.white_policy === 'analyzer' ? 'Analyzer' : 'Bot';
  var b = c.black_policy === 'analyzer' ? 'Analyzer' : 'Bot';
  var date = new Date().toISOString().slice(0, 10).replace(/-/g, '.');
  return '[Event "Duckish local engine match"]\n[Site "local"]\n[Date "' + date + '"]\n'
    + '[Round "' + c.number + '"]\n[White "' + w + '"]\n[Black "' + b + '"]\n'
    + '[Result "' + (c.result || '*') + '"]\n\n';
}

function renderPgn(d) {
  var c = d.current;
  var el = document.getElementById('pgn');
  if (!c) { el.textContent = 'waiting for the first move...'; return; }
  el.textContent = pgnHeader(c) + (c.movetext ? c.movetext + ' ' : '') + (c.result || '*');
}

function apply(d) {
  document.getElementById('score').innerHTML =
    '<span class="a">Analyzer ' + d.points.analyzer + '</span>'
    + '<span class="sep">-</span>'
    + '<span class="b">' + d.points.bot + ' Bot</span>';
  document.getElementById('status').textContent = d.status;
  var c = d.current;
  var lastMove = (c && c.moves && c.moves.length) ? c.moves[c.moves.length - 1].uci : null;
  renderBoard(d.board_fen, lastMove);
  document.getElementById('turn').textContent = d.turn;
  document.getElementById('last').textContent = (c && c.last_move_san) ? c.last_move_san : '-';
  document.getElementById('cw').textContent = (c && c.clocks) ? fmtClock(c.clocks.white) : '--:--';
  document.getElementById('cb').textContent = (c && c.clocks) ? fmtClock(c.clocks.black) : '--:--';
  renderChips(d);
  renderMoves(c ? c.moves : []);
  renderPgn(d);
}

function tick() {
  fetch('/api/state', {cache: 'no-store'})
    .then(function (r) { return r.json(); })
    .then(apply)
    .catch(function () {});
}

tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""


class ViewerHandler(BaseHTTPRequestHandler):
    state = None  # set by main() before serve_forever

    def _send(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path.split("?")[0] == "/api/state":
            body = json.dumps(self.state.to_json()).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
            return
        self._send(HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")

    def log_message(self, fmt, *args):  # silence the default stderr spam
        pass


# ---- Main -------------------------------------------------------------------

def main():
    print("Duckish engine-vs-engine match", flush=True)
    print("engine: %s" % ENGINE_PATH, flush=True)

    print("starting analyzer engine (MultiPV %d, %dms)..." % (
        ANALYZER_MULTIPV, ANALYZER_MOVETIME_MS), flush=True)
    analyzer_engine = AnalyzerEngine(ENGINE_PATH, ANALYZER_MULTIPV, None)

    print("starting bot engine...", flush=True)
    bot_engine = BotEngine(ENGINE_PATH)

    state = MatchState(GAMES)

    viewer = ThreadingHTTPServer((VIEWER_HOST, VIEWER_PORT), ViewerHandler)
    ViewerHandler.state = state
    viewer_thread = threading.Thread(target=viewer.serve_forever, daemon=True)
    viewer_thread.start()
    print("viewer: http://127.0.0.1:%d" % VIEWER_PORT, flush=True)

    try:
        for number in range(1, GAMES + 1):
            white_policy = "analyzer" if number % 2 == 1 else "bot"
            black_policy = "bot" if white_policy == "analyzer" else "analyzer"
            play_game(state, number, white_policy, black_policy,
                      bot_engine, analyzer_engine)

        write_outputs(state)

        s = state.scores
        print("\n=== MATCH COMPLETE ===", flush=True)
        print("Analyzer wins: %d | Bot wins: %d | Draws: %d" % (
            s["analyzer_wins"], s["bot_wins"], s["draws"]), flush=True)
        print("Score (points): Analyzer %.1f - %.1f Bot" % (
            s["analyzer_wins"] + 0.5 * s["draws"],
            s["bot_wins"] + 0.5 * s["draws"]), flush=True)
        print("Viewer still live on http://127.0.0.1:%d - Ctrl+C to stop." % VIEWER_PORT,
              flush=True)

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nshutting down...", flush=True)
    finally:
        viewer.shutdown()
        viewer.server_close()
        for engine in (analyzer_engine, bot_engine):
            try:
                engine.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    main()