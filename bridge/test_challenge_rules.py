#!/usr/bin/env python3
"""Unit tests for the incoming-challenge decision rules in lichess/runner.py.

Covers LichessBot._handle_challenge:

    * non-clock time controls (unlimited / correspondence) are declined
      INSTANTLY via the authoritative ``timeControl.type`` field
    * clock challenges in allowed speeds are accepted
    * the other decline paths (own challenge, disabled, already in game,
      unsupported variant, disallowed speed) still fire with the right reason
    * every decline reason is a VALID camelCase Lichess reason
      ("timeControl", not "timecontrol")

Style mirrors bridge/test_draw_logic.py: plain functions returning bool,
printed PASS/FAIL, a main() runner, sys.path insertion for the ``bridge``
package, exit code 0 = all passed, 1 = at least one failed. No pytest.

The bot is built via ``object.__new__(LichessBot)`` so __init__ (which starts
threads and opens the network/dashboard) never runs. Only the attributes
_handle_challenge reads are set.

Run from anywhere:

    python bridge/test_challenge_rules.py
"""

import os
import sys
from typing import Any, Dict, List

# Make the bridge package importable regardless of the caller's cwd.
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

from lichess.runner import LichessBot
from lichess.config import MatchConfig


# ---- Offline harness -------------------------------------------------------

class StubClient:
    """Records every accept/decline call with its arguments."""

    def __init__(self):
        self.decline_calls: List[Any] = []
        self.accept_calls: List[str] = []

    def decline_challenge(self, cid, reason):
        self.decline_calls.append((cid, reason))
        return {"ok": True}

    def accept_challenge(self, cid):
        self.accept_calls.append(cid)
        return {"ok": True}


class StubState:
    """Minimal BotState stand-in: _log_event calls self.state.log(msg)."""

    def __init__(self):
        self.logs: List[str] = []

    def log(self, msg: str) -> None:
        self.logs.append(msg)


class StubMatchConfig:
    """Fallback if MatchConfig(...) is ever not constructible with kwargs."""

    def allowed_speed_set(self):
        return {"blitz", "rapid"}


def make_bot(**overrides) -> LichessBot:
    """Build a LichessBot via object.__new__ (never start threads/network)."""
    bot = object.__new__(LichessBot)
    bot.client = StubClient()
    bot._username = "duckish"
    bot._accept_challenges = True
    bot._current_game_id = None
    bot.state = StubState()
    try:
        bot.match_config = MatchConfig(allowed_speeds="blitz,rapid")
    except TypeError:
        bot.match_config = StubMatchConfig()
    for k, v in overrides.items():
        setattr(bot, k, v)
    return bot


def challenge(tc: Dict[str, Any], speed: str = "rapid",
              variant: str = "standard", rated: bool = False,
              challenger: str = "someone", cid: str = "ch1") -> Dict[str, Any]:
    return {
        "id": cid,
        "challenger": {"id": challenger, "name": challenger},
        "variant": {"key": variant},
        "speed": speed,
        "rated": rated,
        "timeControl": tc,
    }


def assert_eq(label: str, got: Any, want: Any) -> bool:
    if got != want:
        print(f"FAIL {label}: expected {want!r}, got {got!r}")
        return False
    return True


# ===========================================================================
# TESTS
# ===========================================================================

def c01_unlimited_declined():
    """timeControl.type == 'unlimited' -> instant decline 'timeControl'."""
    bot = make_bot()
    bot._handle_challenge(challenge({"type": "unlimited"}, speed="correspondence"))
    ok = True
    ok &= assert_eq("c01 decline_calls", bot.client.decline_calls, [("ch1", "timeControl")])
    ok &= assert_eq("c01 accept_calls", bot.client.accept_calls, [])
    return ok


def c02_correspondence_declined():
    """timeControl.type == 'correspondence' -> instant decline 'timeControl'."""
    bot = make_bot()
    bot._handle_challenge(
        challenge({"type": "correspondence", "daysPerTurn": 2}, speed="correspondence")
    )
    ok = True
    ok &= assert_eq("c02 decline_calls", bot.client.decline_calls, [("ch1", "timeControl")])
    ok &= assert_eq("c02 accept_calls", bot.client.accept_calls, [])
    return ok


def c03_clock_accepted():
    """A clock rapid challenge from a human in an allowed speed -> accepted."""
    bot = make_bot()
    bot._handle_challenge(
        challenge({"type": "clock", "limit": 600, "increment": 0},
                  speed="rapid", challenger="human")
    )
    ok = True
    ok &= assert_eq("c03 accept_calls", bot.client.accept_calls, ["ch1"])
    ok &= assert_eq("c03 decline_calls", bot.client.decline_calls, [])
    return ok


def c04_bullet_respects_allowed():
    """Clock bullet is not in allowed speeds {blitz, rapid} -> decline 'timeControl'."""
    bot = make_bot()
    bot._handle_challenge(
        challenge({"type": "clock", "limit": 120, "increment": 0}, speed="bullet")
    )
    ok = True
    ok &= assert_eq("c04 decline_calls", bot.client.decline_calls, [("ch1", "timeControl")])
    ok &= assert_eq("c04 accept_calls", bot.client.accept_calls, [])
    return ok


def c05_own_challenge_ignored():
    """A challenge whose challenger id equals our username -> ignored."""
    bot = make_bot()
    bot._handle_challenge(
        challenge({"type": "clock", "limit": 600, "increment": 0},
                  speed="rapid", challenger="duckish")
    )
    ok = True
    ok &= assert_eq("c05 accept_calls", bot.client.accept_calls, [])
    ok &= assert_eq("c05 decline_calls", bot.client.decline_calls, [])
    return ok


def c06_disabled_declines_generic():
    """_accept_challenges == False -> decline 'generic'."""
    bot = make_bot(_accept_challenges=False)
    bot._handle_challenge(
        challenge({"type": "clock", "limit": 600, "increment": 0}, speed="rapid")
    )
    ok = True
    ok &= assert_eq("c06 decline_calls", bot.client.decline_calls, [("ch1", "generic")])
    ok &= assert_eq("c06 accept_calls", bot.client.accept_calls, [])
    return ok


def c07_already_in_game():
    """_current_game_id set -> decline 'later'."""
    bot = make_bot(_current_game_id="g1")
    bot._handle_challenge(
        challenge({"type": "clock", "limit": 600, "increment": 0}, speed="rapid")
    )
    ok = True
    ok &= assert_eq("c07 decline_calls", bot.client.decline_calls, [("ch1", "later")])
    ok &= assert_eq("c07 accept_calls", bot.client.accept_calls, [])
    return ok


def c08_variant_declined():
    """Clock chess960 (unsupported variant) -> decline 'variant'."""
    bot = make_bot()
    bot._handle_challenge(
        challenge({"type": "clock", "limit": 600, "increment": 0},
                  speed="rapid", variant="chess960")
    )
    ok = True
    ok &= assert_eq("c08 decline_calls", bot.client.decline_calls, [("ch1", "variant")])
    ok &= assert_eq("c08 accept_calls", bot.client.accept_calls, [])
    return ok


def c09_reason_is_camelcase():
    """Across every decline-producing path the reason is camelCase 'timeControl',
    never the invalid lowercase 'timecontrol'."""
    bot = make_bot()
    cases = [
        challenge({"type": "unlimited"}, speed="correspondence"),
        challenge({"type": "correspondence", "daysPerTurn": 2}, speed="correspondence"),
        challenge({"type": "clock", "limit": 120, "increment": 0}, speed="bullet"),
    ]
    for c in cases:
        bot._handle_challenge(c)
    reasons = [r for (_cid, r) in bot.client.decline_calls]
    ok = True
    ok &= assert_eq("c09 no 'timecontrol'", "timecontrol" in reasons, False)
    ok &= assert_eq("c09 all camelCase timeControl",
                    all(r == "timeControl" for r in reasons if r in ("timeControl", "timecontrol")),
                    True)
    return ok


# ---- Runner ----------------------------------------------------------------

def main() -> int:
    tests = [
        c01_unlimited_declined,
        c02_correspondence_declined,
        c03_clock_accepted,
        c04_bullet_respects_allowed,
        c05_own_challenge_ignored,
        c06_disabled_declines_generic,
        c07_already_in_game,
        c08_variant_declined,
        c09_reason_is_camelcase,
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