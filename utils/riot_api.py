"""Shared Riot API transport for every collector."""

import datetime
import math
from email.utils import parsedate_to_datetime
import time
from typing import Any, Optional

import requests

from config.settings import API_KEY
from utils.rate_limiter import limiter
from utils.logger import get_logger

logger = get_logger("riot_api")
HEADERS = {"X-Riot-Token": API_KEY}


class RiotAPIError(RuntimeError):
    """A safe, persistable API failure without credentials or response bodies."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _retry_after(value: Optional[str], default: float = 10.0) -> float:
    """Accept Retry-After delay seconds or the HTTP-date form."""
    if value is None:
        return default
    try:
        delay = float(value)
        return max(0.0, delay) if math.isfinite(delay) else default
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
            return max(0.0, retry_at.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return default


def make_request(url: str, max_retries: int = 3,
                 raise_on_error: bool = False) -> Any:
    """GET through the existing limiter; optionally report a classified error.

    Legacy callers retain the original None-on-failure behavior. New collectors
    can request an exception so failed downloads never look like empty results.
    Logs and exceptions intentionally exclude response bodies and request errors,
    which could include credentials or other sensitive request information.
    """
    error = RiotAPIError("No API request attempted")
    attempts = max(1, max_retries)
    for attempt in range(attempts):
        limiter.wait()
        delay = 5.0
        try:
            response = requests.get(url, headers=HEADERS, timeout=10)
        except requests.exceptions.Timeout:
            error = RiotAPIError("Riot API request timed out")
        except requests.exceptions.ConnectionError:
            error = RiotAPIError("Riot API connection failed")
        except requests.exceptions.RequestException:
            error = RiotAPIError("Riot API request failed")
        else:
            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                except ValueError:
                    error = RiotAPIError("Riot API returned invalid JSON", status)
                    break
                if not isinstance(data, (dict, list)):
                    error = RiotAPIError("Riot API returned an invalid JSON payload", status)
                    break
                return data
            error = RiotAPIError("Riot API HTTP {}".format(status), status)
            if status == 429:
                delay = _retry_after(response.headers.get("Retry-After"))
            elif status < 500:
                # Authentication/authorization failures and 404s do not benefit
                # from immediately repeating the same request.
                break
        if attempt + 1 < attempts or error.status_code == 429:
            # The next match shares this same key: respect the server cooldown
            # even when this request has exhausted its own attempts.
            logger.warning("%s; waiting %.2fs before continuing", error, delay)
            time.sleep(delay)

    logger.error("%s", error)
    if raise_on_error:
        raise error
    return None


def get_league_players(region_code, tier):
    url = f"https://{region_code}.api.riotgames.com/lol/league/v4/{tier}leagues/by-queue/RANKED_SOLO_5x5"
    data = make_request(url)
    if isinstance(data, dict):
        return data.get("entries", [])
    return []


def get_puuid(region_code, summoner_id):
    url = f"https://{region_code}.api.riotgames.com/lol/summoner/v4/summoners/{summoner_id}"
    data = make_request(url)
    if isinstance(data, dict):
        return data.get("puuid")
    return None


def get_match_ids(routing_region, puuid, start=0, count=100, queue=420, start_time=None,
                  raise_on_error=False, end_time=None):
    """Fetch Ranked Solo/Duo IDs, by default starting on January 1, 2026."""
    if start_time is None:
        start_time = int(datetime.datetime(2026, 1, 1).timestamp())
    url = f"https://{routing_region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
    params = []
    for name, value in (("start", start), ("count", count),
                        ("queue", queue), ("startTime", start_time), ("endTime", end_time)):
        if value is not None:
            params.append(f"{name}={value}")
    if params:
        url += "?" + "&".join(params)
    data = make_request(url, raise_on_error=raise_on_error)
    if raise_on_error and not isinstance(data, list):
        raise RiotAPIError('Riot API returned an invalid match history', 200)
    return data if isinstance(data, list) else []


def get_match_details(routing_region: str, match_id: str,
                      raise_on_error: bool = False) -> Any:
    url = f"https://{routing_region}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    return make_request(url, raise_on_error=raise_on_error)


def get_match_timeline(routing_region: str, match_id: str) -> Any:
    """Use the shared transport, reporting failures to the resumable collector."""
    url = f"https://{routing_region}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
    return make_request(url, raise_on_error=True)
