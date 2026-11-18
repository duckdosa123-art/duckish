# Duckish Bridge Server

HTTP bridge for the Duckish UCI chess engine. Pure Python standard library + `python-chess`.

## Requirements

- Python 3.8+
- Duckish engine built at `../target/release/duckish.exe` (from repo root)

```bash
pip install -r requirements.txt
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DUCKISH_MULTIPV` | `3` | Default MultiPV (1..10) |

## Running

```bash
# From repo root (E:\dev\duckish)
python -m bridge.server --port 5000 --movetime 400
```

- `--port`: HTTP port (default 5000)
- `--movetime`: Default analysis time in ms (default 400, clamp 50..5000)

Press `Ctrl+C` to stop. The engine process is terminated on exit.

## Endpoints

### `POST /analyze`

Analyze a position.

**Request body (JSON):**
```json
{
  "moves": ["e4", "e5", "Nf3"],   // SAN move list from startpos
  "movetime": 400,                // optional, 50..5000 ms
  "multipv": 3,                   // optional, 1..10
  "depth": 20                     // optional, use depth instead of movetime
}
```

**Response:**
```json
{
  "ok": true,
  "depth": 18,
  "bestmove": {"uci": "g1f3", "san": "Nf3"},
  "multipv": [
    {
      "rank": 1,
      "uci": "g1f3",
      "san": "Nf3",
      "score_cp": 28,
      "score_mate": null,
      "wdl": [512, 256, 232],
      "pv_uci": ["g1f3", "g8f6", ...],
      "pv_san": ["Nf3", "Nf6", ...],
      "pv_text": "Nf3 Nf6 ...",
      "seldepth": 28,
      "nodes": 1234567,
      "nps": 2500000,
      "time_ms": 400,
      "hashfull": 123
    }
    // ... up to multipv lines
  ],
  "nodes": 1234567,
  "nps": 2500000,
  "time_ms": 400,
  "hashfull": 123
}
```

- `multipv` array sorted by `rank` (1 = best).
- `score_cp` = centipawns from side to move; `score_mate` = mate in N (positive = winning).
- `wdl` = [win, draw, loss] in permille (sum = 1000).
- `pv_uci` / `pv_san` = principal variation in UCI / SAN.
- On error: `{"ok": false, "error": "..."}` with HTTP 400/500.

### `GET /health`

Engine health check.

**Response:**
```json
{
  "status": "ok",
  "engine": "duckish",
  "pid": 1234,
  "multipv_default": 3,
  "alive": true
}
```

### CORS

All responses include `Access-Control-Allow-Origin: *`. `OPTIONS` preflight supported.

## Logging

One line per request to stdout:
```
POST /analyze -> 200 (412ms)
GET /health -> 200 (1ms)
```

## Architecture

- `Engine` class manages the UCI subprocess with a `threading.Lock` for serialized I/O.
- Dead engine detection + automatic restart on next request.
- Stderr drained by a daemon thread to prevent pipe blocking.
- A daemon reader thread drains engine stdout into a `queue.Queue`; `_read_line(deadline)` uses `queue.get(timeout=...)`. `select.select` is NOT used because it cannot poll pipes on Windows (WinError 10038).
- SAN↔UCI conversion via `python-chess.Board.parse_san` / `Board.san`.
- MultiPV parsing: groups `info` lines by `depth`, keeps only the LAST line per `(depth, rank)` (the engine reprints rank 1 several times per depth during aspiration re-searches), then picks the deepest depth that contains every rank from 1 to the requested MultiPV.
- PV and bestmove converted to SAN by replaying on the position.

## Troubleshooting

- `OSError: [WinError 10038]` on startup: you are on an older build that used `select.select` on a pipe. Re-pull; the reader is now thread + queue based.
- `{"ok": false, "error": "engine timeout"}`: raise `--movetime`, or pass a `depth` in the request.
- Duplicate candidate rows in the UI: the bridge now de-duplicates by rank; a stale copy of `server.py` is running.
- Arrows land on the wrong squares: the overlay is positioned from `wc-chess-board`'s `offsetLeft`/`offsetTop`; if Chess.com adds padding around the board this needs a visual QA pass.