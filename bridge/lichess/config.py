#!/usr/bin/env python3
"""
Configuration constants and shared bot state for the Duckish Lichess bot.
Pure stdlib (os, threading, time, typing).
"""
import os
import json
import time
import threading
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List, Optional

# ---- Paths ------------------------------------------------------------------

# config.py lives at bridge/lichess/config.py -> repo root is three levels up.
_LICHESS_DIR = os.path.dirname(os.path.abspath(__file__))
_BRIDGE_DIR = os.path.dirname(_LICHESS_DIR)
REPO_ROOT = os.path.dirname(_BRIDGE_DIR)

ENGINE_PATH = os.path.join(REPO_ROOT, "target", "release", "duckish.exe")
DASHBOARD_PORT = 5001
DASHBOARD_HOST = "127.0.0.1"


def load_dotenv_token(path: str = os.path.join(REPO_ROOT, ".env")) -> Optional[str]:
    """Load LICHESS_TOKEN from a .env file at the given path."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "LICHESS_TOKEN":
                    # Strip surrounding quotes if present
                    value = value.strip()
                    if (value.startswith('"') and value.endswith('"')) or (
                        value.startswith("'") and value.endswith("'")
                    ):
                        value = value[1:-1]
                    return value
    except OSError:
        pass
    return None


# ---- Engine / API constants -------------------------------------------------

DEFAULT_HASH_MB = 256
CONTEMPT = 0  # 0 = neutral; positive contempt makes the engine decline draws
              # and grab material in equal endgames, which loses vs stronger bots.
DEFAULT_THREADS = max(1, (os.cpu_count() or 2) // 2)
BOOK_PATH = os.environ.get("DUCKISH_BOOK_PATH", "")
if not BOOK_PATH:
    BOOK_PATH = os.path.join(REPO_ROOT, "Performance.bin")

LICHESS_API = "https://lichess.org/api"
MOVE_OVERHEAD_MS = 100  # safety buffer so we never flag on network latency

# Supported time controls (standard variants only)
SUPPORTED_VARIANTS = {"standard"}
SUPPORTED_SPEEDS = {"bullet", "blitz", "rapid"}  # real-time only, no correspondence


# ---- Match settings (persisted to settings.json) ----------------------------

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


@dataclass
class MatchConfig:
    speed: str = "blitz"        # bullet | blitz | rapid
    limit_min: int = 3          # clock limit in minutes
    increment: int = 2          # increment in seconds
    rated: bool = True
    elo_min: int = 0            # 0 = no lower bound
    elo_max: int = 0            # 0 = no upper bound

    def to_dict(self):
        return {"speed": self.speed, "limit_min": self.limit_min,
                "increment": self.increment, "rated": self.rated,
                "elo_min": self.elo_min, "elo_max": self.elo_max}

    @classmethod
    def from_dict(cls, d):
        return cls(
            speed=str(d.get("speed", "blitz")),
            limit_min=int(d.get("limit_min", 3)),
            increment=int(d.get("increment", 2)),
            rated=bool(d.get("rated", True)),
            elo_min=int(d.get("elo_min", 0)),
            elo_max=int(d.get("elo_max", 0)),
        )

    @classmethod
    def load(cls):
        try:
            if os.path.isfile(SETTINGS_PATH):
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    return cls.from_dict(json.load(f))
        except Exception:
            pass
        return cls()

    def save(self):
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2)
        except Exception:
            pass


# ---- Shared bot state -------------------------------------------------------

class BotState:
    """Holds the dashboard state machine data with a reentrant lock.

    Uses an RLock because log() may be called while a caller already holds the
    lock (e.g. _set_phase updates fields then logs the detail message).
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.phase: str = "IDLE"
        self.detail: str = "Idle \u2014 click Start Auto-Match or challenge a bot"
        self.target_bot: Optional[str] = None
        self.challenge_id: Optional[str] = None
        self.active_game_url: Optional[str] = None
        self.accept_challenges: bool = True
        self.logs: List[Dict[str, Any]] = []

    def log(self, msg: str) -> None:
        entry = {"time": time.time(), "msg": msg}
        with self.lock:
            self.logs.append(entry)
            if len(self.logs) > 50:
                self.logs = self.logs[-50:]
        print(msg, flush=True)

    def snapshot(self) -> Dict[str, Any]:
        """Return a plain-dict copy of the state for JSON serialization."""
        with self.lock:
            return {
                "phase": self.phase,
                "detail": self.detail,
                "target_bot": self.target_bot,
                "challenge_id": self.challenge_id,
                "active_game_url": self.active_game_url,
                "logs": list(self.logs),
            }