#!/usr/bin/env python3
"""
Minimal Lichess Bot API client for Duckish.
Pure stdlib (json, socket, urllib, typing).
"""
import json
import socket
import urllib.request
import urllib.error

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

from typing import Any, Dict, List, Optional
from http.client import HTTPResponse

from .config import LICHESS_API


class LichessClient:
    """Minimal Lichess Bot API client using stdlib urllib."""

    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/x-ndjson",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        stream: bool = False,
        timeout: Optional[float] = None,
    ) -> HTTPResponse:
        url = f"{LICHESS_API}{endpoint}"
        body = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=body, method=method, headers=self.headers)
        # Default timeouts: 3.0s for bot-list fetch, 10s for startup calls, 30s otherwise
        if timeout is None:
            if endpoint == "/bot/online":
                timeout = 5.0
            elif endpoint in ("/account", "/upgrade"):
                timeout = 10.0
            else:
                timeout = 30.0
        return urllib.request.urlopen(req, timeout=timeout)

    def upgrade_account(self) -> Dict[str, Any]:
        """POST /upgrade - convert account to bot (per official docs)."""
        return self._request("POST", "/upgrade", {}, timeout=10.0)

    def stream_events(self):
        """GET /api/stream/event - NDJSON event stream."""
        if not HAS_REQUESTS:
            raise RuntimeError("requests library not installed - streaming unavailable")
        url = f"{LICHESS_API}/stream/event"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/x-ndjson",
        }
        resp = requests.get(url, headers=headers, stream=True, timeout=(10, 30))
        resp.raise_for_status()
        return resp

    def stream_game(self, game_id: str):
        """GET /api/bot/game/stream/{gameId} - NDJSON game stream."""
        if not HAS_REQUESTS:
            raise RuntimeError("requests library not installed - streaming unavailable")
        url = f"{LICHESS_API}/bot/game/stream/{game_id}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/x-ndjson",
        }
        resp = requests.get(url, headers=headers, stream=True, timeout=(10, 300))
        resp.raise_for_status()
        return resp

    def accept_challenge(self, challenge_id: str) -> Dict[str, Any]:
        resp = self._request("POST", f"/challenge/{challenge_id}/accept")
        return json.loads(resp.read().decode("utf-8"))

    def decline_challenge(self, challenge_id: str, reason: str = "generic") -> Dict[str, Any]:
        resp = self._request("POST", f"/challenge/{challenge_id}/decline", {"reason": reason})
        return json.loads(resp.read().decode("utf-8"))

    def cancel_challenge(self, challenge_id: str) -> Dict[str, Any]:
        resp = self._request("POST", f"/challenge/{challenge_id}/cancel")
        return json.loads(resp.read().decode("utf-8"))

    def make_move(self, game_id: str, move_uci: str) -> Dict[str, Any]:
        try:
            resp = self._request("POST", f"/bot/game/{game_id}/move/{move_uci}")
            return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            return {"error": f"HTTP {e.code}: {body}"}
        except Exception as e:
            return {"error": str(e)}

    def challenge_user(
        self,
        username: str,
        clock_limit: int = 180,
        clock_increment: int = 2,
        variant: str = "standard",
        rated: bool = True,
    ) -> Dict[str, Any]:
        data = {
            "clock.limit": clock_limit,
            "clock.increment": clock_increment,
            "variant": variant,
            "rated": rated,
        }
        resp = self._request("POST", f"/challenge/{username}", data)
        return json.loads(resp.read().decode("utf-8"))

    def get_online_bots(self, own_username: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET /api/bot/online - fetch online bots via NDJSON stream with 5s timeout."""
        resp = self._request("GET", "/bot/online")
        bots: List[Dict[str, Any]] = []
        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    bot = json.loads(line)
                    if not isinstance(bot, dict) or ("username" not in bot and "id" not in bot):
                        continue
                    name = bot.get("username") or bot.get("id")
                    if own_username and name == own_username:
                        continue
                    perfs = bot.get("perfs", {})
                    blitz = perfs.get("blitz", {})
                    if "games" in blitz and blitz["games"] == 0:
                        continue
                    if bot.get("tosViolation") or bot.get("disabled"):
                        continue
                    bots.append(bot)
                    if len(bots) >= 30:
                        break  # Stop once we have up to 30 valid bots
                except json.JSONDecodeError:
                    continue
        except (socket.timeout, TimeoutError):
            pass  # Timeout is expected; return whatever we collected
        finally:
            try:
                resp.close()
            except Exception:
                pass
        return bots

    def get_account_info(self) -> Dict[str, Any]:
        """GET /api/account - fetch account info including perfs."""
        resp = self._request("GET", "/account", timeout=10.0)
        return json.loads(resp.read().decode("utf-8"))

    def resign_game(self, game_id: str) -> Dict[str, Any]:
        resp = self._request("POST", f"/bot/game/{game_id}/resign")
        return json.loads(resp.read().decode("utf-8"))