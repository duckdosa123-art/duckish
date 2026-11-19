// ==UserScript==
// @name         Duckish Advisor
// @namespace    duckish
// @version      1.0.0
// @description  Offline Duckish engine analysis overlay for Chess.com and Lichess.org (self-study / post-game review / bot practice).
// @author       Duck
// @match        https://chess.com/*
// @match        https://*.chess.com/*
// @match        https://lichess.org/*
// @match        https://*.lichess.org/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

"use strict";

// -----------------------------------------------------------------------------
// Configuration & State
// -----------------------------------------------------------------------------
const BRIDGE_URL = "http://127.0.0.1:5000";

const state = {
  moves: [],
  orientation: "w",
  mode: "analysis",
  running: false,
  lastResult: null,
  panel: null,
  svg: null,
  debounceTimer: null,
  gameOverSent: false,
  playerColor: "white",
  site: null
};

// -----------------------------------------------------------------------------
// Site Adapter
// -----------------------------------------------------------------------------
const SiteAdapter = {
  chesscom: {
    name: "chesscom",
    extractMoves: function() {
      const sanList = [];
      const plyElements = document.querySelectorAll(
        ".move-list-row .main-line-ply, .move-list-row [data-node].main-line-ply"
      );
      plyElements.forEach((ply) => {
        let container = ply.querySelector(".node-highlight-content") || ply;
        const clone = container.cloneNode(true);
        const figurineEls = clone.querySelectorAll("[data-figurine]");
        figurineEls.forEach((el) => {
          const fig = el.getAttribute("data-figurine") || "";
          const textNode = document.createTextNode(fig);
          el.replaceWith(textNode);
        });
        const san = clone.textContent.trim().replace(/\s+/g, '');
        if (san) sanList.push(san);
      });
      return sanList;
    },
    getOrientation: function() {
      const board = document.querySelector("wc-chess-board");
      const isFlipped = board && board.classList.contains("flipped");
      const orientation = isFlipped ? "b" : "w";
      state.playerColor = isFlipped ? "black" : "white";
      return orientation;
    },
    getBoardElement: function() {
      return document.querySelector("wc-chess-board");
    },
    getBoardContainer: function() {
      const board = this.getBoardElement();
      return board ? board.parentElement : null;
    },
    detectGameResult: function() {
      const resultSelectors = [
        ".game-over-modal",
        ".game-over",
        "[data-testid='game-over']",
        ".archive-game-result",
        ".game-result",
        ".game-over-modal-content",
        ".modal-game-over-component",
        "[class*='game-over-header']",
        ".board-layout-sidebar",
        ".board-layout-main"
      ];
      
      const resultStrings = [
        "you won",
        "white won",
        "black won",
        "checkmate",
        "resignation",
        "resigned",
        "draw",
        "stalemate",
        "insufficient material",
        "threefold repetition",
        "50 move rule",
        "timeout",
        "abandoned"
      ];
      
      for (const sel of resultSelectors) {
        const el = document.querySelector(sel);
        if (el) {
          const text = el.textContent.toLowerCase();
          for (const rs of resultStrings) {
            if (text.includes(rs)) {
              if (rs === "you won") {
                return { result: "win", winner: state.playerColor };
              }
              if (rs === "white won" || (text.includes("white") && (text.includes("won") || text.includes("victory") || text.includes("checkmate")))) {
                return { result: "win", winner: "white" };
              }
              if (rs === "black won" || (text.includes("black") && (text.includes("won") || text.includes("victory") || text.includes("checkmate")))) {
                return { result: "win", winner: "black" };
              }
              if (rs === "draw" || rs === "stalemate" || rs === "insufficient material" || rs === "threefold repetition" || rs === "50 move rule") {
                return { result: "draw", winner: "draw" };
              }
              if (rs === "checkmate" || rs === "resignation" || rs === "resigned" || rs === "timeout" || rs === "abandoned") {
                if (text.includes("white") && !text.includes("black")) {
                  return { result: "win", winner: "white" };
                }
                if (text.includes("black") && !text.includes("white")) {
                  return { result: "win", winner: "black" };
                }
                return { result: "win", winner: null };
              }
            }
          }
        }
      }
      return null;
    }
  },
  lichess: {
    name: "lichess",
    extractMoves: function() {
      const sanList = [];
      // Main game moves: .z7yx elements inside the move list (app > z7yx)
      const moveElements = document.querySelectorAll("z7yx");
      moveElements.forEach((el) => {
        const san = el.textContent.trim().replace(/\s+/g, '');
        if (san) sanList.push(san);
      });
      // Also check for analysis/study board moves: l4x > kwdb
      const analysisMoves = document.querySelectorAll("l4x > kwdb");
      analysisMoves.forEach((el) => {
        const san = el.textContent.trim().replace(/\s+/g, '');
        if (san && !sanList.includes(san)) sanList.push(san);
      });
      return sanList;
    },
    getOrientation: function() {
      const wrap = document.querySelector(".cg-wrap");
      const isBlack = wrap && wrap.classList.contains("orientation-black");
      const orientation = isBlack ? "b" : "w";
      state.playerColor = isBlack ? "black" : "white";
      return orientation;
    },
    getBoardElement: function() {
      return document.querySelector(".cg-board");
    },
    getBoardContainer: function() {
      return document.querySelector(".cg-wrap") || document.querySelector(".cg-board");
    },
    detectGameResult: function() {
      const resultSelectors = [
        ".rcontrols .result-wrap",
        ".rcontrols .result",
        ".rcontrols .status"
      ];
      
      for (const sel of resultSelectors) {
        const el = document.querySelector(sel);
        if (el) {
          const text = el.textContent.toLowerCase();
          // "White is victorious", "Black is victorious", "Checkmate", "Draw", "Stalemate"
          if (text.includes("white is victorious") || text.includes("white wins")) {
            return { result: "win", winner: "white" };
          }
          if (text.includes("black is victorious") || text.includes("black wins")) {
            return { result: "win", winner: "black" };
          }
          if (text.includes("checkmate")) {
            // Winner is the opposite of who is checkmated
            if (text.includes("white")) {
              return { result: "win", winner: "black" };
            }
            if (text.includes("black")) {
              return { result: "win", winner: "white" };
            }
            return { result: "win", winner: null };
          }
          if (text.includes("draw") || text.includes("stalemate") || text.includes("insufficient") || text.includes("repetition") || text.includes("50 move")) {
            return { result: "draw", winner: "draw" };
          }
          if (text.includes("timeout") || text.includes("abandoned") || text.includes("resign")) {
            if (text.includes("white")) {
              return { result: "win", winner: "black" };
            }
            if (text.includes("black")) {
              return { result: "win", winner: "white" };
            }
            return { result: "win", winner: null };
          }
        }
      }
      return null;
    }
  }
};

function getCurrentSite() {
  const hostname = window.location.hostname;
  if (hostname === "chess.com" || hostname.endsWith(".chess.com")) {
    return "chesscom";
  }
  if (hostname === "lichess.org" || hostname.endsWith(".lichess.org")) {
    return "lichess";
  }
  return null;
}

function getAdapter() {
  const site = state.site;
  return SiteAdapter[site] || SiteAdapter.chesscom;
}

// -----------------------------------------------------------------------------
// DOM Extraction (delegated to site adapter)
// -----------------------------------------------------------------------------
function extractMoves() {
  return getAdapter().extractMoves();
}

function getOrientation() {
  return getAdapter().getOrientation();
}

// -----------------------------------------------------------------------------
// Game Over Detection & Reporting (delegated to site adapter)
// -----------------------------------------------------------------------------
function detectGameResult() {
  return getAdapter().detectGameResult();
}

function sendGameOver(result, winner, moves) {
  if (state.gameOverSent) return;
  
  const payload = {
    result: result,
    winner: winner,
    moves: moves
  };
  
  GM_xmlhttpRequest({
    method: "POST",
    url: BRIDGE_URL + "/game-over",
    data: JSON.stringify(payload),
    headers: { "Content-Type": "application/json" },
    timeout: 5000,
    onload: (res) => {
      try {
        const parsed = JSON.parse(res.responseText);
        if (parsed.ok) {
          console.log("[Duckish] Game-over reported successfully");
        }
      } catch (e) {
        console.error("[Duckish] Game-over response parse error:", e);
      }
    },
    onerror: () => {
      console.warn("[Duckish] Game-over request failed (bridge offline?)");
    },
    ontimeout: () => {
      console.warn("[Duckish] Game-over request timeout");
    }
  });
  
  state.gameOverSent = true;
}

// -----------------------------------------------------------------------------
// MutationObserver - debounced board change detection
// -----------------------------------------------------------------------------
function scheduleAnalyze() {
  if (state.debounceTimer) clearTimeout(state.debounceTimer);
  state.debounceTimer = setTimeout(onBoardChanged, 250);
}

function onBoardChanged() {
  const moves = extractMoves();
  const orientation = getOrientation();
  const movesKey = JSON.stringify(moves);
  const currentMovesKey = JSON.stringify(state.moves);
  
  // Determine whose turn it is from move count
  const turnColor = moves.length % 2 === 0 ? "white" : "black";
  
  // Check for game over
  const gameResult = detectGameResult();
  if (gameResult && !state.gameOverSent && moves.length > 0) {
    sendGameOver(gameResult.result, gameResult.winner, moves);
  }
  
  // Reset gameOverSent if new game started (moves reset to 0 or 1)
  if (moves.length <= 1) {
    state.gameOverSent = false;
  }
  
  // Strict turn guard: only analyze/draw arrows on player's turn
  if (turnColor !== state.playerColor) {
    if (state.svg) {
      state.svg.remove();
      state.svg = null;
    }
    const body = document.getElementById("duckish-body");
    if (body) {
      body.innerHTML = `<div style="text-align:center;color:#9ca3af;padding:20px;">Waiting for opponent...</div>`;
    }
    setStatus("waiting", "#6b7280");
    return;
  }
  
  if (movesKey !== currentMovesKey || orientation !== state.orientation) {
    state.moves = moves;
    state.orientation = orientation;
    analyze();
  }
}

const observer = new MutationObserver(scheduleAnalyze);
observer.observe(document.body, { childList: true, subtree: true });

// -----------------------------------------------------------------------------
// Floating Panel UI
// -----------------------------------------------------------------------------
function createPanel() {
  if (state.panel) return state.panel;
  const panel = document.createElement("div");
  panel.id = "duckish-panel";
  panel.style.cssText = `
    position: fixed; top: 12px; right: 12px; z-index: 2147483647; width: 300px;
    background: rgba(20,22,28,0.85); backdrop-filter: blur(12px);
    border: 1px solid rgba(255,255,255,0.12); border-radius: 12px;
    color: #e6e8ee; font-family: system-ui, -apple-system, sans-serif;
    padding: 12px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); user-select: none;
  `;
  const header = document.createElement("div");
  header.style.cssText = `
    display: flex; align-items: center; justify-content: space-between;
    cursor: move; margin-bottom: 8px; padding-bottom: 8px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
  `;
  header.innerHTML = `
    <span style="font-weight:600;font-size:14px;">Duckish 1.0</span>
    <span id="duckish-status-dot" style="width:10px;height:10px;border-radius:50%;background:#6b7280;flex-shrink:0;"></span>
  `;
  let dragOffsetX = 0,
    dragOffsetY = 0,
    isDragging = false;
  header.addEventListener("mousedown", (e) => {
    isDragging = true;
    const rect = panel.getBoundingClientRect();
    dragOffsetX = e.clientX - rect.left;
    dragOffsetY = e.clientY - rect.top;
    panel.style.transition = "none";
    e.preventDefault();
  });
  document.addEventListener("mousemove", (e) => {
    if (!isDragging) return;
    panel.style.left = e.clientX - dragOffsetX + "px";
    panel.style.top = e.clientY - dragOffsetY + "px";
    panel.style.right = "auto";
  });
  document.addEventListener("mouseup", () => {
    isDragging = false;
  });
  const body = document.createElement("div");
  body.id = "duckish-body";
  panel.appendChild(header);
  panel.appendChild(body);
  document.body.appendChild(panel);
  state.panel = panel;
  return panel;
}

function setStatus(text, color) {
  const dot = document.getElementById("duckish-status-dot");
  if (dot) dot.style.background = color;
  const body = document.getElementById("duckish-body");
  if (body) body.dataset.status = text;
}

// -----------------------------------------------------------------------------
// Score Formatting & WDL Bar
// -----------------------------------------------------------------------------
function formatScore(entry) {
  if (entry.score_mate != null) {
    const m = entry.score_mate;
    return (m > 0 ? "M" : "-M") + Math.abs(m);
  }
  if (entry.score_cp != null) {
    const cp = entry.score_cp / 100;
    const sign = cp >= 0 ? "+" : "";
    return sign + cp.toFixed(2);
  }
  return "0.00";
}

function createWdlBar(entry) {
  const container = document.createElement("div");
  container.style.cssText = `
    display: flex; height: 6px; border-radius: 3px; overflow: hidden;
    margin-top: 4px; background: #1e2026;
  `;
  let wdl = entry.wdl || [0, 0, 0];
  const sum = wdl[0] + wdl[1] + wdl[2];
  if (sum === 0) wdl = [33.33, 33.34, 33.33];
  const colors = ["#e6e8ee", "#6b7280", "#111318"];
  wdl.forEach((pct, i) => {
    const seg = document.createElement("div");
    seg.style.cssText = `flex:0 0 ${(pct / sum * 100).toFixed(2)}%;background:${colors[i]};transition:flex-basis 0.3s ease;`;
    container.appendChild(seg);
  });
  return container;
}

// -----------------------------------------------------------------------------
// Render Panel Content
// -----------------------------------------------------------------------------
function render() {
  const body = document.getElementById("duckish-body");
  if (!body) return;
  const data = state.lastResult;
  if (!data || !data.entries || data.entries.length === 0) {
    body.innerHTML = `<div style="text-align:center;color:#9ca3af;padding:20px;">Waiting for position...</div>`;
    return;
  }
  const best = data.entries[0];
  const evalLine = document.createElement("div");
  evalLine.style.cssText = "margin-bottom:12px;";
  evalLine.innerHTML = `<div style="font-size:28px;font-weight:700;letter-spacing:-0.5px;">${formatScore(best)}</div>`;
  evalLine.appendChild(createWdlBar(best));
  const candidates = document.createElement("div");
  candidates.style.cssText = "margin-bottom:12px;";
  const maxCandidates = Math.min(3, data.entries.length);
  for (let i = 0; i < maxCandidates; i++) {
    const e = data.entries[i];
    const pvText = (e.pv || []).join(" ");
    const row = document.createElement("div");
    row.style.cssText = `
      display:flex;align-items:center;gap:8px;padding:6px 8px;
      background:rgba(255,255,255,0.03);border-radius:6px;margin-bottom:4px;font-size:13px;
    `;
    row.innerHTML = `
      <span style="width:20px;height:20px;border-radius:4px;background:rgba(52,211,153,0.2);color:#34d399;font-weight:700;font-size:11px;display:flex;align-items:center;justify-content:center;flex-shrink:0;">${i + 1}</span>
      <span style="font-weight:600;font-size:14px;min-width:48px;">${e.san || ""}</span>
      <span style="flex:1;color:#9ca3af;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${pvText}</span>
      <span style="font-variant-numeric:tabular-nums;color:#d1d5db;">${formatScore(e)}</span>
    `;
    candidates.appendChild(row);
  }
  const controls = document.createElement("div");
  controls.style.cssText = "display:flex;gap:8px;";
  const modeBtn = document.createElement("button");
  modeBtn.textContent = state.mode === "analysis" ? "Analysis (3)" : "Fast (1)";
  const btnStyle = `
    flex:1;padding:8px 12px;border-radius:6px;border:1px solid rgba(255,255,255,0.15);
    background:rgba(255,255,255,0.05);color:#e6e8ee;font-size:12px;font-weight:500;
    cursor:pointer;transition:background 0.15s;
  `;
  modeBtn.style.cssText = btnStyle;
  modeBtn.addEventListener("mouseenter", () => (modeBtn.style.background = "rgba(255,255,255,0.1)"));
  modeBtn.addEventListener("mouseleave", () => (modeBtn.style.background = "rgba(255,255,255,0.05)"));
  modeBtn.addEventListener("click", () => {
    state.mode = state.mode === "analysis" ? "fast" : "analysis";
    modeBtn.textContent = state.mode === "analysis" ? "Analysis (3)" : "Fast (1)";
    analyze();
  });
  const clearBtn = document.createElement("button");
  clearBtn.textContent = "Clear arrows";
  clearBtn.style.cssText = btnStyle;
  clearBtn.addEventListener("mouseenter", () => (clearBtn.style.background = "rgba(255,255,255,0.1)"));
  clearBtn.addEventListener("mouseleave", () => (clearBtn.style.background = "rgba(255,255,255,0.05)"));
  clearBtn.addEventListener("click", () => {
    if (state.svg) {
      state.svg.remove();
      state.svg = null;
    }
  });
  controls.appendChild(modeBtn);
  controls.appendChild(clearBtn);
  body.innerHTML = "";
  body.appendChild(evalLine);
  body.appendChild(candidates);
  body.appendChild(controls);
}

function renderError(msg) {
  const body = document.getElementById("duckish-body");
  if (!body) return;
  body.innerHTML = `<div style="text-align:center;color:#fca5a5;padding:20px;font-size:13px;">${msg}</div>`;
}

// -----------------------------------------------------------------------------
// Bridge Communication
// -----------------------------------------------------------------------------
function analyze() {
  if (state.running) return;
  if (state.svg) {
    state.svg.remove();
    state.svg = null;
  }
  state.running = true;
  setStatus("thinking...", "#fbbf24");
  const payload = {
    moves: state.moves,
    movetime: 400,
    multipv: state.mode === "analysis" ? 3 : 1
  };
  GM_xmlhttpRequest({
    method: "POST",
    url: BRIDGE_URL + "/analyze",
    data: JSON.stringify(payload),
    headers: { "Content-Type": "application/json" },
    timeout: 15000,
    onload: (res) => {
      state.running = false;
      try {
        const parsed = JSON.parse(res.responseText);
        if (parsed.ok) {
          // Double-check turn from server matches player color
          if (parsed.turn && parsed.turn !== state.playerColor) {
            if (state.svg) { state.svg.remove(); state.svg = null; }
            const body = document.getElementById("duckish-body");
            if (body) {
              body.innerHTML = `<div style="text-align:center;color:#9ca3af;padding:20px;">Waiting for opponent...</div>`;
            }
            setStatus("waiting", "#6b7280");
            state.running = false;
            return;
          }
          state.lastResult = parsed;
          setStatus("ready", "#34d399");
          render();
          drawBestArrow(parsed);
        } else {
          if (state.svg) { state.svg.remove(); state.svg = null; }
          setStatus("error", "#ef4444");
          renderError(parsed.error || "Unknown error");
        }
      } catch (e) {
        if (state.svg) { state.svg.remove(); state.svg = null; }
        setStatus("error", "#ef4444");
        renderError("Invalid response: " + e.message);
      }
    },
    onerror: () => {
      state.running = false;
      if (state.svg) { state.svg.remove(); state.svg = null; }
      setStatus("bridge offline", "#ef4444");
      renderError("Bridge offline \u2014 start bridge/server.py");
    },
    ontimeout: () => {
      state.running = false;
      if (state.svg) { state.svg.remove(); state.svg = null; }
      setStatus("bridge offline", "#ef4444");
      renderError("Bridge timeout \u2014 start bridge/server.py");
    }
  });
}

// -----------------------------------------------------------------------------
// Arrow Overlay (SVG) - uses site adapter for board element
// -----------------------------------------------------------------------------
function drawBestArrow(data) {
  if (state.svg) {
    state.svg.remove();
    state.svg = null;
  }
  const board = getAdapter().getBoardElement();
  if (!board) return;
  const best = (data.bestmove && data.bestmove.uci) ? data.bestmove : (data.multipv && data.multipv[0]);
  if (!best || !best.uci) return;
  const parent = getAdapter().getBoardContainer();
  if (!parent) return;
  const computed = getComputedStyle(parent);
  if (computed.position === "static") parent.style.position = "relative";
  const width = board.clientWidth;
  const height = board.clientHeight;
  const sq = width / 8;
  const uci = best.uci;
  if (!/^[a-h][1-8][a-h][1-8]/.test(uci || "")) return;
  const fromFile = uci.charCodeAt(0) - 97;
  const fromRank = parseInt(uci.charAt(1), 10) - 1;
  const toFile = uci.charCodeAt(2) - 97;
  const toRank = parseInt(uci.charAt(3), 10) - 1;
  const orient = state.orientation;
  const fFile = orient === "b" ? 7 - fromFile : fromFile;
  const fRank = orient === "b" ? 7 - fromRank : fromRank;
  const tFile = orient === "b" ? 7 - toFile : toFile;
  const tRank = orient === "b" ? 7 - toRank : toRank;
  const cx1 = (fFile + 0.5) * sq;
  const cy1 = (7.5 - fRank) * sq;
  const rawX2 = (tFile + 0.5) * sq;
  const rawY2 = (7.5 - tRank) * sq;
  const dx = rawX2 - cx1;
  const dy = rawY2 - cy1;
  const dist = Math.sqrt(dx * dx + dy * dy) || 1;
  const shorten = sq * 0.35;
  const cx2 = rawX2 - (dx / dist) * shorten;
  const cy2 = rawY2 - (dy / dist) * shorten;
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.style.cssText = `position:absolute;width:${width}px;height:${height}px;pointer-events:none;z-index:10;`;
  svg.style.left = board.offsetLeft + "px";
  svg.style.top = board.offsetTop + "px";
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  const defs = document.createElementNS(svgNS, "defs");
  const marker = document.createElementNS(svgNS, "marker");
  marker.setAttribute("id", "duckish-arrowhead");
  marker.setAttribute("viewBox", "0 0 10 10");
  marker.setAttribute("refX", "8");
  marker.setAttribute("refY", "5");
  marker.setAttribute("markerWidth", "6");
  marker.setAttribute("markerHeight", "6");
  marker.setAttribute("orient", "auto-start-reverse");
  const path = document.createElementNS(svgNS, "path");
  path.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
  path.setAttribute("fill", "#7dd3fc");
  marker.appendChild(path);
  defs.appendChild(marker);
  svg.appendChild(defs);
  const fromRect = document.createElementNS(svgNS, "rect");
  fromRect.setAttribute("x", fFile * sq);
  fromRect.setAttribute("y", (7 - fRank) * sq);
  fromRect.setAttribute("width", sq);
  fromRect.setAttribute("height", sq);
  fromRect.setAttribute("fill", "#fde047");
  fromRect.setAttribute("fill-opacity", "0.35");
  svg.appendChild(fromRect);
  const toRect = document.createElementNS(svgNS, "rect");
  toRect.setAttribute("x", tFile * sq);
  toRect.setAttribute("y", (7 - tRank) * sq);
  toRect.setAttribute("width", sq);
  toRect.setAttribute("height", sq);
  toRect.setAttribute("fill", "#4ade80");
  toRect.setAttribute("fill-opacity", "0.35");
  svg.appendChild(toRect);
  const line = document.createElementNS(svgNS, "line");
  line.setAttribute("x1", cx1);
  line.setAttribute("y1", cy1);
  line.setAttribute("x2", cx2);
  line.setAttribute("y2", cy2);
  line.setAttribute("stroke", "#7dd3fc");
  line.setAttribute("stroke-width", Math.max(2, sq * 0.12));
  line.setAttribute("stroke-linecap", "round");
  line.setAttribute("marker-end", "url(#duckish-arrowhead)");
  svg.appendChild(line);
  parent.appendChild(svg);
  state.svg = svg;
}

// -----------------------------------------------------------------------------
// Initialization
// -----------------------------------------------------------------------------
(function init() {
  try {
    // Hostname guard: only run on chess.com or lichess.org
    const site = getCurrentSite();
    if (!site) {
      console.log("[Duckish] Not on a supported site, exiting");
      return;
    }
    state.site = site;
    console.log("[Duckish] Initialized for", site);
    
    createPanel();
    setTimeout(() => {
      state.moves = extractMoves();
      state.orientation = getOrientation();
      analyze();
    }, 500);
  } catch (e) {
    console.error("[Duckish] Init error:", e);
  }
})();