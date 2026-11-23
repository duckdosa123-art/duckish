#!/usr/bin/env python3
"""Unit tests for LichessBot.save_match_config in lichess/runner.py.

Verifies the save-only settings path (used by the dashboard's /api/settings
route) persists changes live WITHOUT starting auto-match, and crucially
preserves the current accept_challenges state (incoming payloads omit it).

Style mirrors bridge/test_challenge_rules.py: plain functions returning bool,
printed PASS/FAIL, a main() runner, sys.path insertion for the ``bridge``
package, exit code 0 = all passed, 1 = at least one failed. No pytest.

The bot is built via ``object.__new__(LichessBot)`` so __init__ never runs.
A tiny stub config.save() records calls instead of touching settings.json.

Run from anywhere:

    python bridge/test_settings_save.py
"""

import os
import sys
from typing import List

# Make the bridge package importable regardless of the caller's cwd.
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

from lichess.runner import LichessBot
from lichess.config import MatchConfig


class StubState:
    def __init__(self):
        self.logs: List[str] = []

    def log(self, msg: str) -> None:
        self.logs.append(msg)


class StubClient:
    pass


def _config_with_save(accept_challenges=False, rated=True):
    """Build a MatchConfig whose save() is a no-op (never touches disk)."""
    cfg = MatchConfig(elo_min=0, elo_max=0, rated=rated,
                      accept_challenges=accept_challenges,
                      allowed_speeds="blitz,rapid")
    cfg.save = lambda: None  # type: ignore[assignment]
    return cfg


def make_bot(**overrides):
    """Build a LichessBot via object.__new__ (never start threads/network)."""
    bot = object.__new__(LichessBot)
    bot.client = StubClient()
    bot.state = StubState()
    bot._username = "duckish"
    bot._accept_challenges = True
    bot._current_game_id = None
    bot._auto_match_active = False
    bot._auto_match_rated = True
    bot.match_config = _config_with_save()
    for k, v in overrides.items():
        setattr(bot, k, v)
    return bot


def assert_eq(label: str, got, want) -> bool:
    if got != want:
        print(f"FAIL {label}: expected {want!r}, got {got!r}")
        return False
    return True


# ===========================================================================
# TESTS
# ===========================================================================

def s01_preserves_accept_challenges():
    """accept_challenges=True is preserved after a save that omits it."""
    bot = make_bot()
    bot.match_config = _config_with_save(accept_challenges=True)
    bot.save_match_config({"elo_min": 2700, "elo_max": 2900})
    ok = True
    ok &= assert_eq("s01 elo_min", bot.match_config.elo_min, 2700)
    ok &= assert_eq("s01 elo_max", bot.match_config.elo_max, 2900)
    ok &= assert_eq("s01 accept_challenges preserved",
                    bot.match_config.accept_challenges, True)
    return ok


def s02_does_not_start_auto_match():
    """save_match_config must NOT flip _auto_match_active to True."""
    bot = make_bot()
    bot._auto_match_active = False
    bot.save_match_config({"elo_min": 2600})
    return assert_eq("s02 auto_match_active stays False",
                     bot._auto_match_active, False)


def s03_updates_rated():
    """A saved rated=False is applied to match_config.rated."""
    bot = make_bot()
    bot.save_match_config({"rated": False})
    return assert_eq("s03 rated updated", bot.match_config.rated, False)


def s04_invalid_payload_ignored():
    """A non-dict payload does not crash and leaves config unchanged."""
    bot = make_bot()
    before = bot.match_config.to_dict()
    bot.save_match_config("not-a-dict")  # type: ignore[arg-type]
    after = bot.match_config.to_dict()
    ok = True
    ok &= assert_eq("s04 config unchanged", after, before)
    return ok


# ---- Runner ----------------------------------------------------------------

def main() -> int:
    tests = [
        s01_preserves_accept_challenges,
        s02_does_not_start_auto_match,
        s03_updates_rated,
        s04_invalid_payload_ignored,
    ]

    failures = 0
    passed = 0
    for test in tests:
        try:
            ok = test()
            if ok is False:
                print(f"FAIL {test.__name__}")
                failures += 1
            else:
                print(f"PASS {test.__name__}")
                passed += 1
        except Exception as e:
            import traceback
            print(f"FAIL {test.__name__}: raised {e}")
            traceback.print_exc()
            failures += 1
    print(f"\n{passed} passed, {failures} failed, {len(tests)} total")
    if failures:
        print(f"{failures} test(s) FAILED")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())