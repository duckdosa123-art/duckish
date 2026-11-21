#!/usr/bin/env python3
"""
Dashboard HTTP server for the Duckish Lichess bot.
Pure stdlib (json, os, sqlite3, threading, http.server, typing).
"""
import json
import os
import sqlite3
import threading
import http.server
from urllib.parse import urlparse, parse_qs
from typing import Any, Dict, Optional

from .config import DASHBOARD_HOST, DASHBOARD_PORT, REPO_ROOT


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    allow_reuse_address = True

    def __init__(self, bot_ref, *args, **kwargs):
        self.bot_ref = bot_ref
        super().__init__(*args, **kwargs)

    def _write_response(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        """Write response, ignoring client disconnect errors (WinError 10053, BrokenPipeError)."""
        try:
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            # Client disconnected (browser refresh, tab close) - ignore silently
            pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._write_response(200, {"Content-Type": "text/html; charset=utf-8"}, DASHBOARD_HTML.encode("utf-8"))
        elif self.path == "/api/status":
            status = self.bot_ref.get_dashboard_status()
            self._write_response(200, {"Content-Type": "application/json"}, json.dumps(status).encode("utf-8"))
        elif self.path == "/experience" or self.path.startswith("/experience?"):
            self._write_response(200, {"Content-Type": "text/html; charset=utf-8"}, EXPERIENCE_HTML.encode("utf-8"))
        elif self.path == "/api/experience" or self.path.startswith("/api/experience?"):
            # Query experience.db and return JSON
            db_path = os.path.join(REPO_ROOT, "bridge", "experience.db")
            if not os.path.isfile(db_path):
                self._write_response(200, {"Content-Type": "application/json"}, json.dumps({"summary": {}, "lessons": []}).encode("utf-8"))
                return

            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            filter_type = params.get("filter", ["all"])[0]
            try:
                limit = int(params.get("limit", ["100"])[0])
            except (TypeError, ValueError):
                limit = 100
            limit = max(1, min(100000, limit))

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row

            try:
                # Summary
                pos = conn.execute("SELECT COUNT(*), SUM(visits), SUM(win_count), SUM(loss_count), SUM(draw_count) FROM positions").fetchone()
                mistakes = conn.execute("SELECT COUNT(*) FROM move_experience WHERE penalty_cp > 0").fetchone()[0]
                boosts = conn.execute("SELECT COUNT(*) FROM move_experience WHERE bonus_cp > 0").fetchone()[0]

                # Lessons
                query = "SELECT p.fen, p.visits, p.win_count, p.loss_count, p.draw_count, p.deep_score_cp, m.move_uci, m.penalty_cp, m.bonus_cp, m.visits as move_visits FROM positions p LEFT JOIN move_experience m ON p.fen = m.fen"
                if filter_type == "penalties":
                    query += " WHERE m.penalty_cp > 0"
                elif filter_type == "bonuses":
                    query += " WHERE m.bonus_cp > 0"
                query += f" LIMIT {limit}"

                rows = conn.execute(query).fetchall()
            finally:
                conn.close()

            lessons = []
            for row in rows:
                fen = row["fen"]
                turn = "White" if " w " in f" {fen} " else "Black"
                explanation = ""
                penalty_cp = row["penalty_cp"]
                bonus_cp = row["bonus_cp"]
                if penalty_cp and penalty_cp > 0:
                    explanation = f"Penalized -{penalty_cp:.0f} cp after blunder"
                elif bonus_cp and bonus_cp > 0:
                    explanation = f"Boosted +{bonus_cp:.0f} cp as stronger alternative"
                lessons.append({
                    "fen": fen,
                    "turn": turn,
                    "visits": row["visits"],
                    "wins": row["win_count"],
                    "draws": row["draw_count"],
                    "losses": row["loss_count"],
                    "deep_score_cp": row["deep_score_cp"],
                    "moves": [{
                        "move_uci": row["move_uci"],
                        "penalty_cp": row["penalty_cp"],
                        "bonus_cp": row["bonus_cp"],
                        "visits": row["move_visits"],
                        "explanation": explanation,
                    }] if row["move_uci"] else [],
                })

            response = {
                "summary": {
                    "total_positions": pos[0] or 0,
                    "total_visits": pos[1] or 0,
                    "wins": pos[2] or 0,
                    "losses": pos[3] or 0,
                    "draws": pos[4] or 0,
                    "mistakes_penalized": mistakes,
                    "moves_boosted": boosts,
                },
                "lessons": lessons,
            }
            self._write_response(200, {"Content-Type": "application/json"}, json.dumps(response).encode("utf-8"))
        else:
            self._write_response(404, {}, b"")

    def do_POST(self):
        if self.path == "/api/auto-match":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length else ""
            config = None
            if body:
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        config = parsed
                except Exception:
                    config = None
            self.bot_ref.start_auto_match(config)
            self._write_response(200, {"Content-Type": "application/json"}, b'{"ok":true}')
        elif self.path == "/api/auto-match/stop":
            self.bot_ref.stop_auto_match()
            self._write_response(200, {"Content-Type": "application/json"}, b'{"ok":true}')
        elif self.path == "/api/challenge":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body)
                username = data.get("username", "").strip()
                rated = data.get("rated", True)
                limit_min = data.get("limit_min")
                increment = data.get("increment")
                clock_limit = int(limit_min) * 60 if limit_min is not None else None
                clock_increment = int(increment) if increment is not None else None
                if username:
                    self.bot_ref.challenge_user(username, rated=rated, clock_limit=clock_limit, clock_increment=clock_increment)
                    self._write_response(200, {"Content-Type": "application/json"}, b'{"ok":true}')
                else:
                    self._write_response(400, {"Content-Type": "application/json"}, b'{"error":"username required"}')
            except Exception as e:
                self._write_response(400, {"Content-Type": "application/json"}, json.dumps({"error": str(e)}).encode("utf-8"))
        elif self.path == "/api/toggle-challenges":
            self.bot_ref._accept_challenges = not self.bot_ref._accept_challenges
            self._write_response(200, {"Content-Type": "application/json"}, json.dumps({"ok": True, "accept_challenges": self.bot_ref._accept_challenges}).encode("utf-8"))
        elif self.path == "/api/engine/restart":
            try:
                self.bot_ref.engine.respawn()
                self._write_response(200, {"Content-Type": "application/json"}, b'{"ok":true}')
            except Exception as e:
                self._write_response(500, {"Content-Type": "application/json"}, json.dumps({"error": str(e)}).encode("utf-8"))
        else:
            self._write_response(404, {}, b"")

    def log_message(self, format, *args):
        pass  # suppress default logging


class DashboardServer:
    def __init__(self, bot_ref, host: str = DASHBOARD_HOST, port: int = DASHBOARD_PORT):
        self.bot_ref = bot_ref
        self.host = host
        self.port = port
        self.server: Optional[http.server.ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self):
        handler = lambda *args, **kwargs: DashboardHandler(self.bot_ref, *args, **kwargs)
        self.server = http.server.ThreadingHTTPServer((self.host, self.port), handler)
        self.server.allow_reuse_address = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


# ---- Dashboard HTML ---------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Duckish Bot Dashboard</title>
<style>
:root { --bg:#0d1117; --panel:#161b22; --panel2:#21262d; --fg:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --accent2:#a371f7; --ok:#3fb950; --warn:#d29922; --err:#f85149; --border:#30363d; }
*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
body{background:var(--bg);color:var(--fg);min-height:100vh;display:flex;flex-direction:column}
header{background:var(--panel);border-bottom:1px solid var(--border);padding:16px 24px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:10}
h1{font-size:1.25rem;font-weight:600;letter-spacing:-0.02em}
.status-badge{padding:4px 10px;border-radius:999px;font-size:0.75rem;font-weight:600;text-transform:uppercase}
.status-online{background:rgba(63,185,80,.15);color:var(--ok);border:1px solid var(--ok)}
.status-offline{background:rgba(248,81,73,.15);color:var(--err);border:1px solid var(--err)}
.status-auto{background:rgba(210,153,34,.15);color:var(--warn);border:1px solid var(--warn)}
.action-box{background:var(--panel2);border:1px solid var(--border);border-radius:12px;padding:16px 20px;margin:16px 24px 0;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.action-phase{font-size:0.75rem;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;padding:4px 10px;border-radius:999px;background:rgba(88,166,255,.15);color:var(--accent);border:1px solid var(--accent);min-width:110px;text-align:center}
.action-detail{flex:1;font-size:0.9rem;color:var(--fg);min-width:280px}
.watch-btn{display:inline-block;padding:8px 16px;background:var(--accent);color:#0d1117;border-radius:8px;font-weight:600;text-decoration:none;transition:opacity .2s}
.watch-btn:hover{opacity:.85}
main{flex:1;padding:24px;display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;max-width:1400px;margin:0 auto;width:100%}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:20px;display:flex;flex-direction:column}
.card h2{font-size:0.875rem;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;color:var(--muted);margin-bottom:16px;border-bottom:1px solid var(--border);padding-bottom:12px}
.stat-row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border)}
.stat-row:last-child{border-bottom:none}
.stat-label{color:var(--muted);font-size:0.875rem}
.stat-value{font-weight:600;font-size:0.875rem;text-align:right}
.game-card{flex:1;display:flex;flex-direction:column}
.game-info{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}
.info-item{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:12px}
.info-label{font-size:0.7rem;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
.info-value{font-weight:600;font-size:0.95rem}
.move-list{flex:1;overflow-y:auto;font-family:monospace;font-size:0.8rem;line-height:1.8;color:var(--muted);background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:12px;max-height:200px}
.move-list .eval{color:var(--accent);font-weight:600}
.watch-link{display:inline-block;margin-top:12px;padding:10px 16px;background:var(--accent);color:#0d1117;border-radius:8px;font-weight:600;text-decoration:none;text-align:center;transition:opacity .2s}
.watch-link:hover{opacity:.85}
.btn{padding:10px 16px;border-radius:8px;border:1px solid var(--border);background:var(--panel2);color:var(--fg);font-weight:600;font-size:0.875rem;cursor:pointer;transition:all .15s}
.btn:hover{border-color:var(--accent);background:var(--panel)}
.btn-primary{background:var(--accent);color:#0d1117;border-color:var(--accent)}
.btn-primary:hover{filter:brightness(1.1)}
.btn-danger{background:rgba(248,81,73,.15);color:var(--err);border-color:var(--err)}
.btn-danger:hover{background:rgba(248,81,73,.25)}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.control-group{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.toggle{display:flex;align-items:center;gap:6px}
.toggle input{width:16px;height:16px;accent-color:var(--accent)}
.log-container{flex:1;overflow-y:auto;font-family:monospace;font-size:0.75rem;line-height:1.6;background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:12px;max-height:300px}
.log-entry{padding:4px 0;border-bottom:1px solid var(--border);display:flex;gap:8px}
.log-time{color:var(--muted);white-space:nowrap;min-width:70px}
.log-msg{color:var(--fg);flex:1}
.log-success{color:var(--ok)}
.log-error{color:var(--err)}
.log-warn{color:var(--warn)}
.manual-challenge{display:flex;gap:8px;margin-top:12px}
.manual-challenge input{flex:1;padding:8px 12px;border:1px solid var(--border);background:var(--panel);color:var(--fg);border-radius:6px;font-size:0.875rem}
.manual-challenge input:focus{outline:none;border-color:var(--accent)}
.no-game{text-align:center;color:var(--muted);padding:40px 20px;font-size:0.9rem}
.refresh-indicator{height:3px;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:0 0 12px 12px;transform-origin:left;transform:scaleX(0);animation:pulse 1.5s linear infinite}
@keyframes pulse{0%{transform:scaleX(0)}100%{transform:scaleX(1)}}
.rating-badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:0.7rem;font-weight:600;background:var(--panel2);border:1px solid var(--border)}
.console-card{grid-column:1/-1}
.console-toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.console-title{font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;color:var(--muted)}
.console-clear{padding:4px 10px;font-size:0.7rem;border:1px solid var(--border);background:var(--panel);color:var(--fg);border-radius:4px;cursor:pointer}
.console-clear:hover{border-color:var(--accent)}
.console-output{flex:1;overflow-y:auto;font-family:monospace;font-size:0.7rem;line-height:1.5;background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:12px;max-height:350px;min-height:200px}
.console-line{padding:2px 0;display:flex;gap:8px;white-space:pre-wrap;word-break:break-word}
.console-time{color:var(--muted);white-space:nowrap;min-width:70px}
.console-msg{color:var(--fg);flex:1}
.console-phase-IDLE{color:var(--muted)}
.console-phase-FETCHING{color:var(--accent)}
.console-phase-CHALLENGING{color:var(--warn)}
.console-phase-WAITING_REPLY{color:var(--accent2)}
.console-phase-DECLINED{color:var(--err)}
.console-phase-IN_GAME{color:var(--ok)}
</style>
</head>
<body>
<header>
<h1>\U0001F986 Duckish Bot Dashboard</h1>
<div style="display:flex;align-items:center;gap:16px;">
<a class="watch-btn" href="/experience" target="_blank" rel="noopener">View Experience Book (/experience)</a>
<span id="statusBadge" class="status-badge status-offline">Offline</span>
</div>
</header>
<div class="action-box" id="actionBox">
<span class="action-phase" id="actionPhase">IDLE</span>
<span class="action-detail" id="actionDetail">Idle \u2014 click Start Auto-Match or challenge a bot</span>
<a class="watch-btn" id="watchBtn" href="#" target="_blank" style="display:none;">Watch Live</a>
</div>
<main>
<div class="card">
<h2>Bot Status</h2>
<div id="botStatus">
<div class="stat-row"><span class="stat-label">Username</span><span class="stat-value" id="username">-</span></div>
<div class="stat-row"><span class="stat-label">Blitz Rating</span><span class="stat-value" id="blitzRating">-</span></div>
<div class="stat-row"><span class="stat-label">Rapid Rating</span><span class="stat-value" id="rapidRating">-</span></div>
<div class="stat-row"><span class="stat-label">Session W/L/D</span><span class="stat-value" id="sessionRecord">0/0/0</span></div>
<div class="stat-row"><span class="stat-label">Performance.bin</span><span class="stat-value" id="perfBook">-</span></div>
<div class="stat-row"><span class="stat-label">Experience Book</span><span class="stat-value" id="expBook">-</span></div>
<div class="stat-row"><span class="stat-label">Tablebases</span><span class="stat-value" id="tbStatus">-</span></div>
</div>
</div>
<div class="card game-card">
<h2>Active Game</h2>
<div id="activeGame">
<div class="no-game">No active game</div>
</div>
</div>
<div class="card">
<h2>Auto-Match Controls</h2>
<div class="controls">
<div class="control-group">
<button id="btnStart" class="btn btn-primary">Start Auto-Match</button>
<button id="btnStop" class="btn btn-danger" disabled>Stop Auto-Match</button>
<button id="btnRestartEngine" class="btn">Restart Engine</button>
</div>
<div class="control-group toggle">
<input type="checkbox" id="toggleRated" checked>
<label for="toggleRated">Rated</label>
</div>
<div class="control-group toggle">
<input type="checkbox" id="toggleChallenges" checked>
<label for="toggleChallenges">Accept Challenges</label>
</div>
<div class="control-group toggle">
<input type="checkbox" id="cfgSpeedBullet">
<label for="cfgSpeedBullet">Bullet</label>
</div>
<div class="control-group toggle">
<input type="checkbox" id="cfgSpeedBlitz" checked>
<label for="cfgSpeedBlitz">Blitz</label>
</div>
<div class="control-group toggle">
<input type="checkbox" id="cfgSpeedRapid" checked>
<label for="cfgSpeedRapid">Rapid</label>
</div>
</div>
<div class="manual-challenge">
<input type="text" id="manualUser" placeholder="Username to challenge...">
<button id="btnManual" class="btn">Challenge</button>
</div>
<div class="log-container" id="challengeLog"></div>
</div>
<div class="card">
<h2>Match Settings</h2>
<div class="controls">
<div class="control-group">
<label for="cfgSpeed">Speed</label>
<select id="cfgSpeed">
<option value="bullet">Bullet</option>
<option value="blitz" selected>Blitz</option>
<option value="rapid">Rapid</option>
</select>
</div>
<div class="control-group">
<label for="cfgLimit">Limit (min)</label>
<input type="number" id="cfgLimit" min="1" max="60" value="3">
</div>
<div class="control-group">
<label for="cfgInc">Increment (sec)</label>
<input type="number" id="cfgInc" min="0" max="60" value="2">
</div>
<div class="control-group">
<label for="cfgEloMin">ELO min</label>
<input type="number" id="cfgEloMin" min="0" max="4000" value="0">
</div>
<div class="control-group">
<label for="cfgEloMax">ELO max</label>
<input type="number" id="cfgEloMax" min="0" max="4000" value="0">
</div>
</div>
</div>
<div class="card console-card">
<h2>Live Activity Console</h2>
<div class="console-toolbar">
<span class="console-title">Real-time event stream (last 50)</span>
<button class="console-clear" id="consoleClear">Clear</button>
</div>
<div class="console-output" id="consoleOutput"></div>
</div>
</main>
<div class="refresh-indicator" id="refreshBar"></div>
<script>
const $ = (s) => document.querySelector(s);
const log = (msg, cls='') => { const el=document.createElement('div'); el.className='log-entry'+(cls?' '+cls:''); el.innerHTML=`<span class="log-time">${new Date().toLocaleTimeString()}</span><span class="log-msg">${msg}</span>`; $('#challengeLog').prepend(el); };
let autoMatchActive = false;
function matchConfig(){ return {speed:$('#cfgSpeed').value, limit_min:parseInt($('#cfgLimit').value||3), increment:parseInt($('#cfgInc').value||2), rated:$('#toggleRated').checked, elo_min:parseInt($('#cfgEloMin').value||0), elo_max:parseInt($('#cfgEloMax').value||0), allowed_speeds:allowedSpeeds()}; }
function allowedSpeeds(){
  var a=[]; if($('#cfgSpeedBullet').checked)a.push('bullet'); if($('#cfgSpeedBlitz').checked)a.push('blitz'); if($('#cfgSpeedRapid').checked)a.push('rapid');
  return a.length ? a.join(',') : 'blitz';
}
async function fetchStatus(){
try{const r=await fetch('/api/status');if(!r.ok)throw 0;const d=await r.json();updateUI(d);}catch(e){try{log('Status fetch error: '+(e && e.message ? e.message : String(e)));}catch(_){}$('#statusBadge').textContent='Error';$('#statusBadge').className='status-badge status-offline';}
}
function updateUI(d){
const ok = d.ok === true;
const online = d.online === true;
const autoMatch = d.auto_match === true;
$('#statusBadge').textContent=autoMatch?'Auto-Match':(online?'Online':'Offline');
$('#statusBadge').className='status-badge '+(autoMatch?'status-auto':(online?'status-online':'status-offline'));
$('#username').textContent=d.username||'-';
$('#blitzRating').innerHTML=d.blitz_rating?`${d.blitz_rating}<span class="rating-badge">blitz</span>`:'-';
$('#rapidRating').innerHTML=d.rapid_rating?`${d.rapid_rating}<span class="rating-badge">rapid</span>`:'-';
$('#sessionRecord').textContent=`${d.session_wins||0}/${d.session_losses||0}/${d.session_draws||0}`;
$('#perfBook').textContent=d.perf_book_loaded?'Loaded':'Missing';
$('#expBook').textContent=d.exp_book_loaded?'Loaded':'Missing';
$('#tbStatus').textContent=d.tb_loaded ? 'Loaded' : 'Missing';
$('#btnStart').disabled=autoMatch;$('#btnStop').disabled=!autoMatch;
autoMatchActive=autoMatch;
$('#toggleChallenges').checked = d.accept_challenges !== false;
if(d.match_config){
 const mc=d.match_config;
 const active=document.activeElement;
 if(active!==$('#cfgSpeed')) $('#cfgSpeed').value=mc.speed||'blitz';
 if(active!==$('#cfgLimit')) $('#cfgLimit').value=mc.limit_min!=null?mc.limit_min:3;
 if(active!==$('#cfgInc')) $('#cfgInc').value=mc.increment!=null?mc.increment:2;
 if(active!==$('#cfgEloMin')) $('#cfgEloMin').value=mc.elo_min!=null?mc.elo_min:0;
 if(active!==$('#cfgEloMax')) $('#cfgEloMax').value=mc.elo_max!=null?mc.elo_max:0;
 const allowed = (d.match_config && d.match_config.allowed_speeds ? d.match_config.allowed_speeds : 'blitz,rapid').split(',');
 $('#cfgSpeedBullet').checked = allowed.indexOf('bullet') >= 0;
 $('#cfgSpeedBlitz').checked = allowed.indexOf('blitz') >= 0;
 $('#cfgSpeedRapid').checked = allowed.indexOf('rapid') >= 0;
}
const g=d.active_game;
if(g){
$('#activeGame').innerHTML=`
<div class="game-info">
<div class="info-item"><div class="info-label">Opponent</div><div class="info-value">${g.opponent_name} <span class="rating-badge">${g.opponent_rating||'-'}</span></div></div>
<div class="info-item"><div class="info-label">Our Color</div><div class="info-value">${g.our_color}</div></div>
<div class="info-item"><div class="info-label">Moves</div><div class="info-value">${g.move_count}</div></div>
<div class="info-item"><div class="info-label">Last Eval</div><div class="info-value"><span class="eval">${g.last_eval||'?'}</span></div></div>
</div>
<div class="move-list" id="moveList"></div>
<a class="watch-link" href="https://lichess.org${g.game_url}" target="_blank">Watch Live on Lichess</a>
`;
if(g.moves){
const ml=$('#moveList');
ml.innerHTML=g.moves.map((m,i)=>`<div>${i+1}. ${m.san} <span class="eval">(${m.eval||'?'})</span></div>`).join('');
}
}else{
$('#activeGame').innerHTML='<div class="no-game">No active game</div>';
}
// State machine display - use top-level phase/detail/logs
const phase = d.phase || 'IDLE';
const detail = d.detail || '';
const logs = d.logs || [];
$('#actionPhase').textContent=phase;
$('#actionPhase').className='action-phase console-phase-'+phase;
$('#actionDetail').textContent=detail;
const watchBtn=$('#watchBtn');
const activeGameUrl = d.state?.active_game_url;
if(phase==='IN_GAME' && activeGameUrl){
watchBtn.href=activeGameUrl;
watchBtn.style.display='inline-block';
}else{
watchBtn.style.display='none';
}
// Console logs
const consoleOut=$('#consoleOutput');
if(logs && logs.length){
consoleOut.innerHTML=logs.map(e=>`<div class="console-line"><span class="console-time">${new Date(e.time*1000).toLocaleTimeString()}</span><span class="console-msg console-phase-${phase}">${e.msg}</span></div>`).join('');
consoleOut.scrollTop=consoleOut.scrollHeight;
}
}
$('#btnStart').onclick=async()=>{await fetch('/api/auto-match',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(matchConfig())});log('Auto-match started','log-success');fetchStatus();};
$('#btnStop').onclick=async()=>{await fetch('/api/auto-match/stop',{method:'POST'});log('Auto-match stopped','log-warn');fetchStatus();};
$('#btnRestartEngine').onclick=async()=>{try{await fetch('/api/engine/restart',{method:'POST'});log('Engine restarted','log-success');}catch(e){log('Engine restart failed','log-error');}fetchStatus();};
$('#btnManual').onclick=async()=>{const u=$('#manualUser').value.trim();if(!u)return;$('#manualUser').value='';log(`Challenging ${u}...`);try{const r=await fetch('/api/challenge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,rated:$('#toggleRated').checked,limit_min:parseInt($('#cfgLimit').value||3),increment:parseInt($('#cfgInc').value||2)})});const d=await r.json();if(r.ok)log(`Challenge sent to ${u}`,'log-success');else log(`Failed: ${d.error||'unknown'}`,'log-error');}catch(e){log('Request failed','log-error');}fetchStatus();};
$('#consoleClear').onclick=()=>{$('#consoleOutput').innerHTML='';};
$('#toggleChallenges').onchange = async () => {
  await fetch('/api/toggle-challenges', {method: 'POST'});
  fetchStatus();
};
fetchStatus();setInterval(fetchStatus,1000);
</script>
</body>
</html>
"""


# ---- Experience Book HTML ---------------------------------------------------

EXPERIENCE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Duckish Experience Book</title>
<style>
:root { --bg:#0d1117; --panel:#161b22; --panel2:#21262d; --fg:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --ok:#3fb950; --warn:#d29922; --err:#f85149; --border:#30363d; }
*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
body{background:var(--bg);color:var(--fg);min-height:100vh;padding:24px}
h1{font-size:1.3rem;font-weight:600;letter-spacing:-0.02em;margin-bottom:4px}
.subtitle{color:var(--muted);font-size:0.85rem;margin-bottom:20px}
.banner{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px;max-width:1200px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px}
.stat-label{font-size:0.7rem;text-transform:uppercase;letter-spacing:0.05em;color:var(--muted);margin-bottom:6px}
.stat-value{font-size:1.5rem;font-weight:700}
.stat-value.poss{color:var(--accent)}
.stat-value.mist{color:var(--err)}
.stat-value.boost{color:var(--ok)}
.stat-value.wdl{color:var(--fg);font-size:1.15rem}
.layout{display:grid;grid-template-columns:minmax(280px,360px) 1fr;gap:20px;max-width:1200px;align-items:start}
.board-panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px;position:sticky;top:16px}
.board-panel h2{font-size:0.8rem;text-transform:uppercase;letter-spacing:0.05em;color:var(--muted);margin-bottom:12px}
.board-wrap{position:relative;width:100%;aspect-ratio:1/1;border:2px solid var(--border);border-radius:6px;overflow:hidden}
.board-grid{display:grid;grid-template-columns:repeat(8,1fr);grid-template-rows:repeat(8,1fr);width:100%;height:100%}
.sq{display:flex;align-items:center;justify-content:center;font-size:min(6.2vw,38px);line-height:1;user-select:none}
.sq.light{background:#ebecd0}
.sq.dark{background:#779556}
.sq.white{color:#fff;text-shadow:0 0 2px #000,0 1px 2px rgba(0,0,0,.6)}
.sq.black{color:#000;text-shadow:0 1px 1px rgba(255,255,255,.3)}
#arrowLayer{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none}
.board-hint{margin-top:10px;color:var(--muted);font-size:0.75rem;line-height:1.5}
.board-hint b{color:var(--fg)}
.legend{margin-top:8px;font-size:0.72rem}
.legend span{margin-right:12px}
.legend .red{color:var(--err)}
.legend .green{color:var(--ok)}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;align-items:center}
.btn{padding:7px 14px;border-radius:8px;border:1px solid var(--border);background:var(--panel2);color:var(--fg);font-weight:600;font-size:0.82rem;cursor:pointer;transition:all .15s}
.btn:hover{border-color:var(--accent)}
.btn.active{background:var(--accent);color:#0d1117;border-color:var(--accent)}
#searchBox{flex:1;min-width:180px;padding:8px 12px;border:1px solid var(--border);background:var(--panel);color:var(--fg);border-radius:8px;font-size:0.85rem}
#searchBox:focus{outline:none;border-color:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:0.8rem;background:var(--panel);border-radius:12px;overflow:hidden}
thead th{text-align:left;padding:10px 12px;background:var(--panel2);color:var(--muted);font-size:0.72rem;text-transform:uppercase;letter-spacing:0.04em;border-bottom:1px solid var(--border);white-space:nowrap}
tbody td{padding:9px 12px;border-bottom:1px solid var(--border);vertical-align:top}
tbody tr{cursor:pointer;transition:background .12s}
tbody tr:hover{background:var(--panel2)}
tbody td.fen{font-family:ui-monospace,monospace;font-size:0.72rem;color:var(--muted);max-width:220px;overflow-wrap:anywhere}
td.pen{color:var(--err);font-weight:600}
td.bon{color:var(--ok);font-weight:600}
.empty{text-align:center;color:var(--muted);padding:24px}
.table-scroll{overflow-x:auto}
@media (max-width:820px){ .layout{grid-template-columns:1fr} .board-panel{position:static} .sq{font-size:min(9vw,40px)} }
</style>
</head>
<body>
<h1>\U0001F9E0 Duckish Experience Book</h1>
<div class="subtitle">Lessons learned from your games. Click any row to view the position and its learned moves.</div>
<div class="banner">
<div class="stat"><div class="stat-label">Total Positions Learned</div><div class="stat-value poss" id="sumPositions">0</div></div>
<div class="stat"><div class="stat-label">Mistakes Penalized</div><div class="stat-value mist" id="sumMistakes">0</div></div>
<div class="stat"><div class="stat-label">Stronger Moves Boosted</div><div class="stat-value boost" id="sumBoosts">0</div></div>
<div class="stat"><div class="stat-label">Record (W / D / L)</div><div class="stat-value wdl" id="sumWDL">0 / 0 / 0</div></div>
</div>
<div class="layout">
<div class="board-panel">
<h2>Position Viewer</h2>
<div class="board-wrap">
<div class="board-grid" id="boardGrid"></div>
<svg id="arrowLayer" viewBox="0 0 8 8" preserveAspectRatio="none"></svg>
</div>
<div class="legend"><span class="red">\u25CF Penalized move</span><span class="green">\u25CF Boosted move</span></div>
<div class="board-hint" id="boardHint">Select a lesson from the table to view it here.</div>
</div>
<div class="table-scroll">
<div class="controls">
<button class="btn active" data-filter="all">All Lessons</button>
<button class="btn" data-filter="penalties">Mistakes Penalized</button>
<button class="btn" data-filter="bonuses">Boosted Moves</button>
<input type="text" id="searchBox" placeholder="Search FEN or move (e.g. e2e4)...">
</div>
<table>
<thead><tr>
<th>FEN</th><th>Turn</th><th>Visits</th><th>W/D/L</th><th>Move</th><th>Pen</th><th>Bonus</th><th>Explanation</th>
</tr></thead>
<tbody id="lessonsBody"><tr><td colspan="8" class="empty">Loading...</td></tr></tbody>
</table>
</div>
</div>
<script>
const $ = (s) => document.querySelector(s);
const FILES = 'abcdefgh';
const PIECES = {
  'P':'\\u2659','N':'\\u2658','B':'\\u2657','R':'\\u2656','Q':'\\u2655','K':'\\u2654',
  'p':'\\u265F','n':'\\u265E','b':'\\u265D','r':'\\u265C','q':'\\u265B','k':'\\u265A'
};
let allLessons = [];
let currentFilter = 'all';

function parseFen(fen){
  const placement = (fen || '').split(' ')[0];
  const rows = placement.split('/');
  const board = {};
  for(let i=0;i<8;i++){
    const rank = 8 - i;
    let file = 0;
    for(const ch of (rows[i]||'')){
      if(ch >= '1' && ch <= '8'){ file += parseInt(ch,10); }
      else { board[FILES[file] + rank] = ch; file++; }
    }
  }
  return board;
}

function renderBoard(fen){
  const board = parseFen(fen);
  const grid = document.getElementById('boardGrid');
  grid.innerHTML = '';
  for(let r=8;r>=1;r--){
    for(let f=0;f<8;f++){
      const sq = FILES[f] + r;
      const cell = document.createElement('div');
      const isLight = ((f + r) % 2 === 0);
      cell.className = 'sq ' + (isLight ? 'light' : 'dark');
      const p = board[sq];
      if(p){
        cell.textContent = PIECES[p] || '';
        cell.className += (p === p.toUpperCase()) ? ' white' : ' black';
      }
      grid.appendChild(cell);
    }
  }
}

function center(sq){
  const f = FILES.indexOf(sq[0]);
  const r = parseInt(sq[1],10);
  return [f + 0.5, 8.5 - r];
}

function drawArrows(moves){
  const svg = document.getElementById('arrowLayer');
  svg.innerHTML = '<defs>'
    + '<marker id="ahRed" markerWidth="0.55" markerHeight="0.55" refX="0.45" refY="0.275" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L0.55,0.275 L0,0.55 z" fill="#f85149"/></marker>'
    + '<marker id="ahGreen" markerWidth="0.55" markerHeight="0.55" refX="0.45" refY="0.275" orient="auto" markerUnits="userSpaceOnUse"><path d="M0,0 L0.55,0.275 L0,0.55 z" fill="#3fb950"/></marker>'
    + '</defs>';
  (moves || []).forEach(function(m){
    const uci = m.move_uci || '';
    if(uci.length < 4) return;
    const fromSq = uci.slice(0,2);
    const toSq = uci.slice(2,4);
    const a = center(fromSq);
    const b = center(toSq);
    let color, marker;
    if(m.penalty_cp && m.penalty_cp > 0){ color = '#f85149'; marker = 'url(#ahRed)'; }
    else if(m.bonus_cp && m.bonus_cp > 0){ color = '#3fb950'; marker = 'url(#ahGreen)'; }
    else return;
    const line = document.createElementNS('http://www.w3.org/2000/svg','line');
    line.setAttribute('x1', a[0]); line.setAttribute('y1', a[1]);
    line.setAttribute('x2', b[0]); line.setAttribute('y2', b[1]);
    line.setAttribute('stroke', color);
    line.setAttribute('stroke-width', '0.14');
    line.setAttribute('stroke-linecap', 'round');
    line.setAttribute('marker-end', marker);
    svg.appendChild(line);
  });
}

function fmt(cp){ return (cp === null || cp === undefined) ? '-' : Number(cp).toFixed(0); }

function renderLessons(){
  const q = (document.getElementById('searchBox').value || '').toLowerCase().trim();
  const filtered = allLessons.filter(function(l){
    if(!q) return true;
    const hay = (l.fen + ' ' + (l.moves || []).map(function(m){return m.move_uci;}).join(' ')).toLowerCase();
    return hay.indexOf(q) !== -1;
  });
  const tbody = document.getElementById('lessonsBody');
  if(!filtered.length){
    tbody.innerHTML = '<tr><td colspan="8" class="empty">No lessons found</td></tr>';
    return;
  }
  tbody.innerHTML = filtered.map(function(l){
    const m = (l.moves && l.moves[0]) || {};
    return '<tr>'
      + '<td class="fen">' + l.fen + '</td>'
      + '<td>' + l.turn + '</td>'
      + '<td>' + (l.visits || 0) + '</td>'
      + '<td>' + (l.wins||0) + '/' + (l.draws||0) + '/' + (l.losses||0) + '</td>'
      + '<td>' + (m.move_uci || '-') + '</td>'
      + '<td class="pen">' + (m.penalty_cp ? fmt(m.penalty_cp) : '-') + '</td>'
      + '<td class="bon">' + (m.bonus_cp ? fmt(m.bonus_cp) : '-') + '</td>'
      + '<td>' + (m.explanation || '-') + '</td>'
      + '</tr>';
  }).join('');
  const rows = tbody.querySelectorAll('tr');
  filtered.forEach(function(l, i){
    rows[i].onclick = function(){
      renderBoard(l.fen);
      drawArrows(l.moves);
      const hint = document.getElementById('boardHint');
      const m = (l.moves && l.moves[0]) || {};
      hint.innerHTML = '<b>' + l.fen + '</b><br>Turn: ' + l.turn
        + ' | Visits: ' + (l.visits||0)
        + ' | Move: ' + (m.move_uci||'-')
        + (m.explanation ? '<br>' + m.explanation : '');
    };
  });
}

async function load(filter){
  currentFilter = filter || 'all';
  try{
    const r = await fetch('/api/experience?filter=' + currentFilter + '&limit=100');
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    const s = d.summary || {};
    document.getElementById('sumPositions').textContent = s.total_positions || 0;
    document.getElementById('sumMistakes').textContent = s.mistakes_penalized || 0;
    document.getElementById('sumBoosts').textContent = s.moves_boosted || 0;
    document.getElementById('sumWDL').textContent = (s.wins||0) + ' / ' + (s.draws||0) + ' / ' + (s.losses||0);
    allLessons = d.lessons || [];
    renderLessons();
    if(allLessons.length){
      renderBoard(allLessons[0].fen);
      drawArrows(allLessons[0].moves);
    }
  }catch(e){
    document.getElementById('lessonsBody').innerHTML = '<tr><td colspan="8" class="empty">Failed to load: ' + (e && e.message ? e.message : e) + '</td></tr>';
  }
}

document.querySelectorAll('.btn[data-filter]').forEach(function(btn){
  btn.onclick = function(){
    document.querySelectorAll('.btn[data-filter]').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
    load(btn.getAttribute('data-filter'));
  };
});
document.getElementById('searchBox').addEventListener('input', renderLessons);
load('all');
</script>
</body>
</html>
"""
