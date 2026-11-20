#!/usr/bin/env python3
"""
Official Lichess Bot API client for Duckish (thin CLI entry point).

The implementation lives in the ``lichess`` package; this module only wires up
imports and the command-line interface.
"""
import os
import sys
import argparse

# Add bridge directory to path for local imports (experience, lichess package).
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

from lichess import LichessBot
from lichess.config import load_dotenv_token, DASHBOARD_HOST, DASHBOARD_PORT


def main() -> None:
    parser = argparse.ArgumentParser(description="Duckish Official Lichess Bot")
    parser.add_argument("--token", help="Lichess Bot API token (or set LICHESS_TOKEN env var)")
    parser.add_argument("--upgrade", action="store_true", help="Upgrade account to BOT (one-time, irreversible)")
    parser.add_argument("--challenge", metavar="USERNAME", help="Send a 3+2 challenge to a user")
    parser.add_argument("--casual", action="store_true", help="Make challenge casual (default: rated)")
    parser.add_argument("--auto-match", action="store_true", help="Enable autonomous bot matchmaking")
    parser.add_argument("--open-browser", action="store_true", help="Open dashboard in browser on start")
    args = parser.parse_args()

    token = args.token or os.environ.get("LICHESS_TOKEN") or load_dotenv_token()
    if not token:
        print("Error: Lichess token required. Set LICHESS_TOKEN env var, use --token, or add to .env", flush=True)
        sys.exit(1)

    bot = LichessBot(token)

    if args.upgrade:
        bot.upgrade_account()
        return

    if args.challenge:
        bot.challenge_user(args.challenge, rated=not args.casual)
        return

    if args.auto_match:
        bot.start_auto_match()

    if args.open_browser:
        import webbrowser
        webbrowser.open(f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")

    try:
        bot.run()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
    finally:
        bot.shutdown()


if __name__ == "__main__":
    main()