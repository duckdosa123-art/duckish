# Duckish 1.0

> **Duckish Fork Notice**
>
> Duckish 1.0 is a modified fork of [cosmobobak/viridithas](https://github.com/cosmobobak/viridithas), distributed under the **GNU Affero General Public License v3.0** (`LICENSE`). Upstream copyright and attribution are retained, including the bundled Pyrrhic/Fathom tablebase code described in `THIRD-PARTY-NOTICES.md`. Duckish is not affiliated with or endorsed by the Viridithas author.

## 1. Overview

Duckish is a Rust NNUE chess engine (a fork of Viridithas) with a Python bridge layer for analysis, Lichess bot play, and experience-based learning. The Rust engine speaks UCI; the Python layer wraps it with an HTTP analysis server, a post-mortem blunder-learning book, and an official Lichess Bot client with a live control dashboard.

## 2. Engine Modifications (Rust NNUE)

Duckish is a fork of Viridithas with the following enhancements:

- **MultiPV 1-10 root-exclusion search.** Each converged principal variation has its root move excluded and the search is re-run for the next rank. PVs are ranked by score, so `multipv 1` always equals `bestmove`. A new `MultiPV` UCI option is exposed (default 1, range 1-10).
- **Internal Iterative Deepening (IID).** A depth-1 shallow pass on a transposition-table miss before the full-depth search.
- **Polyglot `.bin` opening book probing.** A local book (e.g. `Performance.bin`) is probed at the root for time-based `go` commands (`wtime`/`btime`/`movetime`). `go depth` bypasses the book and searches directly.
- **Dynamic CPU thread scaling.** `Threads = max(1, cpu_count // 2)` by default, with a `setoption` override.
- **256 MB default transposition hash.** Raised from the upstream 16 MB default.

### Honest technical caveats

- These are experimental features and may not match upstream Viridithas strength.
- `MultiPV > 1` has reduced search efficiency: it runs N root searches per depth single-threaded for deterministic candidate ordering. Throughput is traded for reproducibility on purpose.
- Engine strength and search behaviour are unchanged from upstream when `MultiPV = 1`.

## 3. Bridge HTTP Server (`bridge/server.py`)

A pure Python (stdlib + python-chess) HTTP bridge that wraps the engine:

- `POST /analyze` - analyze a position with MultiPV; returns `bestmove` plus ranked principal variations (UCI + SAN).
- `GET /health` - engine health check.
- Default port `5000`.

Run:

```bash
python -m bridge.server --port 5000
```

Optional flags: `--movetime <ms>` (default 400) and `--threads <n>` (default: half of CPU cores).

## 4. Live Experience & Mistake-Learning Book

`bridge/experience.py` is a SQLite-based post-mortem blunder auditor (`experience.db`):

- Records final positions with W/D/L results.
- Penalizes losing moves (blunder threshold: -80 cp).
- Boosts stronger alternatives (+15 cp bonus).

**Visual Experience Book Explorer** at `http://127.0.0.1:5001/experience` (integrated into the Lichess dashboard):

- Searchable lessons table with a filter (all / penalties / bonuses).
- Interactive 8x8 CSS/Unicode chessboard.
- Red arrows for penalized blunders, green arrows for boosted corrections.
- Summary banner: total positions, mistakes, boosts, W/D/L record.

Also available via the `GET /api/experience` JSON API.

## 5. Official Lichess BOT Runner & Live Control Dashboard

A modular `bridge/lichess/` package:

- `config.py` - token loading (env / `.env`), paths, `BotState`.
- `api.py` - Lichess HTTP client (urllib / requests).
- `engine_client.py` - persistent UCI engine wrapper (movetime-capped search).
- `runner.py` - event stream, challenge handling, game loop, auto-match with cooldowns.
- `dashboard.py` - live control dashboard (dark mode, auto-match controls, activity console).
- `bridge/lichess_bot.py` - CLI entry point.

CLI flags:

- `--upgrade` - upgrade the account to a BOT account (one-time, irreversible).
- `--auto-match` - enable autonomous bot matchmaking.
- `--challenge <user>` - send a 3+2 challenge to a specific user.
- `--casual` - make the challenge casual (default: rated).
- `--open-browser` - open the dashboard in a browser on start.
- `--token` - supply the Lichess Bot API token inline.

Dashboard at `http://127.0.0.1:5001` with:

- Online status badge and live phase/detail.
- Start/Stop Auto-Match (fetches up to 30 online bots; cooldowns for rate-limited or declined opponents).
- Manual challenge input.
- Live activity console (last 50 events).
- Watch Live on Lichess link.
- View Experience Book link.

Token loading priority: `--token` > `LICHESS_TOKEN` env var > `.env` file.

Run (from the repo root):

```bash
python -m bridge.lichess_bot
```

## 6. Chess.com & Lichess Analysis Overlay

`userscript/duckish-advisor.user.js` is a Tampermonkey userscript that draws the engine's best move on the board and shows ranked candidate lines.

Setup:

1. Install Tampermonkey in your browser.
2. Add the userscript (`userscript/duckish-advisor.user.js`).
3. Start the bridge server (`python -m bridge.server --port 5000`).

**Fair-play disclaimer:** strictly for offline self-study, analysis boards, and computer bot practice on normal accounts. NOT for cheating in rated games.

## 7. Installation & Running

### Prerequisites

- Rust toolchain (for the engine).
- Python 3.8+ (for the bridge).

### Build the engine

```bash
cargo build --release
```

Download `viridithas.nnue.zst` from the [viridithas-networks](https://github.com/cosmobobak/viridithas-networks/releases) releases and place it in the repo root.

### Install Python dependencies

```bash
pip install -r bridge/requirements.txt
```

### Start the bridge server

```bash
python -m bridge.server --port 5000
```

### Start the Lichess bot

```bash
python -m bridge.lichess_bot
```

## 8. Project Structure

```
duckish/
├── src/                     # Rust NNUE engine (Viridithas fork)
├── bridge/
│   ├── server.py            # HTTP bridge: /analyze, /health, /experience
│   ├── experience.py        # SQLite mistake-learning book
│   ├── requirements.txt     # python-chess, requests
│   ├── lichess_bot.py       # Lichess bot CLI entry point
│   └── lichess/
│       ├── config.py        # token loading, paths, BotState
│       ├── api.py           # Lichess HTTP client
│       ├── engine_client.py # persistent UCI engine wrapper
│       ├── runner.py        # event stream, game loop, auto-match
│       └── dashboard.py     # live control dashboard (port 5001)
├── userscript/
│   └── duckish-advisor.user.js  # Tampermonkey Chess.com/Lichess overlay
├── Performance.bin          # local Polyglot book (gitignored)
└── README.md
```

## 9. License

Viridithas is free software, licensed under the [GNU Affero General Public License v3.0](LICENSE) (`AGPL-3.0-only`).

- The neural networks embedded in Viridithas (the `.nnue` / `.nnue.zst` files, hosted in [viridithas-networks](https://github.com/cosmobobak/viridithas-networks)) are dedicated to the public domain under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/).
- Viridithas bundles third-party components under their own AGPLv3-compatible terms; see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
- Versions up to and including 20.0.0 were released under the MIT License and remain available under those terms.

By contributing to Viridithas, you agree that your contributions will be licensed under the `AGPL-3.0-only` license.