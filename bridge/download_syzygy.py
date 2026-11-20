#!/usr/bin/env python3
"""
Download Syzygy endgame tablebases (3-4-5 man, WDL + DTZ) into ./syzygy.

Pure stdlib (urllib.request, os, sys, time). Files are streamed to disk in
chunks so memory stays flat; the full 3-4-5 man set is ~938 MB / ~290 files.

Source: https://tablebase.lichess.ovh/tables/standard/download.txt
(newline-separated URLs)

The manifest also lists 6-man (~149 GB) and 7-man (~16.7 TB) tablebases.
Those are deliberately filtered out - see filter_tb_urls() - so only the
~938 MB 3-4-5 man set (145 WDL + 145 DTZ files) is downloaded.

Usage:
    python bridge/download_syzygy.py

The Lichess bot auto-detects the syzygy/ folder and sets the engine's
SyzygyPath option. This requires the engine to be built with tablebase
support: `cargo build --release --features syzygy` (or `make final-release`).
"""
import os
import sys
import time
import urllib.request

# Repo root /syzygy (this file lives in bridge/)
TB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "syzygy")

MANIFEST_URL = "https://tablebase.lichess.ovh/tables/standard/download.txt"
CHUNK_SIZE = 1 << 16  # 64 KiB streaming write
TIMEOUT_S = 30        # per-request timeout
MAX_ATTEMPTS = 3      # retries per file


def fetch_manifest():
    """Fetch and return the list of tablebase URLs from the manifest."""
    req = urllib.request.Request(MANIFEST_URL, headers={"User-Agent": "duckish-syzygy-downloader/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def filter_tb_urls(urls):
    """Keep only the 3-4-5 man WDL (.rtbw) and DTZ (.rtbz) files.

    The Lichess manifest also lists 6-man (~149 GB) and 7-man (~16.7 TB)
    tables - those must be excluded. We only want the ~938 MB 3-4-5 set.
    """
    out = []
    for u in urls:
        low = u.lower()
        if ("/3-4-5-wdl/" in low or "/3-4-5-dtz/" in low) and low.endswith((".rtbw", ".rtbz")):
            out.append(u)
    return out


def download_file(url, dest):
    """Stream a single file to dest with retries. Returns True if downloaded."""
    name = os.path.basename(url)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "duckish-syzygy-downloader/1.0"})
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp, open(dest, "wb") as out:
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    out.write(chunk)
            return True
        except Exception as e:  # noqa: BLE001 - report and retry any transport error
            # Remove any partial file so a retry starts clean.
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
            if attempt < MAX_ATTEMPTS:
                print(f"  retry {attempt}/{MAX_ATTEMPTS} for {name}: {e}")
                time.sleep(2 * attempt)
            else:
                print(f"  FAILED {name} after {MAX_ATTEMPTS} attempts: {e}")
    return False


def main():
    os.makedirs(TB_DIR, exist_ok=True)

    print(f"Fetching tablebase manifest: {MANIFEST_URL}")
    try:
        all_urls = fetch_manifest()
        urls = filter_tb_urls(all_urls)
        print(
            f"Manifest has {len(all_urls)} entries; keeping {len(urls)} (3-4-5 man only), "
            f"excluding {len(all_urls) - len(urls)} 6/7-man entries."
        )
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: could not fetch manifest: {e}")
        return 1

    if not urls:
        print("ERROR: no .rtbw/.rtbz URLs found in manifest.")
        return 1

    total = len(urls)
    downloaded = 0
    skipped = 0
    failed = 0
    bytes_written = 0

    print(f"Found {total} tablebase files. Target directory: {TB_DIR}\n")

    for i, url in enumerate(urls, start=1):
        name = os.path.basename(url)
        dest = os.path.join(TB_DIR, name)

        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            print(f"[{i}/{total}] skip (exists): {name}")
            skipped += 1
            bytes_written += os.path.getsize(dest)
            continue

        print(f"[{i}/{total}] Downloading: {name}")
        before = os.path.getsize(dest) if os.path.isfile(dest) else 0
        if download_file(url, dest):
            downloaded += 1
            if os.path.isfile(dest):
                bytes_written += os.path.getsize(dest) - before
        else:
            failed += 1

    size_mb = bytes_written / (1024.0 * 1024.0)
    print("\n=== Summary ===")
    print(f"Files downloaded: {downloaded}")
    print(f"Files skipped (already present): {skipped}")
    print(f"Files failed: {failed}")
    print(f"Total size on disk: {size_mb:.1f} MB")

    print("\nSet SyzygyPath automatically - the bot detects the syzygy/ folder.")
    print("Rebuild the engine with `cargo build --release --features syzygy`")
    print("if tablebases don't engage.")

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())