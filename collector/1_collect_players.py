import sys
import os

# Add parent directory to path so we can import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tqdm import tqdm
from config.settings import REGIONS, TIERS
from utils.riot_api import get_league_players, get_puuid
from utils.logger import get_logger
from database import db_manager

logger = get_logger("collector_1")

def is_summoner_processed(summoner_id):
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM players WHERE summoner_id = ?", (summoner_id,))
        return cursor.fetchone() is not None

def is_tier_finished(region_name, tier):
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM checkpoints WHERE task_name = 'collect_players' AND region = ? AND tier = ? AND last_processed_id = 'FINISHED'", (region_name, tier))
        return cursor.fetchone() is not None

def mark_tier_finished(region_name, tier):
    db_manager.save_checkpoint('collect_players', region_name, tier, 'FINISHED')

def main():
    logger.info("Starting Step 1: Player collection")
    
    for region_name, region_code in REGIONS.items():
        for tier in TIERS:
            if is_tier_finished(region_name, tier):
                logger.info(f"Skipping {region_name} - {tier} (Already finished)")
                continue
                
            logger.info(f"Fetching {tier} players for {region_name}...")
            players = get_league_players(region_code, tier)
            
            if not players:
                logger.warning(f"No players found for {region_name} - {tier}. Skipping.")
                continue
                
            logger.info(f"Found {len(players)} players in {region_name} {tier}.")
            
            new_players_count = 0
            
            # Use tqdm for progress bar
            for p in tqdm(players, desc=f"{region_name} - {tier}"):
                puuid = p.get("puuid")
                league_points = p.get("leaguePoints", 0)
                wins = p.get("wins", 0)
                losses = p.get("losses", 0)
                
                if not puuid:
                    continue
                    
                # We can directly use the PUUID, no need for the extra /summoner endpoint!
                db_manager.save_player(
                    puuid=puuid,
                    summoner_id=None,
                    region=region_name,
                    tier=tier.upper(),
                    league_points=league_points,
                    wins=wins,
                    losses=losses
                )
                new_players_count += 1
            
            logger.info(f"Finished {region_name} - {tier}. Added {new_players_count} new players.")
            mark_tier_finished(region_name, tier)

if __name__ == "__main__":
    main()
