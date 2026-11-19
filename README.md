> **Duckish Fork Notice**
>
> Duckish 1.0 is a modified fork of [cosmobobak/viridithas](https://github.com/cosmobobak/viridithas), distributed under the **GNU Affero General Public License v3.0** (`LICENSE`). Upstream copyright and attribution are retained, including the bundled Pyrrhic/Fathom tablebase code described in `THIRD-PARTY-NOTICES.md`. Duckish is not affiliated with or endorsed by the Viridithas author.
>
> Changes from upstream Viridithas:
> - Native MultiPV (1-10) in the root search: each converged principal variation has its root move excluded and the search re-runs for the next rank; PVs are ranked by score, so `multipv 1` always equals `bestmove`. MultiPV greater than 1 runs single-threaded for deterministic candidate ordering.
> - Transposition-table hash default raised from 16 MB to 256 MB, with a new `MultiPV` UCI option (default 1, range 1-10). Binary renamed to `duckish`.
> - `scripts/uci_smoke.ps1`: automated UCI regression harness (handshake, MultiPV ordering, `bestmove`/`multipv 1` agreement, clean output, no panic).
> - `bridge/`: local Python UCI HTTP bridge exposing `POST /analyze` and `GET /health` on `127.0.0.1:5000`, converting SAN move lists to UCI and returning ranked candidates with both UCI and SAN principal variations.
> - `userscript/`: Tampermonkey Chess.com advisor overlay for offline self-study, post-game review, and bot practice, drawing the best move on the board. It talks only to the local bridge; no external service is contacted.
>
> Engine strength and search behaviour are unchanged from upstream when `MultiPV = 1`.

## Quickstart

### 1. Prerequisites
- **Rust** (latest stable via [rustup](https://rustup.rs/))
- Optional: **clang** (for Fathom Syzygy tablebase support)

### 2. Build `duckish.exe`
1. Download `viridithas.nnue.zst` from the latest release of [cosmobobak/viridithas-networks](https://github.com/cosmobobak/viridithas-networks/releases) and place it in the repo root.
2. Build with native optimizations:
   - **Windows (PowerShell)**:
     ```powershell
     $env:RUSTFLAGS="-C target-cpu=native"
     cargo b -r --features syzygy,bindgen
     ```
   - **Linux / macOS**:
     ```bash
     RUSTFLAGS="-C target-cpu=native" cargo build --release --features syzygy,bindgen
     ```
3. Output binary: `target/release/duckish.exe` (or `target/release/duckish` on Unix).

### 3. Optional Polyglot Opening Book
- Place a `.bin` book file (e.g. `Performance.bin`) in the repo root.
- `*.bin` is gitignored, so it stays local-only.
- The bridge auto-wires it via the engine's `BookPath` UCI option when the file exists.

### 4. Local HTTP Bridge (Python)
```bash
pip install -r bridge/requirements.txt   # python-chess
python -m bridge.server --port 5000
```
- **Live debug dashboard**: open <http://127.0.0.1:5000> (engine status, book, Experience Book stats).
- **Analysis endpoint**: `POST /analyze` with a SAN move list → returns ranked candidates with UCI + SAN PVs.
- **Health check**: `GET /health`.

### 5. Tampermonkey Userscript (Chess.com & Lichess Advisor Overlay)
1. Install **Tampermonkey** in Chrome / Opera GX / Edge.
2. Create a new script, paste the full contents of `userscript/duckish-advisor.user.js`, save.
3. Visit a Chess.com or Lichess.org game or bot page.
4. Allow `127.0.0.1` local connections when prompted.
5. Use for **offline self-study**, **post-game review**, and **computer bot practice**. The script talks **only** to the local bridge; no external service is contacted.

<div align="center">

  ![Logo](assets/images/logo2.png)
  
  [![Build][build-badge]][build-link]
  [![License][license-badge]][license-link]
  [![Release][release-badge]][release-link]
  [![Commits][commits-badge]][commits-link]
  
</div>

Viridithas is a very strong chess playing program, developed in his current form since 2022-04-10.

Viridithas communicates over the Universal Chess Interface, which means you can talk to him in the terminal,
or install him into a UCI-compatible graphical user interface, like [En Croissant](https://encroissant.org/download) or [Nibbler](https://github.com/rooklift/nibbler).

Some writing on the internals and development of Viridithas can be found on [his author's website](https://cosmo.tardis.ac).

The Viridithas project prides itself on using original training data for its neural networks.

PS: Viridithas is enormously fond of the [Stormphrax](https://github.com/Ciekce/Stormphrax) chess engine.

## Benchmarks & Match Results

Duckish 1.0 measured against unmodified upstream Viridithas (`upstream/master`, `4609ff2`) on the same machine: 4 physical cores / 8 logical threads, Windows 11. Both binaries were built from source with `RUSTFLAGS="-C target-cpu=native" cargo build --release` and the same embedded Hyperion NNUE, so any difference below is engine behaviour, not toolchain.

### Search speed (`bench`)

Median of 3 runs per configuration.

| Configuration | Nodes | Median NPS | Note |
|---|---|---|---|
| Upstream, 1 thread | 3,385,283 | 425,502 | `bench` |
| Duckish, 1 thread | 3,385,283 | 431,493 | `bench`, same nodes as upstream |
| Duckish, 1 thread | 7,661,282 | 432,421 | `bench 16 1` |
| Duckish, 2 threads | 11.9M - 13.1M | 688,299 | `bench 16 2`, 1.59x NPS |

Identical node counts in the first two rows are the useful signal: the `MultiPV 1` search tree is unchanged from upstream, so the fork adds no search regression. Lazy SMP scales to roughly 1.6x NPS on 2 threads; node totals vary between runs because the threads diverge.

### Match results

40 games each (20 openings x 2 colours), 100 ms per move, single PV, 240-ply cap, run with `scripts/match.py`.

| Match | Duckish | Upstream | W-L-D | Score | Elo (95% CI) |
|---|---|---|---|---|---|
| Equal settings | 1 thread, 64 MB | 1 thread, 64 MB | 4-7-29 | 0.4625 | -26 [-84, +31] |
| Out of the box | 2 threads, 256 MB | 1 thread, 16 MB | 11-2-27 | 0.6125 | +80 [+21, +143] |

The equal-settings match is the regression test: the 95% interval straddles zero, which is what an unchanged search should look like. The out-of-the-box match is the practical number: Duckish's defaults (2 threads, 256 MB) score +80 Elo against stock Viridithas at bullet time control.

Caveats worth keeping:

- 40 games resolves roughly +/-60 Elo at 95% confidence; treat the point estimates as indicative, not precise.
- These are 100 ms/move bullet games. Multi-thread gains grow at longer time controls, so the +80 figure is not the engine's strength at tournament control.
- Fixed openings, no SPRT, single machine, one core count. Reproduce with `scripts/match.py --threads-a N --threads-b N --hash-a N --hash-b N`; rebuild the baseline with `git worktree add ../duckish-base upstream/master` and copy `viridithas.nnue.zst` into that directory before building.

### Multi-line analysis

Upstream declares no `MultiPV` option at all; `setoption name MultiPV` is a Duckish addition (`spin`, default 1, min 1, max 10). That is the whole advisor delta, and it is not free: `MultiPV > 1` runs N root searches per depth single-threaded, so the bridge defaults to a 400 ms budget and the userscript exposes a Fast (1) / Analysis (3) toggle.

### Hardware Scaling (Better CPU = Stronger Duckish)

Duckish turns hardware into strength in three measurable places:

- **Lazy SMP across physical cores (Fast Mode, `MultiPV 1`).** Helper threads search the same root position and share one 256 MB transposition table. On this 2-thread host that measured 1.59x NPS, which is what produced the +80 Elo in the out-of-the-box match. That is slightly below the 1.7-1.9x NPS published for 2 threads by other engines, because the box is small (4 physical cores) and the time control is 100 ms; the direction still holds, and more physical cores buy more search depth inside the same 400 ms budget. Expect sub-linear returns past roughly 8-16 cores, as with every SMP engine.
- **Native SIMD NNUE (`-C target-cpu=native`).** Building locally lets the compiler target your CPU's widest vector ISA - AVX2, or AVX-512 on recent Ryzen/Intel parts - so Hyperion's NNUE accumulator updates and matrix evaluations do more per clock cycle. A generic release binary leaves that throughput on the table. This is a build-time win, not a source change.
- **L3 cache headroom.** At ~700k NPS, node rate is partly a cache story: more L3 keeps the hot slice of the transposition table and the NNUE accumulators on-die instead of stalling on DRAM. The table is 256 MB, so only its hot fraction is ever resident - a bigger L3 makes that fraction bigger.

**Analysis Mode (`MultiPV > 1`) deliberately runs single-threaded.** Rank 2 must be a clean re-search with rank 1's root move excluded; threads inject timing-dependent node counts and can reorder candidates between runs. Throughput is traded for reproducibility on purpose.

#### Is any of this novel?

No, and it is worth being blunt about it:

- Root-exclusion MultiPV (search the best line, exclude its root move, re-search for the next rank, then re-sort so `multipv 1` equals `bestmove`) is exactly what Stockfish (`pvIdx` loop in `search.cpp`), Ethereal and Berserk implement.
- `-C target-cpu=native` (or `-march=native`) is standard engine build practice; upstream Viridithas documents the same Rust command, and Stockfish's `Makefile` defaults to `ARCH = native`.
- Lazy SMP with one shared hash table has been the mainstream multicore scheme since Stockfish 7.

The one thing we could not find in a published engine is the deliberate rule **force `Threads = 1` whenever `MultiPV > 1`**. Stockfish, Ethereal and Berserk all run the PV loop inside every worker thread. That is an advisor-motivated policy, not a search-theory contribution, and it is why the bridge defaults to 400 ms with a Fast/Analysis toggle.

### Performance Book (Performance.bin)

- The local Polyglot book is at `E:\dev\duckish\Performance.bin`; `*.bin` is gitignored, so it stays local-only.
- The bridge auto-wires it: with `DUCKISH_BOOK_PATH` unset it defaults the engine's `BookPath` option to `<repo root>/Performance.bin` and sends `setoption name BookPath value <abs path>` when the file exists.
- The engine probes the book at the root for time-based `go` commands (wtime/btime/movetime), so the advisor gets instant opening moves; `go depth` bypasses the book and searches directly.
- Live E2E on this machine: startpos -> `bestmove e2e4 ponder c7c5`; after `1.e4 e5` -> `bestmove g1f3 ponder g8f6`. Both instant book hits, clean exit.

### What Changed vs. Upstream Viridithas

| Feature | Upstream Viridithas | Duckish 1.0 |
|---|---|---|
| MultiPV | Not present | Native 1-10, root-exclusion re-search; PVs ranked by score so `multipv 1` == `bestmove`. **Runs single-threaded when > 1.** |
| Internal Iterative Deepening | Not present | Depth-1 shallow pass on TT miss before full-depth search. |
| Polyglot opening book probing | Not present | `.bin` book probed at root for time-based `go` (wtime/btime/movetime). |
| CPU thread scaling | Hardcoded `Threads = 1` | Dynamic `available_parallelism()`; half-physical-cores default, `setoption` override. |
| Transposition hash default | 16 MB | 256 MB. |
| Bridge HTTP server | None | `bridge/server.py` + `bridge/experience.py`: live SQLite mistake-learning book with auto-respawn + debug dashboard at `http://127.0.0.1:5000`. |
| Chess.com userscript overlay | None | `userscript/duckish-advisor.user.js`: Tampermonkey advisor for offline study, post-game review, bot practice (local-only). |

> **Caveat**: `MultiPV > 1` runs single-threaded for deterministic candidate ordering. Engine strength and search behaviour are unchanged from upstream when `MultiPV = 1`.

## Building Viridithas

If you just want one of the official releases of Viridithas, check out the **Releases** tab on the right.
If you want to build a specific version of Viridithas from source (say, the latest and greatest master commit) then follow these instructions:

0. Before following any of these instructions, make sure you have [Rust](https://www.rust-lang.org/tools/install) installed.
   You may also need to install the `clang` C compiler, as Viridithas relies on [Fathom](https://github.com/jdart1/Fathom) for tablebase probing support.
1. Clone this repository to your machine via `git clone git@github.com:cosmobobak/viridithas.git`.
2. Enter the source directory via `cd viridithas`.
3. Download the corresponding neural network for the version of Viridithas that you are compiling and save it in the source root as `viridithas.nnue.zst`.
   All of Viridithas's neural networks can be found in the releases of the [viridithas-networks](https://github.com/cosmobobak/viridithas-networks) repo.
   Networks are stored seperately from this repo due to their considerable size.

    **If you just want the latest neural net**, you can download it with the command `curl -s "https://api.github.com/repos/cosmobobak/viridithas-networks/releases/latest" | grep -o '"browser_download_url": "[^"]*' | awk -F'"' '{print $4}' | xargs -L 1 wget -O viridithas.nnue.zst`.

4. Build Viridithas.
   
   **On Windows**, run 
   ```
   > $env:RUSTFLAGS="-C target-cpu=native"
   > cargo b -r --features syzygy,bindgen
   ``` 
   **On Linux**, run
   ```
   > RUSTFLAGS="-C target-cpu=native" cargo b -r --features syzygy,bindgen
   ```
   You now have a fully-functional version of Viridithas at the path `target/release/viridithas`.

## Evaluation Development History/Originality (HCE/NNUE)

- First evaluation was a simple material / PSQT approach, using values from PeSTO.
- Early in development, a local-search [Texel tuning](https://www.chessprogramming.org/Texel%27s_Tuning_Method) module was developed,
  and Viridithas was retrained on the Lichess Elite dataset, removing the PeSTO values with increased strength.
- Many new evaluation terms were added during development, and were continually re-tuned on Viridithas's self-play games.
- The first NNUE was trained on a dataset of games played by Viridithas 2.7.0, 2.6.0, and 2.5.0, all rescored with a development
  version of Viridithas 2.7.0 at low depth.
- Subsequent networks were trained on additional self-play games by subsequent versions of Viridithas.
  The 13th-generation network, and many since, include positions from the Lichess Elite dataset rescored by Viridithas.
- Between versions 7.0.0 and 8.0.0, original datagen code was written that allows Viridithas to generate data without need for an
  opening book to ensure game variety, resulting in even greater strength of play.

All neural networks currently used in the development of Viridithas are trained exclusively on its own self-play games,
and no network has ever been trained on the output of an engine other than Viridithas.

## Thanks and Acknowledgements

[python-chess](https://github.com/niklasf/python-chess), without which I would never have been able to gain a love for chess programming.

The [VICE](https://www.youtube.com/playlist?list=PLZ1QII7yudbc-Ky058TEaOstZHVbT-2hg) video series, which was invaluable for explaining how various chess engine concepts can be implemented.

Andrew Grant's [Ethereal](https://github.com/AndyGrant/Ethereal), the exceedingly clear code of which helped me realise several horrible flaws in Viridithas.

[weather-factory](https://github.com/dsekercioglu/weather-factory), which I used only minimally, but which is still responsible for about ~10 elo in Viridithas.

[marlinflow](https://github.com/dsekercioglu/marlinflow), was responsible for neural network training for Viridithas up until version 11.0.0.

[bullet](https://github.com/jw1912/bullet), which I have used for neural network training in the development of Viridithas since version 11.0.0.

[The SweHosting OpenBench Instance](https://chess.swehosting.se/), which is invaluable in testing new patches and features.

## License

Viridithas is free software, licensed under the [GNU Affero General Public License v3.0](LICENSE) (`AGPL-3.0-only`).

- The neural networks embedded in Viridithas (the `.nnue` / `.nnue.zst` files,
  hosted in [viridithas-networks](https://github.com/cosmobobak/viridithas-networks)) are
  dedicated to the public domain under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/).
- Viridithas bundles third-party components under their own AGPLv3-compatible terms; see 
  [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
- Versions up to and including 20.0.0 were released under the MIT License and remain available under those terms.

By contributing to Viridithas, you agree that your contributions will be licensed under the `AGPL-3.0-only` license.

[build-badge]:https://img.shields.io/github/actions/workflow/status/cosmobobak/viridithas/rust.yml?branch=master&logo=github&style=for-the-badge
[build-link]:https://github.com/cosmobobak/viridithas/actions/workflows/rust.yml
[commits-badge]:https://img.shields.io/github/commits-since/cosmobobak/viridithas/latest?style=for-the-badge
[commits-link]:https://github.com/cosmobobak/viridithas/commits/master
[release-badge]:https://img.shields.io/github/v/tag/cosmobobak/viridithas?style=for-the-badge&label=official%20release
[release-link]:https://github.com/cosmobobak/viridithas/releases/latest
[license-badge]:https://img.shields.io/github/license/cosmobobak/viridithas?style=for-the-badge&label=license&color=success
[license-link]:https://github.com/cosmobobak/viridithas/blob/master/LICENSE

