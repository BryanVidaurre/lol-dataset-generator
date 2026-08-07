import requests
import time
from config.settings import API_KEY
from utils.rate_limiter import limiter
from utils.logger import get_logger

logger = get_logger("riot_api")

HEADERS = {
    "X-Riot-Token": API_KEY
}

def make_request(url, max_retries=3):
    """
    Makes an HTTP GET request to the Riot API, enforcing the rate limiter.
    Retries on 429 and 5xx errors.
    """
    for attempt in range(max_retries):
        limiter.wait() # Enforce local rate limits BEFORE sending request
        
        try:
            response = requests.get(url, headers=HEADERS, timeout=10)
            
            if response.status_code == 200:
                return response.json()
                
            elif response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 10))
                logger.warning(f"Got 429 Rate Limit from Riot API. Sleeping for {retry_after}s.")
                time.sleep(retry_after)
                continue
                
            elif response.status_code >= 500:
                logger.warning(f"Server error {response.status_code}. Retrying in 5s...")
                time.sleep(5)
                continue
                
            elif response.status_code == 404:
                # Not found is typically not retryable
                return None
                
            else:
                logger.error(f"Unexpected status code {response.status_code} on {url}: {response.text}")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Request exception: {e}")
            time.sleep(5)
            
    logger.error(f"Failed to fetch {url} after {max_retries} attempts.")
    return None

def get_league_players(region_code, tier):
    url = f"https://{region_code}.api.riotgames.com/lol/league/v4/{tier}leagues/by-queue/RANKED_SOLO_5x5"
    data = make_request(url)
    if data:
        return data.get("entries", [])
    return []

def get_puuid(region_code, summoner_id):
    url = f"https://{region_code}.api.riotgames.com/lol/summoner/v4/summoners/{summoner_id}"
    data = make_request(url)
    if data:
        return data.get("puuid")
    return None

import datetime

def get_match_ids(routing_region, puuid, start=0, count=100, queue=420, start_time=None):
    """
    Fetch match IDs for a player using Match V5 API.
    routing_region must be one of: europe, americas, asia, sea
    queue=420 is Ranked Solo/Duo.
    start_time is default to Jan 1st 2026.
    """
    if start_time is None:
        start_time = int(datetime.datetime(2026, 1, 1).timestamp())
        
    url = f"https://{routing_region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
    
    params = []
    if start is not None:
        params.append(f"start={start}")
    if count is not None:
        params.append(f"count={count}")
    if queue is not None:
        params.append(f"queue={queue}")
    if start_time is not None:
        params.append(f"startTime={start_time}")
        
    if params:
        url += "?" + "&".join(params)
        
    data = make_request(url)
    if data is not None:
        return data
    return []

def get_match_details(routing_region, match_id):
    url = f"https://{routing_region}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    return make_request(url)
