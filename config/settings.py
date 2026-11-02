import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

API_KEY = os.getenv("RIOT_API_KEY")

REGIONS = {
    "EUW": "euw1",
    "NA": "na1",
    "KR": "kr",
    "BR": "br1",
    "LAS": "la2",
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

DATA_DIR = os.path.abspath(os.getenv("RIOT_DATA_DIR", os.path.join(BASE_DIR, "data")))
DB_PATH = os.path.join(DATA_DIR, "riot_dataset.db")
LOGS_DIR = os.path.abspath(os.getenv("RIOT_LOGS_DIR", os.path.join(BASE_DIR, "logs")))

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
