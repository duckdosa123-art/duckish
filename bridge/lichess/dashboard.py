#!/usr/bin/env python3
"""
Dashboard HTTP server for the Duckish Lichess bot.
Pure stdlib (json, threading, http.server, typing).
"""
import json
import threading
import http.server
from typing import Any, Dict, Optional

from .config import DASHBOARD_HOST, DASHBOARD_PORT


class DashboardHandler(http.server.BaseHTTPRequestHandler):
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
        else:
            self._write_response(404, {}, b"")

    def do_POST(self):
        if self.path == "/api/auto-match":
            self.bot_ref.start_auto_match()
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
                if username:
                    self.bot_ref.challenge_user(username, rated=rated)
                    self._write_response(200, {"Content-Type": "application/json"}, b'{"ok":true}')
                else:
                    self._write_response(400, {"Content-Type": "application/json"}, b'{"error":"username required"}')
            except Exception as e:
                self._write_response(400, {"Content-Type": "application/json"}, json.dumps({"error": str(e)}).encode("utf-8"))
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
<span id="statusBadge" class="status-badge status-offline">Offline</span>
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
</div>
<div class="control-group toggle">
<input type="checkbox" id="toggleRated" checked>
<label for="toggleRated">Rated</label>
</div>
</div>
<div class="manual-challenge">
<input type="text" id="manualUser" placeholder="Username to challenge...">
<button id="btnManual" class="btn">Challenge</button>
</div>
<div class="log-container" id="challengeLog"></div>
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
async function fetchStatus(){
try{const r=await fetch('/api/status');if(!r.ok)throw 0;const d=await r.json();updateUI(d);}catch{$('#statusBadge').textContent='Error';$('#statusBadge').className='status-badge status-offline';}
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
$('#btnStart').disabled=autoMatch;$('#btnStop').disabled=!autoMatch;
autoMatchActive=autoMatch;
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
}
$('#btnStart').onclick=async()=>{await fetch('/api/auto-match',{method:'POST'});log('Auto-match started','log-success');fetchStatus();};
$('#btnStop').onclick=async()=>{await fetch('/api/auto-match/stop',{method:'POST'});log('Auto-match stopped','log-warn');fetchStatus();};
$('#btnManual').onclick=async()=>{const u=$('#manualUser').value.trim();if(!u)return;$('#manualUser').value='';log(`Challenging ${u}...`);try{const r=await fetch('/api/challenge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,rated:$('#toggleRated').checked})});const d=await r.json();if(r.ok)log(`Challenge sent to ${u}`,'log-success');else log(`Failed: ${d.error||'unknown'}`,'log-error');}catch(e){log('Request failed','log-error');}fetchStatus();};
$('#consoleClear').onclick=()=>{$('#consoleOutput').innerHTML='';};
fetchStatus();setInterval(fetchStatus,1000);
</script>
</body>
</html>
"""