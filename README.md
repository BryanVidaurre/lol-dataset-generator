<div align="center">
  
# 🎮 League of Legends Match Dataset Generator for Machine Learning & XGBoost

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![API](https://img.shields.io/badge/Riot%20Games-API-red)](https://developer.riotgames.com/)
[![ML Ready](https://img.shields.io/badge/Machine%20Learning-Ready-brightgreen)]()

An automated data collection tool using the official Riot Games API to generate a massive, clean, and ML-ready dataset of High-Elo (Challenger, Grandmaster, Master) Ranked Solo/Duo matches. 
Perfect for Data Science, XGBoost win prediction models, and draft analysis!

</div>

---

## 📖 Overview

This tool was built to solve the lack of massive, up-to-date League of Legends datasets. It automatically fetches top players across all regions, harvests their match history, and extracts the game details into a clean, flat CSV file. 

The dataset is specifically structured for **Machine Learning** tasks, such as predicting the winning team based solely on the draft (champion synergies, counters, and average team elo).

### 🌟 Features
- **Massive Scale**: Capable of harvesting 800,000+ matches across all Riot regions.
- **Resumable Collection**: Built-in SQLite database tracks progress. If your API key expires or your internet drops, you can restart without losing data or wasting API calls.
- **ML-Ready CSV**: Flattens nested JSON responses into a single CSV row per match (Champions by role, Team Tiers, Game Duration, Patch, Win/Loss).
- **Rate-Limit Safe**: Thread-safe rate limiter strictly adheres to Riot's Developer API limits.

---

## 📊 Dataset Format (`dataset.csv`)

The generated CSV is structured for immediate use in frameworks like Pandas, Scikit-Learn, or XGBoost.

| Column | Description |
|---|---|
| `match_id` | Unique Riot Match ID |
| `patch` | Game version (e.g., "14.2") |
| `game_duration` | Match duration in seconds |
| `game_creation` | Unix timestamp of match start |
| `t1_top` … `t1_support` | Team 1 champions by role (Top, Jgl, Mid, Bot, Sup) |
| `t1_avg_tier` | Team 1 average tier score (1=Master, 2=GM, 3=Challenger) |
| `t2_top` … `t2_support` | Team 2 champions by role |
| `t2_avg_tier` | Team 2 average tier score |
| `win_team1` | Boolean: `True` if Team 1 won |

---

## ⚙️ Installation

```bash
# 1. Clone the repository
git clone https://github.com/mehdbenguiza/lol-dataset-generator.git
cd lol-dataset-generator/lol_dataset

# 2. Create a virtual environment
python -m venv venv

# 3. Activate the environment
# On Windows:
venv\Scripts\activate
# On Linux/Mac:
source venv/bin/activate

# 4. Install dependencies
pip install requests pandas tqdm python-dotenv

# 5. Setup your API key
cp .env.example .env
# Edit .env and paste your Riot Developer API Key
```

---

## 🚀 Usage Guide

The collection process is split into 3 independent phases.

### Phase 1: Collect High-Elo Players
```bash
python collector\1_collect_players.py
```
Scrapes all Challenger, Grandmaster, and Master players across all regions and stores their PUUIDs in the local SQLite database.

### Phase 2: Collect Match IDs
```bash
python collector\2_collect_matches.py
```
Iterates through all collected players and fetches their historical Ranked Solo/Duo Match IDs for the current season. Duplicates (players in the same match) are automatically ignored.

### Phase 3: Extract Details to CSV
```bash
python collector\3_collect_details.py
```
Downloads the detailed telemetry for each Match ID, calculates the average team tiers, assigns champions to their exact roles, and appends the flattened data to `data/dataset.csv`.

> ✅ **Tip:** You can stop any phase using `Ctrl+C` and restart it later. The SQLite database ensures it picks up exactly where it left off.

---

## ⚖️ Riot Games Compliance

This project was built in strict compliance with the [Riot Games API Terms of Use](https://developer.riotgames.com/terms):
- ✅ Uses only official and documented Riot API endpoints (Match-V5, League-V4).
- ✅ Rate-limits are strictly enforced.
- ✅ Personal API keys are protected and never hardcoded (using `.env`).
- ✅ Non-commercial, open-source project intended for educational ML research.

---

## 🔑 Keywords for Search
`league-of-legends` `lol-dataset` `riot-api` `machine-learning` `xgboost` `python` `data-science` `esports-analytics` `match-prediction` `champion-draft` `ranked-solo-duo` `league-v4` `match-v5`
