import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("RIOT_API_KEY")

REGIONS = {
    "EUW": "euw1",
    "EUNE": "eun1",
    "NA": "na1",
    "KR": "kr",
    "BR": "br1",
    "JP": "jp1",
    "OCE": "oc1",
    "LAN": "la1",
    "LAS": "la2",
    "TR": "tr1",
    "RU": "ru"
}

# The routing regions for match-v5 endpoints
ROUTING_REGIONS = {
    "EUW": "europe",
    "EUNE": "europe",
    "TR": "europe",
    "RU": "europe",
    "NA": "americas",
    "BR": "americas",
    "LAN": "americas",
    "LAS": "americas",
    "KR": "asia",
    "JP": "asia",
    "OCE": "sea"
}

TIERS = ["challenger", "grandmaster", "master"]

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "riot_dataset.db")
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
